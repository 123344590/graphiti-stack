#!/usr/bin/env python3
"""Admin panel for the graphiti-stack deployment.

One page to: create/revoke per-agent tokens (shared by the Graphiti auth-proxy
and the vault service — same token, same group_id, one identity per agent),
view each agent's knowledge graph (embedded FalkorDB Browser iframe, scoped to
that agent's own graph key so operators never land on the shared default graph
by accident), and browse/edit each agent's Obsidian vault (list notes, read,
edit, save) via the vault service's HTTP API.

Single operator account, authenticated through an actual login page (not the
browser's native HTTP-Basic prompt) — a signed, httpOnly session cookie backs
it: the login form posts credentials once, the server issues a signed token
(username + expiry + HMAC over both, keyed by a per-process random secret) and
every later request is authenticated by verifying that signature, never by
re-sending the password. Infrastructure administration for one operator, not a
multi-tenant app — this is deliberately simpler than a real user/session store.

Writes directly to the same agent_tokens.json the auth-proxy and vault service
both read. Every one of those three processes re-reads the file per request
(cheap: a few KB of JSON next to work that already does a network hop), so a
token created/revoked here takes effect everywhere on the very next request —
no restart, no signal, no coordination beyond the shared bind-mounted file.
"""
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiohttp import web

TOKENS_PATH = Path(os.environ.get("AGENT_TOKENS_PATH", "/data/agent_tokens.json"))
ADMIN_USER = os.environ["ADMIN_USER"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
FALKORDB_BROWSER_BASE = os.environ.get("FALKORDB_BROWSER_BASE", "http://10.147.200.5:3000")
VAULT_SERVICE_BASE = os.environ.get("VAULT_SERVICE_BASE", "http://vault-service:8070")

# Generated fresh on every process start -- every existing session cookie is
# invalidated on a restart/redeploy, which is the simplest correct behavior
# for a single-operator panel with no persistent session store to manage.
_SESSION_SECRET = secrets.token_bytes(32)
_SESSION_COOKIE = "graphiti_admin_session"
_SESSION_TTL_SECONDS = 7 * 24 * 3600


def _sign(payload: str) -> str:
    return hmac.new(_SESSION_SECRET, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _make_session_cookie() -> str:
    expires_at = int(time.time()) + _SESSION_TTL_SECONDS
    payload = f"{ADMIN_USER}:{expires_at}"
    return f"{payload}:{_sign(payload)}"


def _session_is_valid(cookie_value: str | None) -> bool:
    if not cookie_value or cookie_value.count(":") != 2:
        return False
    user, expires_at, signature = cookie_value.split(":")
    payload = f"{user}:{expires_at}"
    if not hmac.compare_digest(signature, _sign(payload)):
        return False
    if not expires_at.isdigit() or int(expires_at) < time.time():
        return False
    return hmac.compare_digest(user, ADMIN_USER)


def _require_auth(request: web.Request) -> None:
    if not _session_is_valid(request.cookies.get(_SESSION_COOKIE)):
        raise web.HTTPFound("/login")


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


def _layout(body: str, *, authenticated: bool = True) -> str:
    nav = '<div class="nav"><a href="/">Agentes</a><a href="/logout">Cerrar sesión</a></div>' if authenticated else ""
    return f"""<!DOCTYPE html>
<html><head><title>Graphiti Admin</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {{
  --bg: #f7f8fa; --surface: #ffffff; --border: #e2e5ea; --text: #1a1d23; --muted: #6b7280;
  --accent: #4f46e5; --accent-hover: #4338ca; --danger: #dc2626; --ok: #15803d;
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  max-width: 980px; margin: 0 auto; padding: 32px 20px 64px; background: var(--bg); color: var(--text);
}}
h1 {{ font-size: 1.5rem; margin: 0 0 4px; }}
h2, h3 {{ color: var(--text); }}
.nav {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 28px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }}
.nav a {{ color: var(--accent); text-decoration: none; font-weight: 500; }}
.nav a:hover {{ text-decoration: underline; }}
.card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 24px; box-shadow: 0 1px 2px rgba(16,24,40,0.04); }}
.card + .card {{ margin-top: 20px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
td, th {{ border-bottom: 1px solid var(--border); padding: 10px 8px; text-align: left; vertical-align: middle; font-size: 0.92rem; }}
th {{ color: var(--muted); font-weight: 600; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.03em; }}
tr:last-child td {{ border-bottom: none; }}
input[type=text], input[type=password], textarea {{
  padding: 10px 12px; font-family: inherit; font-size: 0.95rem; border: 1px solid var(--border);
  border-radius: 8px; background: var(--surface); color: var(--text); outline: none;
}}
input[type=text]:focus, input[type=password]:focus, textarea:focus {{ border-color: var(--accent); box-shadow: 0 0 0 3px rgba(79,70,229,0.12); }}
input[type=text] {{ width: 260px; }}
textarea {{ width: 100%; min-height: 320px; font-family: ui-monospace, "SF Mono", monospace; font-size: 0.88rem; }}
button {{
  padding: 10px 18px; cursor: pointer; border: none; border-radius: 8px; background: var(--accent);
  color: white; font-weight: 600; font-size: 0.9rem; transition: background 0.15s;
}}
button:hover {{ background: var(--accent-hover); }}
button.danger {{ background: transparent; color: var(--danger); border: 1px solid #fecaca; }}
button.danger:hover {{ background: #fef2f2; }}
a.link-btn {{ color: var(--accent); text-decoration: none; font-weight: 500; font-size: 0.9rem; }}
a.link-btn:hover {{ text-decoration: underline; }}
.banner {{ background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px; padding: 14px 16px; margin-bottom: 20px; }}
iframe {{ width: 100%; height: 640px; border: 1px solid var(--border); border-radius: 8px; margin-top: 12px; }}
ul.notes {{ list-style: none; padding: 0; margin: 0; }}
ul.notes li {{ padding: 8px 0; border-bottom: 1px solid var(--border); }}
ul.notes li:last-child {{ border-bottom: none; }}
.muted {{ color: var(--muted); font-size: 0.88rem; }}
.row {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
.split {{ display: flex; gap: 24px; align-items: flex-start; }}
.split > div:first-child {{ flex: 1; min-width: 220px; }}
.split > div:last-child {{ flex: 2; min-width: 300px; }}
.login-wrap {{ display: flex; align-items: center; justify-content: center; min-height: 80vh; }}
.login-card {{ width: 100%; max-width: 360px; }}
.login-card h1 {{ text-align: center; margin-bottom: 24px; }}
.login-card form {{ display: flex; flex-direction: column; gap: 14px; }}
.login-card button {{ margin-top: 4px; }}
.error {{ color: var(--danger); font-size: 0.88rem; margin: 0; }}
.tab-btn {{ background: transparent; color: var(--muted); border: 1px solid var(--border); padding: 6px 14px; font-weight: 500; }}
.tab-btn:hover {{ background: var(--bg); }}
.tab-btn.active {{ background: var(--accent); color: white; border-color: var(--accent); }}
.md-preview {{ border: 1px solid var(--border); border-radius: 8px; padding: 16px 20px; min-height: 320px; line-height: 1.6; }}
.md-preview h1, .md-preview h2, .md-preview h3 {{ margin-top: 0.6em; }}
.md-preview a {{ color: var(--accent); }}
.md-preview code {{ background: var(--bg); padding: 2px 5px; border-radius: 4px; font-size: 0.9em; }}
.md-preview pre {{ background: var(--bg); padding: 12px; border-radius: 8px; overflow-x: auto; }}
ul.vault-explorer li {{ padding: 0; border-bottom: none; }}
ul.vault-explorer li a {{
  display: flex; align-items: center; gap: 8px; padding: 8px 20px; text-decoration: none;
  color: var(--text); font-size: 0.92rem; border-left: 3px solid transparent;
}}
ul.vault-explorer li a:hover {{ background: var(--bg); }}
ul.vault-explorer li.active a {{ background: #eef2ff; border-left-color: var(--accent); font-weight: 600; color: var(--accent); }}
.note-icon {{ font-size: 0.95em; opacity: 0.7; }}
</style></head>
<body>
{nav}
{body}
</body></html>"""


def _login_page(*, error: bool = False) -> str:
    error_html = '<p class="error">Usuario o contraseña incorrectos.</p>' if error else ""
    return _layout(f"""
<div class="login-wrap">
  <div class="login-card card">
    <h1>Graphiti Admin</h1>
    {error_html}
    <form method="post" action="/login">
      <input type="text" name="username" placeholder="Usuario" autocomplete="username" required autofocus>
      <input type="password" name="password" placeholder="Contraseña" autocomplete="current-password" required>
      <button type="submit">Entrar</button>
    </form>
  </div>
</div>
""", authenticated=False)


def _agents_page(
    tokens: dict[str, str], *, created: tuple[str, str] | None = None, error: str | None = None,
) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(group_id)}</td>"
        f"<td><a class=\"link-btn\" href=\"/agents/{quote(group_id)}/graph\">ver grafo</a></td>"
        f"<td><a class=\"link-btn\" href=\"/agents/{quote(group_id)}/vault\">ver vault</a></td>"
        f"<td><form method=\"post\" action=\"/delete\" style=\"display:inline\">"
        f"<input type=\"hidden\" name=\"group_id\" value=\"{html.escape(group_id)}\">"
        f"<button type=\"submit\" class=\"danger\" onclick=\"return confirm('Revocar el token de {html.escape(group_id)}?')\">revocar</button>"
        f"</form></td></tr>"
        for group_id in sorted(tokens.values())
    )
    if not rows:
        rows = '<tr><td colspan="4" class="muted">Sin agentes todavía — crea el primero arriba.</td></tr>'
    banner = ""
    if created:
        group_id, token = created
        banner = (
            f"<div class=\"banner\"><b>Token para \"{html.escape(group_id)}\" "
            f"— cópialo ahora, no se volverá a mostrar:</b><br>"
            f"<code style=\"font-size:1.05em\">{html.escape(token)}</code></div>"
        )
    elif error:
        banner = f'<div class="banner" style="background:#fef2f2;border-color:#fecaca"><b>{html.escape(error)}</b></div>'
    return _layout(f"""
<h1>Graphiti — Agentes</h1>
<p class="muted">Cada agente tiene su propio grafo de memoria y su propio vault de Obsidian, aislados estructuralmente.</p>
{banner}
<div class="card">
  <form method="post" action="/create" class="row">
    <input type="text" name="group_id" placeholder="nombre del agente (ej. miguel)" required pattern="[a-zA-Z0-9_-]+">
    <button type="submit">Crear agente + token</button>
  </form>
</div>
<div class="card">
  <table>
  <thead><tr><th>Agente</th><th>Grafo</th><th>Vault</th><th></th></tr></thead>
  <tbody>{rows}</tbody>
  </table>
</div>""")


