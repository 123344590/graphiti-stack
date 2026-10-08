# Deploy runbook

Same pattern as the sibling `hermes-agent` deploy to `manty`: clone/update on
the target host, build, `docker compose up`. Examples below use `viernes`
(10.147.200.5 on the `Manty_Red` ZeroTier network) and its `soporte` user,
but the same steps apply to any Docker host — substitute your own hostname/
IP and user. (This project has also been deployed this way to `servidor-casa`,
reusing an existing Ollama container on its own docker network for
embeddings — see the "reusing an existing service" note in step 2.)

## 1. First-time deploy

As a user with Docker access on the target host:

```bash
git clone https://github.com/123344590/graphiti-stack.git ~/graphiti-stack
cd ~/graphiti-stack
cp .env.example .env
nano .env   # fill in LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, EMBEDDING_MODEL,
            # ADMIN_USER, ADMIN_PASSWORD, PUBLIC_HOST (this host's IP/hostname
            # reachable by whoever opens the admin panel in a browser)
./scripts/bootstrap.sh
```

`bootstrap.sh` clones the upstream Graphiti repo into `repo/` and FalkorDB
Browser into `repo-browser/` (both vendored, not committed — `.gitignore`d),
applies the one source patch FalkorDB Browser needs
(`scripts/patch-browser-frame-embed.sh`), builds the `graphiti-mcp` image
with the pinned `GRAPHITI_CORE_VERSION`, and starts every service.

Open `http://<PUBLIC_HOST>:8090` (e.g. `http://10.147.200.5:8090` for
`viernes`) and log in with `ADMIN_USER`/`ADMIN_PASSWORD`
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
GRAPHITI_MCP_URL=http://10.147.200.5:8080/mcp/miguel/
GRAPHITI_MCP_TOKEN=<the token shown once in the admin panel, for THIS agent>
```

The `/mcp/<group_id>/` path segment names which agent the URL is for —
purely for configuration clarity, so a glance at an agent's `.env` tells you
whose endpoint it is. It is **not** what authorizes the request: the Bearer
token is still required and must belong to that same agent, or the proxy
rejects the request with 403 before it ever reaches Graphiti. A copy-pasted
URL with the wrong token for it fails closed, it never silently falls back
to either identity. The bare `/mcp/` path (no agent name) still works
exactly as before for any caller not yet updated to the per-agent form.

The exact config keys depend on how the agent's MCP client config is wired
(`tools/mcp_tool.py` in hermes-agent, `streamable_http` transport) — point it
at the proxy's `/mcp/<group_id>/` path with that agent's Bearer token in the
connection's auth header. Do **not** point any agent at `graphiti-mcp:8000`
directly; that bypasses the group_id enforcement entirely.

Each agent needs its own token (one per `group_id`) — never share a token
across two Hermes profiles, or they share a graph and a vault. Verify it
below (§4) after wiring any agent, especially the first time: a URL naming
one agent with another agent's token is exactly the mistake the 403 exists
to catch.

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

Also confirm the per-agent URL can't be used with the wrong token — this
should return **403**, not 200 and not a silent fallback to either agent:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://10.147.200.5:8080/mcp/miguel/ \
  -H "Authorization: Bearer <B's token>" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
```

## 5. Vaults and the in-page graph viewer

Each agent's vault is a plain directory at `./data/vaults/<group_id>/` on the
host (bind-mounted into `vault-service`), browsable/editable from that
agent's Vault item in the admin panel's sidebar. Picking an agent from the
sidebar's agent switcher scopes both its Grafo and Vault items together —
there's no separate per-section agent picker to keep in sync. To seed a
vault with existing notes, drop `.md` files directly into that directory —
the vault service and admin panel pick them up on next read, no restart
needed.

The Grafo tab embeds the official FalkorDB Browser (service
`falkordb-browser`, `FALKORDB_BROWSER_PORT`, default 3002) in an iframe,
pre-scoped to that agent's own graph via its own `?graph=<name>` share-link
feature. It auto-connects to the stack's `falkordb` service and never shows
its own login form (`FALKORDB_AUTO_CONNECT=true`). Its source is patched
in exactly one place by `scripts/patch-browser-frame-embed.sh` (part of
`bootstrap.sh`) to allow that embedding — upstream otherwise refuses to be
framed at all. Browsing directly to `http://<host>:${FALKORDB_BROWSER_PORT}`
works (it's the same app, unauthenticated by its own login thanks to
auto-connect) but shows whatever graph it last had open, not scoped to one
agent — always go through the admin panel's Grafo page instead.

## Secrets

`.env` holds the LLM API key and the admin panel password — never commit it
(already `.gitignore`d). `data/agent_tokens.json` (inside the `agent_tokens`
Docker volume) holds every agent's Bearer token in plaintext; back it up like
a credentials file, not like ordinary state.
