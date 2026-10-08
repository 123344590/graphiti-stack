#!/usr/bin/env bash
# Applies the ONE source change this project makes to the vendored,
# otherwise-unmodified FalkorDB Browser checkout (repo-browser/): letting the
# admin panel embed it in an <iframe> on its own graph page.
#
# Upstream sends `frame-ancestors 'none'` on every non-API response via its
# own middleware (proxy.ts) -- a CSP nonce middleware that runs on every
# request and sets its OWN Content-Security-Policy header, which is what the
# browser actually receives. (next.config.js also declares a static
# `X-Frame-Options: DENY` header, but it never reaches the client: the
# middleware's per-request CSP header is what Next.js actually sends, and a
# first attempt at this patch that only touched next.config.js confirmed
# this live -- `curl -I` on the running app showed `frame-ancestors 'none'`
# in the CSP despite that file being patched.)
#
# Rather than deleting the protection, this replaces the hardcoded 'none'
# with a scoped origin list read from CSP_FRAME_ANCESTORS (same env-var
# pattern the file already uses for CSP_CONNECT_SRC) -- set to this
# deployment's own ADMIN_PUBLIC_ORIGIN, so only the admin panel's own origin
# may frame it. Narrower than deleting the directive outright, which would
# let any site embed it.
#
# Idempotent: re-running after a `git reset --hard` (which bootstrap.sh does
# on every update) re-applies cleanly since it matches the pristine upstream
# line, not an already-patched one.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

BROWSER_DIR="repo-browser"
PROXY_FILE="${BROWSER_DIR}/proxy.ts"
ADMIN_PUBLIC_ORIGIN="${ADMIN_PUBLIC_ORIGIN:-http://localhost:${ADMIN_PORT:-8090}}"

if [[ ! -f "${PROXY_FILE}" ]]; then
  echo "Missing ${PROXY_FILE} -- run this after cloning repo-browser, not before." >&2
  exit 1
fi

if grep -q "CSP_FRAME_ANCESTORS" "${PROXY_FILE}"; then
  echo "==> ${PROXY_FILE} already patched for iframe embedding, skipping"
  exit 0
fi

python3 - "$PROXY_FILE" "$ADMIN_PUBLIC_ORIGIN" <<'PYEOF'
import sys

proxy_path, admin_origin = sys.argv[1], sys.argv[2]
with open(proxy_path, "r", encoding="utf-8") as f:
    content = f.read()

old_line = '        "frame-ancestors \'none\'",'
new_line = (
    "        // Patched by scripts/patch-browser-frame-embed.sh: this deployment\n"
    "        // embeds this app in an <iframe> on the admin panel's own\n"
    "        // authenticated graph page. getFrameAncestors() scopes that\n"
    "        // permission to ONLY CSP_FRAME_ANCESTORS (this deployment's admin\n"
    "        // panel origin) -- narrower than 'none' removed outright, which\n"
    "        // would let any site embed this one.\n"
    "        `frame-ancestors ${getFrameAncestors()}`,"
)

if old_line not in content:
    print(f"ERROR: expected frame-ancestors 'none' line not found verbatim in {proxy_path} "
          "-- upstream's proxy.ts has likely changed shape; update this patch.", file=sys.stderr)
    sys.exit(1)

content = content.replace(old_line, new_line, 1)

# Insert the helper right next to the existing getExtraConnectSrc(), whose
# exact same env-var-parsing shape this mirrors.
anchor = "function getExtraConnectSrc(): string[] {"
if anchor not in content:
    print(f"ERROR: getExtraConnectSrc() anchor not found in {proxy_path} -- update this patch.", file=sys.stderr)
    sys.exit(1)

helper = f'''function getFrameAncestors(): string {{
    const raw = process.env.CSP_FRAME_ANCESTORS;
    if (!raw) return "'none'";
    return raw
        .split(",")
        .map(origin => origin.trim())
        .filter(Boolean)
        .join(" ");
}}

{anchor}'''
content = content.replace(anchor, helper, 1)

with open(proxy_path, "w", encoding="utf-8") as f:
    f.write(content)
print(f"==> Patched {proxy_path}: frame-ancestors 'none' -> dynamic from CSP_FRAME_ANCESTORS "
      f"(set to {admin_origin} in docker-compose.yml)")
PYEOF
