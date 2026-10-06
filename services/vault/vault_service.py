#!/usr/bin/env python3
"""Per-agent Obsidian vault service.

Each agent's vault lives at VAULTS_ROOT/<group_id>/ — a plain directory of .md
files, same shape the bundled Hermes `obsidian` skill already expects at
OBSIDIAN_VAULT_PATH (see skills/note-taking/obsidian/SKILL.md: filesystem-first,
no official Obsidian server exists). This service does NOT replace that skill;
it exposes the same files over HTTP so:
  - the admin panel can list/read/write notes for any agent (operator view), and
  - a Hermes agent itself can be pointed at its own vault remotely (e.g. the
    `viernes` box is not where the agent's terminal tools run) using the same
    per-agent Bearer token the auth-proxy issues for Graphiti.

Isolation is structural, same as the Graphiti proxy: the token's group_id IS
the vault directory name, resolved server-side from the token, never taken
from a client-supplied field. A request with agent A's token can only ever
resolve to agent A's directory; there is no parameter that lets a caller name
a different vault.

Path safety: group_id is validated against a strict slug pattern before ever
touching the filesystem, and every note path is resolved and checked to stay
inside that agent's vault directory, rejecting any `..` escape.
"""
import json
import os
import re
from pathlib import Path, PurePosixPath

from aiohttp import web

VAULTS_ROOT = Path(os.environ.get("VAULTS_ROOT", "/data/vaults"))
TOKENS_PATH = Path(os.environ.get("AGENT_TOKENS_PATH", "/data/agent_tokens.json"))
_SLUG_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _load_tokens() -> dict[str, str]:
    try:
        data = json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, UnicodeDecodeError):
        return {}


def _agent_vault_dir(group_id: str) -> Path:
    if not _SLUG_RE.match(group_id):
        raise web.HTTPBadRequest(text="invalid group_id")
    vault_dir = VAULTS_ROOT / group_id
    vault_dir.mkdir(parents=True, exist_ok=True)
    return vault_dir


def _resolve_note_path(vault_dir: Path, rel_path: str) -> Path:
    """Resolve *rel_path* inside *vault_dir*, rejecting any escape attempt."""
    candidate = (vault_dir / PurePosixPath(rel_path)).resolve()
    vault_resolved = vault_dir.resolve()
    if candidate != vault_resolved and vault_resolved not in candidate.parents:
        raise web.HTTPBadRequest(text="path escapes vault")
    return candidate


def _authenticate(request: web.Request) -> str:
    """Returns the group_id bound to the request's Bearer token, or raises 401."""
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    group_id = _load_tokens().get(token)
    if not group_id:
        raise web.HTTPUnauthorized(text="invalid or missing token")
    return group_id


async def handle_list(request: web.Request) -> web.Response:
    group_id = _authenticate(request)
    vault_dir = _agent_vault_dir(group_id)
    notes = sorted(
        str(p.relative_to(vault_dir)) for p in vault_dir.rglob("*.md") if p.is_file()
    )
    return web.json_response({"group_id": group_id, "notes": notes})


async def handle_read(request: web.Request) -> web.Response:
    group_id = _authenticate(request)
    vault_dir = _agent_vault_dir(group_id)
    rel_path = request.query.get("path", "")
    if not rel_path:
        raise web.HTTPBadRequest(text="missing path")
    note_path = _resolve_note_path(vault_dir, rel_path)
    if not note_path.is_file():
        raise web.HTTPNotFound(text="note not found")
    return web.json_response({"path": rel_path, "content": note_path.read_text(encoding="utf-8")})


async def handle_write(request: web.Request) -> web.Response:
    group_id = _authenticate(request)
    vault_dir = _agent_vault_dir(group_id)
    payload = await request.json()
    rel_path = str(payload.get("path", ""))
    content = payload.get("content")
    if not rel_path or not rel_path.endswith(".md") or content is None:
        raise web.HTTPBadRequest(text="expected {path: '*.md', content: str}")
    note_path = _resolve_note_path(vault_dir, rel_path)
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(content, encoding="utf-8")
    return web.json_response({"path": rel_path, "bytes": len(content)})


async def handle_delete(request: web.Request) -> web.Response:
    group_id = _authenticate(request)
    vault_dir = _agent_vault_dir(group_id)
    rel_path = request.query.get("path", "")
    if not rel_path:
        raise web.HTTPBadRequest(text="missing path")
    note_path = _resolve_note_path(vault_dir, rel_path)
    if note_path.is_file():
        note_path.unlink()
    return web.json_response({"path": rel_path, "deleted": True})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "healthy", "service": "vault-service"})


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/vault-health", handle_health)
    app.router.add_get("/notes", handle_list)
    app.router.add_get("/notes/read", handle_read)
    app.router.add_put("/notes/write", handle_write)
    app.router.add_delete("/notes/delete", handle_delete)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=int(os.environ.get("VAULT_PORT", "8070")))