def _graph_page(group_id: str) -> str:
    browser_url = f"{FALKORDB_BROWSER_BASE}/?graph={quote(group_id)}"
    return _layout(f"""
<h1>Grafo de "{html.escape(group_id)}"</h1>
<div class="card">
<p class="muted">FalkorDB Browser no permite ser embebido dentro de otra página
(bloqueo propio del servidor vía <code>X-Frame-Options</code>, no algo que este
panel pueda desactivar) — se abre en pestaña nueva, pre-seleccionado en el
grafo de este agente.</p>
<p><a class="link-btn" href="{browser_url}" target="_blank" rel="noopener">
Abrir grafo de "{html.escape(group_id)}" en FalkorDB Browser →</a></p>
<p class="muted">Si ahí pide conexión manual, host/puerto son los del propio
FalkorDB en este stack (ver <code>docs/DEPLOY.md</code>) — el <code>graph</code>
en la URL ya fija la base de datos a <code>{html.escape(group_id)}</code>, no al
grafo compartido <code>main</code>.</p>
</div>
""")


async def _vault_request(method: str, group_id: str, token: str, path: str, **kwargs):
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.request(method, f"{VAULT_SERVICE_BASE}{path}", headers=headers, **kwargs) as resp:
            if resp.status == 404:
                raise web.HTTPNotFound(text=await resp.text())
            if resp.status >= 400:
                raise web.HTTPBadGateway(text=f"vault-service error {resp.status}: {await resp.text()}")
            return await resp.json()


