# Architecture

## Goal

A fleet of Hermes agents (`miguel`, `majo`, `larry`, `ana`, `aprovisionador`,
`aqua_admin`, and any future agent) each gets:

- a knowledge-graph memory (Graphiti, backed by FalkorDB), and
- an Obsidian vault (plain markdown notes),

with a hard guarantee: **agent A can never read or write agent B's graph or
vault, even if A's own model forgets to pass the right identifier, or tries
to pass someone else's on purpose.** Isolation is enforced by the
infrastructure, never by trusting the model to behave.

## Why isolation can't be "the model passes group_id correctly"

Graphiti's own MCP tools take `group_id` (or `group_ids`) as a plain
**optional** parameter with a `None`/default-group fallback. Nothing in
Graphiti itself stops a caller from omitting it — and in practice, a model
calling `add_memory` without being told to pass a specific group_id simply
doesn't, and the call silently lands in the shared default graph (`main`).
This was reproduced directly: an agent's own tool call explicitly reported
writing to "the default group" even though its skill instructed a specific
`group_id`. A prompt-level instruction is not a security boundary.

## How isolation is actually enforced

```
Hermes agent (any profile)
   │  Bearer <agent's own token>
   ▼
auth-proxy  (the ONLY port exposed to agents)
   │  looks up token -> group_id (re-reads agent_tokens.json every request)
   │  rewrites the JSON-RPC body: forces group_id/group_ids = that group_id
   │  on EVERY tools/call — injecting it if absent, overwriting it if present
   ▼
graphiti-mcp  (official upstream, unmodified)
   ▼
FalkorDB  (one graph keyed by group_id per agent)
```

The proxy (`services/proxy/proxy.py`) is the only thing agents can reach. It:

1. Requires a valid Bearer token (401 otherwise) — no anonymous access.
2. Parses every `tools/call` request body and looks at `params.name` to
   decide whether the tool takes a singular `group_id` or a list `group_ids`
   (mapped from Graphiti's own tool signatures — see the comment block at the
   top of `proxy.py` for the exact list).
3. **Always sets that field to the token's own group_id**, regardless of
   whether the caller's request included it, omitted it, or tried to set a
   different one. The caller-supplied value (if any) is discarded, not
   merged or trusted.

This means the identifier that actually reaches Graphiti is never something
the model chose — it's something the token (issued once, by the operator,
in the admin panel) determines. A model that "forgets" to pass `group_id`
gets it injected anyway. A model that tries to read another agent's memory
by passing their group_id gets overridden back to its own.

The vault service (`services/vault/vault_service.py`) uses the identical
pattern for Obsidian notes: the vault directory is resolved from the
token server-side, never from a client-supplied field, so there is no
parameter a client could even manipulate to name a different vault.

## Components

| Service | Role | Exposed? |
|---|---|---|
| `falkordb` | Graph storage (one logical graph per `group_id`) | Its own Browser UI on `FALKORDB_BROWSER_PORT`, for manual/debug use only — the admin panel's graph viewer talks to FalkorDB directly, not through it |
| `graphiti-mcp` | Official Graphiti MCP server, unmodified | No — only reachable via `auth-proxy` |
| `auth-proxy` | Token auth + structural group_id/group_ids enforcement | Yes — the only agent-facing port |
| `vault-service` | Per-agent Obsidian vault over HTTP, same token scheme | No — only reachable via `admin` |
| `admin` | Operator web UI: create/revoke agent tokens, in-page graph viewer per agent (queries FalkorDB directly, renders with vis-network), vault browser/editor per agent | Yes — behind its own login page |

### Why the graph viewer queries FalkorDB directly instead of embedding FalkorDB Browser

The first version of the graph viewer linked out to FalkorDB's own Browser UI
(`FALKORDB_BROWSER_PORT`), passing `?graph=<group_id>` in the URL to try to
pre-select the right database. Two problems surfaced in real use: FalkorDB
Browser sends `X-Frame-Options: SAMEORIGIN`, so it can't be embedded in an
iframe at all (confirmed: the same URL opened directly in a new tab works
fine, embedded it's refused) — and separately, its own graph switcher turned
out not to read that `?graph=` parameter, so two different agents' links
rendered the exact same generic browser session instead of two different
graphs. Querying FalkorDB directly (`select_graph(group_id).query(...)`,
same `falkordb` Python client Graphiti itself uses) and rendering the result
with vis-network sidesteps both problems: the viewer is genuinely part of
the admin panel's own page (same login session, same layout), and the graph
it shows is unambiguously the one `group_id` asked for.

## Known version pins (and why)

- `falkordb/falkordb:v4.22.0` — not `:latest`. FalkorDB v6's
  `db.idx.fulltext.createNodeIndex` module procedure takes at most 1 argument;
  graphiti-core's Cypher calls it with 5 (the v4.x signature). Bumping
  FalkorDB requires graphiti-core to have migrated first.
- `GRAPHITI_CORE_VERSION=0.30.2` build arg for `graphiti-mcp` — the upstream
  Dockerfile on `main` pins `0.29.1`, which predates the
  `OpenAIGenericClient(structured_output_mode=...)` keyword argument that
  `mcp_server`'s own code on `main` already calls. Any upstream pull that
  bumps `mcp_server`'s code may need a matching core-version bump here.

## What this does NOT do

- It does not fork or patch Graphiti or FalkorDB — both run unmodified
  upstream builds/images, version-pinned only.
- It does not replace the bundled Hermes `obsidian` skill (filesystem-first,
  reads `OBSIDIAN_VAULT_PATH` locally) — it's a separate, optional way to
  reach the *same shape* of vault (a directory of `.md` files) remotely, for
  agents whose terminal/file tools don't run on this host, and for the admin
  panel's own browsing/editing UI.
