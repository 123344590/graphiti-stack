#!/usr/bin/env bash
# Applies the ONE source change this project makes to the vendored,
# otherwise-unmodified FalkorDB Browser checkout (repo-browser/): letting the
# admin panel embed it in an <iframe> on its own graph page.
#
# Upstream hardcodes `X-Frame-Options: DENY` in next.config.js with no env
# var to control it -- reasonable as a library default (clickjacking
# protection), wrong for this specific deployment where the admin panel
# embedding it on the SAME origin's own authenticated page is the whole
# point. Rather than deleting the protection, this replaces DENY with a
# scoped Content-Security-Policy `frame-ancestors` header that allows only
# ADMIN_PUBLIC_ORIGIN (the admin panel's own origin) to frame it --
# strictly narrower than removing the header outright, which would let ANY
# site embed it.
#
# Idempotent: re-running after a `git reset --hard` (which bootstrap.sh does
# on every update) re-applies cleanly since it matches the pristine upstream
# line, not a already-patched one.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

BROWSER_DIR="repo-browser"
CONFIG_FILE="${BROWSER_DIR}/next.config.js"
ADMIN_PUBLIC_ORIGIN="${ADMIN_PUBLIC_ORIGIN:-http://localhost:${ADMIN_PORT:-8090}}"

if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "Missing ${CONFIG_FILE} -- run this after cloning repo-browser, not before." >&2
  exit 1
fi

if grep -q "frame-ancestors" "${CONFIG_FILE}"; then
  echo "==> ${CONFIG_FILE} already patched for iframe embedding, skipping"
  exit 0
fi

python3 - "$CONFIG_FILE" "$ADMIN_PUBLIC_ORIGIN" <<'PYEOF'
import re
import sys

config_path, admin_origin = sys.argv[1], sys.argv[2]
with open(config_path, "r", encoding="utf-8") as f:
    content = f.read()

old_block = """          {
            key: 'X-Frame-Options',
            value: 'DENY'
          },"""
new_block = f"""          {{
            // Patched by scripts/patch-browser-frame-embed.sh: upstream's
            // DENY has no env-var override and this deployment embeds this
            // app in an <iframe> on the admin panel's own authenticated
            // graph page. frame-ancestors scopes that permission to ONLY
            // the admin panel's own origin -- narrower than deleting the
            // header, which would let any site embed this one.
            key: 'Content-Security-Policy',
            value: "frame-ancestors 'self' {admin_origin}"
          }},"""

if old_block not in content:
    print(f"ERROR: expected X-Frame-Options block not found verbatim in {config_path} "
          "-- upstream's next.config.js has likely changed shape; update this patch.", file=sys.stderr)
    sys.exit(1)

content = content.replace(old_block, new_block, 1)
with open(config_path, "w", encoding="utf-8") as f:
    f.write(content)
print(f"==> Patched {config_path}: X-Frame-Options: DENY -> Content-Security-Policy: frame-ancestors 'self' {admin_origin}")
PYEOF
