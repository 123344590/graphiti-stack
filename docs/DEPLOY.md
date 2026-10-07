# Deploy runbook

Same pattern as the sibling `hermes-agent` deploy to `manty`: clone/update on
the target host, build, `docker compose up`. This one targets `viernes`
(10.147.200.5 on the `Manty_Red` ZeroTier network), which already has Docker
+ real sudo.

## 1. First-time deploy

On `viernes`, as the `soporte` user (has sudo + docker group):

```bash
git clone https://github.com/123344590/graphiti-stack.git ~/graphiti-stack
cd ~/graphiti-stack
cp .env.example .env
nano .env   # fill in LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, EMBEDDING_MODEL,
            # ADMIN_USER, ADMIN_PASSWORD
./scripts/bootstrap.sh
```

`bootstrap.sh` clones the upstream Graphiti repo into `repo/` (vendored, not
committed — it's `.gitignore`d), builds the `graphiti-mcp` image with the
pinned `GRAPHITI_CORE_VERSION`, and starts every service.

Open `http://10.147.200.5:8090` and log in with `ADMIN_USER`/`ADMIN_PASSWORD`
from `.env` (an actual login page, not the browser's native auth prompt) —
with no agents yet, it redirects straight to "crear agente" to create your
first one, handing you a Bearer token shown exactly once. Once an agent
exists, the panel always opens on an agent's own page (graph or vault); the
nav's agent switcher jumps between agents without leaving the section
you're on, and "+ Crear agente" is the one place that creates or revokes
tokens.

## 2. Point a Hermes agent at the stack

In that agent's profile (`~/.hermes/profiles/<name>/.env` on the Hermes host),
configure the Graphiti MCP connection to the proxy, not directly to
`graphiti-mcp`:

```
GRAPHITI_MCP_URL=http://10.147.200.5:8080/mcp/
GRAPHITI_MCP_TOKEN=<the token shown once in the admin panel>
```

The exact config keys depend on how the agent's MCP client config is wired
(`tools/mcp_tool.py` in hermes-agent, `streamable_http` transport) — point it
at the proxy's `/mcp/` path with the Bearer token in the connection's auth
header. Do **not** point any agent at `graphiti-mcp:8000` directly; that
bypasses the group_id enforcement entirely.

Each agent needs its own token (one per `group_id`) — never share a token
across two Hermes profiles, or they share a graph and a vault.

## 3. Updating the stack

```bash
cd ~/graphiti-stack
git pull
./scripts/bootstrap.sh   # re-clones/updates repo/, rebuilds, recreates containers
```

## 4. Checking agent isolation after any proxy change

Before trusting a change to `services/proxy/proxy.py` in production:

```bash
# as agent A's token, write a memory
curl -s -X POST http://10.147.200.5:8080/mcp/ \
  -H "Authorization: Bearer <A's token>" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"add_memory","arguments":{"name":"probe","episode_body":"isolation test"}}}'

# confirm the graph that landed in is A's own group, not "main" and not B's
docker compose exec falkordb redis-cli GRAPH.LIST
```

`GRAPH.LIST` should show a graph named after agent A's `group_id`. If it
shows `main` instead, the proxy failed to inject `group_id` — check that the
tool name reached `_enforce_group_id` and matches one of
`_SINGULAR_GROUP_TOOLS` / `_LIST_GROUP_TOOLS` in `proxy.py`.

## 5. Vaults and the in-page graph viewer

Each agent's vault is a plain directory at `./data/vaults/<group_id>/` on the
host (bind-mounted into `vault-service`), browsable/editable from that
agent's Vault tab in the admin panel. To seed a vault with existing notes,
drop `.md` files directly into that directory — the vault service and admin
panel pick them up on next read, no restart needed.

The Grafo tab queries FalkorDB directly (the `admin` service talks to
`falkordb:6379` over the internal docker network) and renders the result
in-page with vis-network — nothing to configure here beyond the stack
itself being up. `FALKORDB_BROWSER_PORT` (default 3000) still exposes
FalkorDB's own third-party Browser UI for raw Cypher/manual debugging, but
the admin panel's own graph view doesn't use it.

## Secrets

`.env` holds the LLM API key and the admin panel password — never commit it
(already `.gitignore`d). `data/agent_tokens.json` (inside the `agent_tokens`
Docker volume) holds every agent's Bearer token in plaintext; back it up like
a credentials file, not like ordinary state.
