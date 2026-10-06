#!/usr/bin/env python3
"""Admin panel for the graphiti-stack deployment.

One page to: create/revoke per-agent tokens (shared by the Graphiti auth-proxy
and the vault service — same token, same group_id, one identity per agent),
view each agent's knowledge graph (embedded FalkorDB Browser iframe, scoped to
that agent's own graph key so operators never land on the shared default graph
by accident), and browse/edit each agent's Obsidian vault (list notes, read,
edit, save) via the vault service's HTTP API.

Single operator account (HTTP Basic) — infrastructure administration, not a
multi-tenant app, same pattern Hermes's own dashboard uses for a non-loopback
bind.

Writes directly to the same agent_tokens.json the auth-proxy and vault service
both read. Every one of those three processes re-reads the file per request
(cheap: a few KB of JSON next to work that already does a network hop), so a
token created/revoked here takes effect everywhere on the very next request —
no restart, no signal, no coordination beyond the shared bind-mounted file.
"""
import html
import json
import os
import secrets
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiohttp import web, BasicAuth

TOKENS_PATH = Path(os.environ.get("AGENT_TOKENS_PATH", "/data/agent_tokens.json"))
ADMIN_USER = os.environ["ADMIN_USER"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
FALKORDB_BROWSER_BASE = os.environ.get("FALKORDB_BROWSER_BASE", "http://10.147.200.5:3000")
VAULT_SERVICE_BASE = os.environ.get("VAULT_SERVICE_BASE", "http://vault-service:8070")


def _load() -> dict[str, str]:
    if not TOKENS_PATH.exists():
        return {}
    try:
        data = json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def _save(tokens: dict[str, str]) -> None:
    TOKENS_PATH.write_text(json.dumps(tokens, indent=2), encoding="utf-8")


def _token_for(group_id: str, tokens: dict[str, str]) -> str | None:
    for tok, gid in tokens.items():
        if gid == group_id:
            return tok
    return None


def _require_auth(request: web.Request) -> None:
    hdr = request.headers.get("Authorization", "")
    try:
        auth = BasicAuth.decode(hdr)
    except ValueError:
        raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="graphiti-admin"'})
    if not (secrets.compare_digest(auth.login, ADMIN_USER)
            and secrets.compare_digest(auth.password, ADMIN_PASSWORD)):
        raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="graphiti-admin"'})


def _layout(body: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>Graphiti Admin</title>
<meta charset="utf-8">
<style>
body {{ font-family: system-ui, sans-serif; max-width: 960px; margin: 40px auto; padding: 0 16px; color: #1a1a1a; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
td, th {{ border-bottom: 1px solid #ddd; padding: 8px; text-align: left; vertical-align: top; }}
input[type=text], textarea {{ padding: 6px; font-family: inherit; }}
input[type=text] {{ width: 240px; }}
textarea {{ width: 100%; min-height: 320px; font-family: ui-monospace, monospace; font-size: 0.9em; }}
button {{ padding: 6px 12px; cursor: pointer; }}
a {{ color: #2563eb; }}
.nav {{ margin-bottom: 24px; }}
.nav a {{ margin-right: 16px; }}
.banner {{ background:#fffae0; border:1px solid #d9b500; padding:12px; margin-bottom:16px; }}
iframe {{ width: 100%; height: 640px; border: 1px solid #ddd; margin-top: 12px; }}
ul.notes {{ list-style: none; padding: 0; }}
ul.notes li {{ padding: 4px 0; border-bottom: 1px solid #eee; }}
</style></head>
<body>
<div class="nav"><a href="/">Agentes</a></div>
{body}
</body></html>"""


def _agents_page(tokens: dict[str, str], *, created: tuple[str, str] | None = None) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(group_id)}</td>"
        f"<td><a href=\"/agents/{quote(group_id)}/graph\">ver grafo</a></td>"
        f"<td><a href=\"/agents/{quote(group_id)}/vault\">ver vault</a></td>"
        f"<td><form method=\"post\" action=\"/delete\" style=\"display:inline\">"
        f"<input type=\"hidden\" name=\"group_id\" value=\"{html.escape(group_id)}\">"
        f"<button type=\"submit\" onclick=\"return confirm('Revocar el token de {html.escape(group_id)}?')\">revocar</button>"
        f"</form></td></tr>"
        for group_id in sorted(tokens.values())
    )
    banner = ""
    if created:
        group_id, token = created
        banner = (
            f"<div class=\"banner\"><b>Token para \"{html.escape(group_id)}\" "
            f"— cópialo ahora, no se volverá a mostrar:</b><br>"
            f"<code style=\"font-size:1.1em\">{html.escape(token)}</code></div>"
        )
    return _layout(f"""
<h1>Graphiti — Agentes</h1>
{banner}
<form method="post" action="/create">
  <input type="text" name="group_id" placeholder="nombre del agente (ej. miguel)" required pattern="[a-zA-Z0-9_-]+">
  <button type="submit">Crear agente + token</button>
</form>
<table>
<thead><tr><th>Agente (group_id)</th><th>Grafo</th><th>Vault</th><th></th></tr></thead>
<tbody>{rows}</tbody>
</table>""")


def _graph_page(group_id: str) -> str:
    browser_url = f"{FALKORDB_BROWSER_BASE}/?graph={quote(group_id)}"
    return _layout(f"""
<h1>Grafo de "{html.escape(group_id)}"</h1>
<p>FalkorDB Browser, pre-seleccionado en el grafo de este agente.
Si pide conexión manual, host/puerto son los del propio FalkorDB en este stack
(ver <code>docs/DEPLOY.md</code>) — el <code>graph</code> en la URL ya fija la base
de datos a <code>{html.escape(group_id)}</code>, no al grafo compartido
<code>main</code>.</p>
<iframe src="{browser_url}"></iframe>
""")


async def _vault_request(method: str, group_id: str, token: str, path: str, **kwargs):
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.request(method, f"{VAULT_SERVICE_BASE}{path}", headers=headers, **kwargs) as resp:
            if resp.status >= 400:
                raise web.HTTPBadGateway(text=f"vault-service error {resp.status}: {await resp.text()}")
            return await resp.json()


def _vault_page(group_id: str, notes: list[str], *, open_note: str | None, content: str | None, saved: bool) -> str:
    note_items = "".join(
        f"<li><a href=\"/agents/{quote(group_id)}/vault?note={quote(n)}\">{html.escape(n)}</a></li>"
        for n in notes
    ) or "<li><em>(sin notas aún)</em></li>"
    editor = ""
    if open_note is not None:
        saved_banner = '<p style="color:#15803d">Guardado.</p>' if saved else ""
        editor = f"""
<h2>{html.escape(open_note)}</h2>
{saved_banner}
<form method="post" action="/agents/{quote(group_id)}/vault/save">
  <input type="hidden" name="note" value="{html.escape(open_note)}">
  <textarea name="content">{html.escape(content or "")}</textarea><br>
  <button type="submit">Guardar</button>
</form>"""
    new_note_form = f"""
<h3>Nueva nota</h3>
<form method="post" action="/agents/{quote(group_id)}/vault/save">
  <input type="text" name="note" placeholder="nombre.md" required pattern=".+\\.md">
  <textarea name="content" placeholder="Contenido markdown..."></textarea><br>
  <button type="submit">Crear nota</button>
</form>"""
    return _layout(f"""
<h1>Vault de "{html.escape(group_id)}"</h1>
<div style="display:flex; gap:32px;">
  <div style="flex:1"><h3>Notas</h3><ul class="notes">{note_items}</ul>{new_note_form}</div>
  <div style="flex:2">{editor}</div>
</div>
""")


async def handle_index(request: web.Request) -> web.Response:
    _require_auth(request)
    return web.Response(text=_agents_page(_load()), content_type="text/html")


async def handle_create(request: web.Request) -> web.Response:
    _require_auth(request)
    form = await request.post()
    group_id = str(form.get("group_id", "")).strip()
    if not group_id or not group_id.replace("_", "").replace("-", "").isalnum():
        raise web.HTTPBadRequest(text="Nombre de agente inválido")
    tokens = _load()
    if group_id in tokens.values():
        raise web.HTTPBadRequest(text=f"El agente '{group_id}' ya existe — revócalo primero si quieres regenerar su token")
    new_token = secrets.token_urlsafe(32)
    tokens[new_token] = group_id
    _save(tokens)
    return web.Response(text=_agents_page(tokens, created=(group_id, new_token)), content_type="text/html")


async def handle_delete(request: web.Request) -> web.Response:
    _require_auth(request)
    form = await request.post()
    group_id = str(form.get("group_id", "")).strip()
    tokens = _load()
    tokens = {tok: gid for tok, gid in tokens.items() if gid != group_id}
    _save(tokens)
    raise web.HTTPFound("/")


async def handle_graph(request: web.Request) -> web.Response:
    _require_auth(request)
    group_id = request.match_info["group_id"]
    if group_id not in _load().values():
        raise web.HTTPNotFound(text="unknown agent")
    return web.Response(text=_graph_page(group_id), content_type="text/html")


async def handle_vault(request: web.Request) -> web.Response:
    _require_auth(request)
    group_id = request.match_info["group_id"]
    tokens = _load()
    token = _token_for(group_id, tokens)
    if token is None:
        raise web.HTTPNotFound(text="unknown agent")
    listing = await _vault_request("GET", group_id, token, "/notes")
    notes = listing.get("notes", [])
    open_note = request.query.get("note")
    content = None
    if open_note:
        try:
            note_data = await _vault_request("GET", group_id, token, f"/notes/read?path={quote(open_note)}")
            content = note_data.get("content", "")
        except web.HTTPNotFound:
            content = ""
    return web.Response(
        text=_vault_page(group_id, notes, open_note=open_note, content=content, saved=False),
        content_type="text/html",
    )


async def handle_vault_save(request: web.Request) -> web.Response:
    _require_auth(request)
    group_id = request.match_info["group_id"]
    tokens = _load()
    token = _token_for(group_id, tokens)
    if token is None:
        raise web.HTTPNotFound(text="unknown agent")
    form = await request.post()
    note = str(form.get("note", "")).strip()
    content = str(form.get("content", ""))
    if not note.endswith(".md"):
        raise web.HTTPBadRequest(text="el nombre de la nota debe terminar en .md")
    await _vault_request("PUT", group_id, token, "/notes/write", json={"path": note, "content": content})
    raise web.HTTPFound(f"/agents/{quote(group_id)}/vault?note={quote(note)}")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_post("/create", handle_create)
    app.router.add_post("/delete", handle_delete)
    app.router.add_get("/agents/{group_id}/graph", handle_graph)
    app.router.add_get("/agents/{group_id}/vault", handle_vault)
    app.router.add_post("/agents/{group_id}/vault/save", handle_vault_save)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=int(os.environ.get("ADMIN_PORT", "8090")))
