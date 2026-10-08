#!/usr/bin/env python3
"""Per-agent Obsidian vault as a REAL MCP server, not just a REST API.

Why this exists: the bundled Hermes setups that predate graphiti-stack wire
`obsidian` as an MCP tool server (`@modelcontextprotocol/server-filesystem`
behind `supergateway`, giving the model read_file/write_file/list_directory/
etc. as proper MCP tools) -- not as a plain HTTP API. services/vault's
vault_service.py is a REST API for the admin panel's own browser/editor UI;
it was never meant to be what a Hermes agent calls as an MCP tool, and
`@modelcontextprotocol/server-filesystem` can't point two different clients
at two different roots through one shared process -- its root directory is
fixed at process-launch time via argv.

So each agent gets its OWN `supergateway + server-filesystem` child process,
confined to its own `VAULTS_ROOT/<group_id>/` directory, listening on its own
internal port. This supervisor:
  - reads agent_tokens.json (same file the Graphiti proxy and admin panel
    read) and launches one child process per known group_id, restarting any
    child that dies;
  - re-scans that file periodically so creating/revoking an agent in the
    admin panel starts/stops the matching child without restarting this
    service;
  - runs a tiny HTTP proxy on the public port that resolves the caller's
    Bearer token to its group_id (exactly like services/proxy/proxy.py),
    then forwards to that agent's own child process by port -- the port
    number itself is never agent-identifying information the caller sees;
    only the token decides which child receives the request.

This mirrors the Graphiti proxy's isolation guarantee for the exact same
reason: a child process can only ever read/write inside the one directory
it was launched against, so there is no code path -- correct or buggy -- in
server-filesystem itself that could serve agent B's files to agent A's
session. The isolation is structural (OS-level process confinement), not
just "the code happens to filter correctly."
"""
import asyncio
import json
import logging
import os
import signal
from pathlib import Path

from aiohttp import web, ClientSession, ClientTimeout

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vault-mcp-supervisor")

LISTEN_PORT = int(os.environ.get("VAULT_MCP_PORT", "8080"))
VAULTS_ROOT = Path(os.environ.get("VAULTS_ROOT", "/data/vaults"))
TOKENS_PATH = Path(os.environ.get("AGENT_TOKENS_PATH", "/data/agent_tokens.json"))
RESCAN_INTERVAL_SECONDS = 5
# Internal ports for child processes -- never exposed outside this container;
# the only public port is LISTEN_PORT, fronted by the proxy below.
_CHILD_PORT_BASE = 9100

_HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "host", "content-length"}


def _load_tokens() -> dict[str, str]:
    try:
        data = json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, UnicodeDecodeError):
        return {}


class ChildProcess:
    def __init__(self, group_id: str, port: int):
        self.group_id = group_id
        self.port = port
        self.proc: asyncio.subprocess.Process | None = None

    async def ensure_running(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            return
        vault_dir = VAULTS_ROOT / self.group_id
        vault_dir.mkdir(parents=True, exist_ok=True)
        log.info("starting vault MCP child for group_id=%s on port %d", self.group_id, self.port)
        self.proc = await asyncio.create_subprocess_exec(
            "supergateway",
            "--stdio", f"mcp-server-filesystem {vault_dir}",
            "--outputTransport", "streamableHttp",
            "--stateful",
            "--sessionTimeout", "86400000",
            "--port", str(self.port),
            "--streamableHttpPath", "/mcp",
            "--healthEndpoint", "/healthz",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def stop(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None


class Supervisor:
    def __init__(self) -> None:
        self._children: dict[str, ChildProcess] = {}
        self._next_port = _CHILD_PORT_BASE

    def _port_for(self, group_id: str) -> int:
        if group_id not in self._children:
            port = self._next_port
            self._next_port += 1
            self._children[group_id] = ChildProcess(group_id, port)
        return self._children[group_id].port

    async def reconcile(self) -> None:
        """Start a child for every agent currently in agent_tokens.json;
        stop children for agents that were revoked. Runs on a timer so the
        admin panel's create/revoke actions take effect without restarting
        this service -- same live-reload guarantee the Graphiti proxy and
        vault_service.py already give for their own token checks."""
        known_agents = set(_load_tokens().values())
        for group_id in known_agents:
            child = self._children.get(group_id)
            if child is None:
                self._port_for(group_id)
                child = self._children[group_id]
            await child.ensure_running()
        for group_id in list(self._children):
            if group_id not in known_agents:
                await self._children[group_id].stop()
                del self._children[group_id]

    def port_for_existing(self, group_id: str) -> int | None:
        child = self._children.get(group_id)
        return child.port if child else None

    async def reconcile_loop(self) -> None:
        while True:
            try:
                await self.reconcile()
            except Exception:
                log.exception("reconcile failed")
            await asyncio.sleep(RESCAN_INTERVAL_SECONDS)


_supervisor = Supervisor()


async def handle_proxy(request: web.Request) -> web.StreamResponse:
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    token_group_id = _load_tokens().get(token)
    if not token_group_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    # /mcp/<url_group_id>/<rest> -- same per-agent URL convention as the
    # Graphiti proxy (services/proxy/proxy.py): the path segment identifies
    # the agent for configuration clarity only. The token is still what
    # authorizes the request, and a URL naming a DIFFERENT agent than the
    # token belongs to is rejected outright.
    url_group_id = request.match_info.get("group_id")
    if url_group_id is not None:
        if url_group_id != token_group_id:
            return web.json_response({"error": "token does not match the agent in this URL"}, status=403)
        rest = request.match_info["rest"]
        upstream_path = f"mcp/{rest}" if rest else "mcp/"
    else:
        upstream_path = request.match_info["upstream_path"]
    group_id = token_group_id

    port = _supervisor.port_for_existing(group_id)
    if port is None:
        # Known token but the reconcile loop hasn't started its child yet
        # (e.g. agent created seconds ago) -- force an immediate reconcile
        # rather than making the caller wait out the full poll interval.
        await _supervisor.reconcile()
        port = _supervisor.port_for_existing(group_id)
    if port is None:
        return web.json_response({"error": "vault MCP not ready for this agent yet"}, status=503)

    query = f"?{request.query_string}" if request.query_string else ""
    url = f"http://127.0.0.1:{port}/{upstream_path}{query}"
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    body = await request.read()

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
    return web.json_response({"status": "healthy", "service": "vault-mcp-supervisor"})


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/vault-mcp-health", handle_health)
    # Per-agent URL form first (more specific) -- aiohttp matches routes in
    # registration order, so this must come before the bare catch-all or it
    # would never be reached. Same convention as services/proxy/proxy.py.
    app.router.add_route("*", "/mcp/{group_id}/{rest:.*}", handle_proxy)
    app.router.add_route("*", "/{upstream_path:.*}", handle_proxy)

    async def _start_background(app: web.Application) -> None:
        app["reconcile_task"] = asyncio.create_task(_supervisor.reconcile_loop())

    app.on_startup.append(_start_background)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=LISTEN_PORT)
