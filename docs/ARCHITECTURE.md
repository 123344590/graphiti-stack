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
| `falkordb` | Graph storage (one logical graph per `group_id`) | No host port — only `falkordb-browser` and `graphiti-mcp` reach it, over the docker network |
| `falkordb-browser` | Official FalkorDB Browser, vendored with one source patch (see below) | Yes, but not meant to be browsed to directly — the admin panel embeds its `/graph?graph=<id>` page in an iframe |
| `graphiti-mcp` | Official Graphiti MCP server, unmodified | No — only reachable via `auth-proxy` |
| `auth-proxy` | Token auth + structural group_id/group_ids enforcement | Yes — the only agent-facing port |
| `vault-service` | Per-agent Obsidian vault over HTTP, same token scheme | No — only reachable via `admin` |
| `admin` | Operator web UI: create/revoke agent tokens, embedded per-agent graph view, vault browser/editor per agent | Yes — behind its own login page |

### Why the graph viewer embeds FalkorDB Browser instead of reimplementing one

Two wrong attempts preceded this. The first reimplemented a graph canvas
from scratch with vis-network, querying FalkorDB directly — a worse viewer
than the official app, built without first reading that app's own source.
The second tried linking out to FalkorDB Browser passing `?graph=<group_id>`
in the URL, assuming it would pre-select the database; that version of
FalkorDB Browser's graph switcher didn't read the parameter, so two
different agents' links rendered the exact same generic browser session.

Reading FalkorDB Browser's actual current source (not assuming from a
previous, stale check) settled both questions: its "share a link" feature
really does read `?graph=<name>` off the URL (`lib/graphTabs.ts`,
`parseSharedTab`) and opens directly on that graph — no patch needed for
that part. Auto-connecting to our `falkordb` service without its login form
is also a real, documented, env-var-only feature
(`lib/preconfiguredConnection.ts`: `FALKORDB_HOST`/`FALKORDB_PORT`/
`FALKORDB_AUTO_CONNECT`). The one real obstacle was that it refuses to be
framed: `next.config.js` declares a static `X-Frame-Options: DENY` header,
but that one never reaches the browser — `proxy.ts`, a per-request
middleware, sets its own `Content-Security-Policy` header (with a nonce for
inline scripts) on every non-API response, including a hardcoded
`frame-ancestors 'none'`, and that header is what Next.js actually sends.
(The first version of this patch touched only `next.config.js` and shipped
without live-testing the running app; `curl -I` against it afterwards still
showed `frame-ancestors 'none'` in the response — the real lesson being to
verify a patch against the running server, not just against the file it
edits.) `scripts/patch-browser-frame-embed.sh` (applied by `bootstrap.sh`
after every clone/update) patches `proxy.ts` instead: the hardcoded `'none'`
becomes a call to a small helper reading `CSP_FRAME_ANCESTORS`, mirroring
the exact pattern the file already uses for its `CSP_CONNECT_SRC` env var —
scoped to the admin panel's own origin, narrower than deleting the
directive outright, which would let any site embed it.

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
  upstream builds/images, version-pinned only. FalkorDB Browser is the one
  exception, and deliberately a narrow one: a single directive in its
  `proxy.ts` middleware (see above), applied by a script, not a maintained
  fork.
- It does not replace the bundled Hermes `obsidian` skill (filesystem-first,
  reads `OBSIDIAN_VAULT_PATH` locally) — it's a separate, optional way to
  reach the *same shape* of vault (a directory of `.md` files) remotely, for
  agents whose terminal/file tools don't run on this host, and for the admin
  panel's own browsing/editing UI.
