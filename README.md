# graphiti-stack

Self-hosted Graphiti (knowledge-graph memory) + per-agent Obsidian vaults for a
fleet of Hermes agents, with:

- **FalkorDB** (graph backend) + **Graphiti MCP server**, both official upstream
  images/builds — not forked, just pinned to compatible versions.
- **FalkorDB Browser** (official graph visualization UI), embedded directly
  in the admin panel's Grafo page — with one small source patch to allow
  that embedding, see `docs/ARCHITECTURE.md`.
- **auth-proxy**: the only port reachable from outside — requires a per-agent
  Bearer token and *structurally* forces that agent's `group_id` on every
  `tools/call`, so no agent (by mistake or by trying) can read or write another
  agent's memory. This is enforced server-side, not by prompt instruction.
  Supports both a bare `/mcp/` URL and a `/mcp/<group_id>/` form for
  configuration clarity — either way, the Bearer token is what actually
  authorizes the request, never the URL alone.
- **vault-mcp**: per-agent Obsidian vault as a real MCP server
  (`@modelcontextprotocol/server-filesystem`), one child process per agent
  confined to its own directory — what a Hermes profile's `mcp_servers.obsidian`
  entry points at.
- **admin panel**: one logged-in app with a fixed sidebar (same shape as the
  sibling `hermes-agent` dashboard) — pick an agent once, it scopes both its
  embedded graph view and its Obsidian vault (list/read/write notes)
  together. "Agentes" in the sidebar is the one place that creates or
  revokes tokens.
- **vault-service**: per-agent Obsidian vault directories over a plain REST
  API, for the admin panel's own browser/editor UI (distinct from vault-mcp
  above, which is what a Hermes agent calls as an MCP tool).

See `docs/ARCHITECTURE.md` for the full design and the known FalkorDB/graphiti-core
version-compatibility pin, and `docs/DEPLOY.md` for the deploy runbook (same
pattern as this project's sibling `hermes-agent` deployment: clone, `.env`,
`docker compose up`).

## Quick start (fresh host, Docker already installed)

```bash
git clone <this-repo-url> graphiti-stack
cd graphiti-stack
cp .env.example .env    # fill in your LLM/embedder provider
./scripts/bootstrap.sh  # clones graphiti upstream, builds, starts the stack
```

Then open `http://<host>:8090` (admin panel) to create your first agent.

## Layout

```
graphiti-stack/
├── docker-compose.yml       # falkordb, falkordb-browser, graphiti-mcp, auth-proxy,
│                            # vault-service, vault-mcp, admin
├── services/
│   ├── proxy/proxy.py          # token auth + structural group_id/group_ids enforcement
│   ├── vault/vault_service.py  # per-agent Obsidian vault REST API, for the admin panel's UI
│   ├── vault-mcp/supervisor.py # per-agent Obsidian vault as a real MCP server (one child
│   │                          # process per agent), for a Hermes profile's mcp_servers.obsidian
│   └── admin/admin.py       # operator UI: agents/tokens, embedded FalkorDB Browser, vault editor
├── scripts/
│   ├── bootstrap.sh                    # clone upstream Graphiti + FalkorDB Browser, patch, build, up
│   └── patch-browser-frame-embed.sh    # the one source patch applied to FalkorDB Browser
├── docs/ARCHITECTURE.md     # isolation guarantee, component map, version pins
└── docs/DEPLOY.md           # deploy runbook
```