def _note_display_name(filename: str) -> str:
    return filename[:-3] if filename.endswith(".md") else filename


def _vault_page(group_id: str, notes: list[str], *, open_note: str | None, content: str | None, saved: bool) -> str:
    note_items = "".join(
        f"<li class=\"{'active' if n == open_note else ''}\">"
        f"<a href=\"/agents/{quote(group_id)}/vault?note={quote(n)}\">"
        f"<span class=\"note-icon\">📄</span>{html.escape(_note_display_name(n))}</a></li>"
        for n in notes
    ) or '<li class="muted" style="padding:8px 12px">(sin notas aún — crea la primera abajo)</li>'
    editor = ""
    if open_note is not None:
        note_exists = open_note in notes
        saved_banner = '<p style="color:var(--ok)">Guardado.</p>' if saved else ""
        note_id = "note-editor"
        default_mode = "preview" if (note_exists and content) else "edit"
        editor = f"""
<div class="card">
<div class="row" style="justify-content:space-between">
  <h3 style="margin:0">{html.escape(_note_display_name(open_note))}{'' if note_exists else ' <span class="muted" style="font-weight:400">(nota nueva)</span>'}</h3>
  <div class="row">
    <button type="button" class="tab-btn" data-mode="edit" onclick="setVaultMode('edit')">Editar</button>
    <button type="button" class="tab-btn" data-mode="preview" onclick="setVaultMode('preview')">Vista previa</button>
  </div>
</div>
{saved_banner}
<form method="post" action="/agents/{quote(group_id)}/vault/save">
  <input type="hidden" name="note" value="{html.escape(open_note)}">
  <textarea id="{note_id}" name="content" oninput="renderVaultPreview()">{html.escape(content or "")}</textarea>
  <div id="{note_id}-preview" class="md-preview" style="display:none"></div>
  <br>
  <button type="submit">Guardar</button>
</form>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<script>
function vaultNoteHref(name) {{
  return "/agents/{quote(group_id)}/vault?note=" + encodeURIComponent(name.endsWith(".md") ? name : name + ".md");
}}
function renderVaultPreview() {{
  const src = document.getElementById("{note_id}").value;
  const withLinks = src.replace(/\\[\\[([^\\]|]+)(\\|[^\\]]+)?\\]\\]/g, (m, target, label) => {{
    const text = label ? label.slice(1) : target;
    return "[" + text + "](" + vaultNoteHref(target.trim()) + ")";
  }});
  document.getElementById("{note_id}-preview").innerHTML = marked.parse(withLinks);
}}
function setVaultMode(mode) {{
  const isEdit = mode === "edit";
  document.getElementById("{note_id}").style.display = isEdit ? "block" : "none";
  document.getElementById("{note_id}-preview").style.display = isEdit ? "none" : "block";
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.mode === mode));
  if (!isEdit) renderVaultPreview();
}}
setVaultMode("{default_mode}");
</script>"""
    return _layout(f"""
<h1>Vault de "{html.escape(group_id)}"</h1>
<div class="split">
  <div>
    <div class="card" style="padding:12px 0">
      <h3 style="padding:0 20px">Notas</h3>
      <ul class="notes vault-explorer">{note_items}</ul>
    </div>
    <div class="card">
      <h3>Nueva nota</h3>
      <form method="post" action="/agents/{quote(group_id)}/vault/save">
        <input type="text" name="note" placeholder="nombre.md" required pattern=".+\\.md"><br><br>
        <textarea name="content" placeholder="Contenido markdown..."></textarea><br><br>
        <button type="submit">Crear nota</button>
      </form>
    </div>
  </div>
  <div>{editor or '<div class="card muted">Selecciona una nota de la izquierda, o crea una nueva.</div>'}</div>
</div>
""")


