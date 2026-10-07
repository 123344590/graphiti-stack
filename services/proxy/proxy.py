#!/usr/bin/env python3
"""Auth + group_id-enforcing reverse proxy in front of the Graphiti MCP server.

Graphiti itself has no authentication, and group_id is a plain, OPTIONAL parameter
the caller may pass on a tools/call -- nothing on the server side stops one agent
from omitting it (falling back to the shared default group) or, worse, passing
another agent's group_id deliberately and reading/writing its memory.

This proxy closes that gap structurally, not by prompt instruction:
  - Each agent gets its own Bearer token (token -> group_id map, re-read from
    AGENT_TOKENS_PATH on every request so the admin panel's edits take effect
    live, no proxy restart needed).
  - Every tools/call request has its group-scoping argument(s) FORCED to that
    token's group_id -- INJECTED if absent, OVERWRITTEN if present. The first
    version of this proxy only overwrote an existing key, which silently did
    nothing when the caller omitted the parameter entirely (the common case --
    group_id is optional with a None default on nearly every Graphiti tool,
    so a model that doesn't explicitly set it produces an args dict with no
    group_id key at all, and `if "group_id" in args` is simply False).
  - Tool-specific shape: some tools take a singular `group_id: str`, others take
    `group_ids: str | list[str]` (see GROUP_ARG_BY_TOOL). Tools with neither
    (get_status) are left untouched.
  - Non-tools/call methods (initialize, tools/list, ...) pass through unchanged.
  - Streams the response body (SSE / chunked) rather than buffering it whole --
    required for MCP's streamable-HTTP transport, which keeps the connection
    open; buffer-then-reply breaks it ("SSE stream ended without a response").

URL shape: an agent's MCP endpoint is /mcp/<group_id>/ -- the path names
which agent this is FOR CONFIGURATION CLARITY, but it is never trusted on
its own. The Bearer token is still required and must resolve (via
agent_tokens.json) to that SAME group_id, or the request is rejected before
it ever reaches Graphiti -- the URL documents intent, the token is what
actually authorizes it. The bare /mcp/ path (no agent name) still works
exactly as before, resolving group_id from the token alone, for any caller
not yet updated to the per-agent URL.
"""
import json
import logging
import os
from pathlib import Path

from aiohttp import web, ClientSession, ClientTimeout

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("graphiti-proxy")

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(os.environ.get("PROXY_PORT", "8080"))
UPSTREAM = os.environ.get("UPSTREAM", "http://graphiti-mcp:8000")
TOKENS_PATH = Path(os.environ.get("AGENT_TOKENS_PATH", "/data/agent_tokens.json"))

_HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "host", "content-length"}

# Per Graphiti MCP server's own tool signatures (src/graphiti_mcp_server.py):
# singular `group_id: str | None` on these --
_SINGULAR_GROUP_TOOLS = {
    "add_memory", "delete_entity_edge", "delete_episode", "get_entity_edge",
    "summarize_saga", "add_triplet", "get_episode_entities",
}
# list-shaped `group_ids: str | list[str] | None` on these --
_LIST_GROUP_TOOLS = {
    "search_nodes", "search_memory_facts", "get_episodes", "build_communities",
    "clear_graph",
}
# Tools with neither (get_status) are left alone -- any tool name not in either
# set above also passes through unmodified (fail-open on the ENFORCEMENT only;
# the request still requires a valid token to reach this point at all).


def _load_tokens() -> dict[str, str]:
    """Re-read on every call -- a small JSON file, cheap next to the network hop
    this proxy already makes per request. Lets the admin panel's token edits
    take effect on the very next request, no proxy restart or signal needed."""
    try:
        data = json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, UnicodeDecodeError):
        return {}


def _enforce_group_id(body: bytes, group_id: str) -> tuple[bytes, bool]:
    """For a tools/call request, force the group-scoping argument to *group_id*,
    injecting it if the caller omitted it. Returns (possibly-rewritten body,
    whether a rewrite happened) -- the caller logs only on an actual rewrite.
    Any other method, or an unparseable/non-JSON/non-dict body, passes through
    byte-for-byte unchanged: this function only ever narrows access for a
    recognized tools/call, never widens it, and a parse failure fails CLOSED
    toward Graphiti's own error handling rather than guessing at intent."""
    try:
        msg = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body, False
    if not isinstance(msg, dict) or msg.get("method") != "tools/call":
        return body, False
    params = msg.get("params")
    if not isinstance(params, dict):
        return body, False
    tool_name = params.get("name")
    args = params.setdefault("arguments", {})
    if not isinstance(args, dict):
        return body, False

    if tool_name in _SINGULAR_GROUP_TOOLS:
        if args.get("group_id") != group_id:
            args["group_id"] = group_id
            return json.dumps(msg).encode("utf-8"), True
    elif tool_name in _LIST_GROUP_TOOLS:
        if args.get("group_ids") != [group_id]:
            args["group_ids"] = [group_id]
            return json.dumps(msg).encode("utf-8"), True
    return body, False


async def handle(request: web.Request) -> web.StreamResponse:
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    token_group_id = _load_tokens().get(token)
    if not token_group_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    # /mcp/<url_group_id>/<rest> -- present only on the per-agent URL form,
    # e.g. /mcp/miguel/ itself (rest="") or /mcp/miguel/some/sub/path. The
    # token's own group_id is the sole source of truth; a URL naming a
    # DIFFERENT agent than the token belongs to is rejected outright rather
    # than silently using either one, so a stale/copy-pasted URL can never
    # quietly operate on the wrong agent's memory. Upstream (Graphiti itself)
    # knows nothing about per-agent paths, so the agent-name segment is
    # stripped and "mcp/" is restored in its place before forwarding --
    # /mcp/miguel/ becomes upstream /mcp/, /mcp/miguel/foo becomes /mcp/foo.
    url_group_id = request.match_info.get("group_id")
    if url_group_id is not None:
        if url_group_id != token_group_id:
            return web.json_response({"error": "token does not match the agent in this URL"}, status=403)
        rest = request.match_info["rest"]
        upstream_path = f"mcp/{rest}" if rest else "mcp/"
    else:
        upstream_path = request.match_info["upstream_path"]
    group_id = token_group_id

    query = f"?{request.query_string}" if request.query_string else ""
    url = f"{UPSTREAM}/{upstream_path}{query}"
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    raw_body = await request.read()
    body, rewrote = _enforce_group_id(raw_body, group_id) if raw_body else (raw_body, False)
    if rewrote:
        log.info("forced group scoping to group_id=%s on tools/call", group_id)

    timeout = ClientTimeout(total=None, sock_connect=10, sock_read=None)
    async with ClientSession(timeout=timeout) as session:
        async with session.request(
            request.method, url, headers=fwd_headers, data=body if body else None,
        ) as resp:
            resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}
            response = web.StreamResponse(status=resp.status, headers=resp_headers)
            await response.prepare(request)
            async for chunk in resp.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
            return response


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "healthy", "service": "graphiti-auth-proxy"})


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/proxy-health", handle_health)
    # Per-agent URL form first (more specific) -- aiohttp matches routes in
    # registration order, so this must come before the bare catch-all or it
    # would never be reached.
    app.router.add_route("*", "/mcp/{group_id}/{rest:.*}", handle)
    app.router.add_route("*", "/{upstream_path:.*}", handle)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host=LISTEN_HOST, port=LISTEN_PORT)
