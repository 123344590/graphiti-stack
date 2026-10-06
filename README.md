# graphiti-stack

Self-hosted Graphiti (knowledge-graph memory) + per-agent Obsidian vaults for a
fleet of Hermes agents, with:

- **FalkorDB** (graph backend) + **Graphiti MCP server**, both official upstream
  images/builds — not forked, just pinned to compatible versions.
- **auth-proxy**: the only port reachable from outside — requires a per-agent
  Bearer token and *structurally* forces that agent's `group_id` on every
  `tools/call`, so no agent (by mistake or by trying) can read or write another
  agent's memory. This is enforced server-side, not by prompt instruction.
- **admin panel**: create/revoke agent tokens, view each agent's graph (embedded
  FalkorDB Browser, scoped to that agent's own database) and manage its Obsidian
  vault (list/read/write notes) from one web UI.
- **vault service**: per-agent Obsidian vault directories, exposed read/write
  over the same per-agent Bearer tokens so each Hermes profile's `terminal`/file
  tools (or the bundled `obsidian` skill) can reach only its own vault.

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
├── docker-compose.yml       # falkordb, graphiti-mcp, auth-proxy, vault-service, admin
├── services/
│   ├── proxy/proxy.py       # token auth + structural group_id/group_ids enforcement
│   ├── vault/vault_service.py  # per-agent Obsidian vault over HTTP, same token scheme
│   └── admin/admin.py       # operator UI: agents/tokens, embedded graph viewer, vault editor
├── scripts/bootstrap.sh     # clone upstream Graphiti, build, compose up (idempotent)
├── docs/ARCHITECTURE.md     # isolation guarantee, component map, version pins
└── docs/DEPLOY.md           # runbook for viernes (ZeroTier 10.147.200.5)
```