async def handle_login_page(request: web.Request) -> web.Response:
    if _session_is_valid(request.cookies.get(_SESSION_COOKIE)):
        raise web.HTTPFound("/")
    return web.Response(text=_login_page(), content_type="text/html")


async def handle_login_submit(request: web.Request) -> web.Response:
    form = await request.post()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    if not (secrets.compare_digest(username, ADMIN_USER) and secrets.compare_digest(password, ADMIN_PASSWORD)):
        return web.Response(text=_login_page(error=True), content_type="text/html", status=401)
    response = web.HTTPFound("/")
    response.set_cookie(
        _SESSION_COOKIE, _make_session_cookie(),
        max_age=_SESSION_TTL_SECONDS, httponly=True, samesite="Strict",
    )
    raise response


async def handle_logout(request: web.Request) -> web.Response:
    response = web.HTTPFound("/login")
    response.del_cookie(_SESSION_COOKIE)
    raise response


async def handle_index(request: web.Request) -> web.Response:
    _require_auth(request)
    return web.Response(text=_agents_page(_load()), content_type="text/html")


async def handle_create(request: web.Request) -> web.Response:
    _require_auth(request)
    form = await request.post()
    group_id = str(form.get("group_id", "")).strip()
    if not group_id or not group_id.replace("_", "").replace("-", "").isalnum():
        tokens = _load()
        return web.Response(
            text=_agents_page(tokens, error="Nombre de agente inválido — usa solo letras, números, guiones y guion bajo."),
            content_type="text/html", status=400,
        )
    tokens = _load()
    if group_id in tokens.values():
        return web.Response(
            text=_agents_page(tokens, error=f'El agente "{group_id}" ya existe — revócalo primero si quieres un token nuevo.'),
            content_type="text/html", status=400,
        )
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
    app.router.add_get("/login", handle_login_page)
    app.router.add_post("/login", handle_login_submit)
    app.router.add_get("/logout", handle_logout)
    app.router.add_get("/", handle_index)
    app.router.add_post("/create", handle_create)
    app.router.add_post("/delete", handle_delete)
    app.router.add_get("/agents/{group_id}/graph", handle_graph)
    app.router.add_get("/agents/{group_id}/vault", handle_vault)
    app.router.add_post("/agents/{group_id}/vault/save", handle_vault_save)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=int(os.environ.get("ADMIN_PORT", "8090")))
