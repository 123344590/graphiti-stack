#!/usr/bin/env bash
# Bootstrap the graphiti-stack deployment on a fresh host with Docker installed.
#
# Mirrors the hermes-agent -> manty deploy pattern: clone/update a vendored
# upstream checkout, build with a pinned override, docker compose up. Safe to
# re-run — it updates the upstream checkout in place and recreates containers.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

UPSTREAM_REPO="https://github.com/getzep/graphiti.git"
UPSTREAM_DIR="repo"
BROWSER_REPO="https://github.com/FalkorDB/falkordb-browser.git"
BROWSER_DIR="repo-browser"

if [[ ! -f .env ]]; then
  echo "Missing .env — copy .env.example to .env and fill in your LLM/embedder settings first." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a
export ADMIN_PUBLIC_ORIGIN="http://${PUBLIC_HOST:-localhost}:${ADMIN_PORT:-8090}"

_clone_or_update() {
  local repo="$1" dir="$2" label="$3"
  if [[ -d "${dir}/.git" ]]; then
    echo "==> Updating existing ${label} checkout in ${dir}"
    git -C "${dir}" fetch origin main
    git -C "${dir}" checkout main
    git -C "${dir}" reset --hard origin/main
  else
    echo "==> Cloning ${label} into ${dir}"
    git clone --depth 1 "${repo}" "${dir}"
  fi
}

_clone_or_update "${UPSTREAM_REPO}" "${UPSTREAM_DIR}" "Graphiti upstream"
# Official FalkorDB Browser — vendored with exactly one source patch (see
# scripts/patch-browser-frame-embed.sh and docs/ARCHITECTURE.md for why);
# auto-connect and its own ?graph=<name> share-link param are both used
# as-is, with zero source changes, for everything else.
_clone_or_update "${BROWSER_REPO}" "${BROWSER_DIR}" "FalkorDB Browser"
./scripts/patch-browser-frame-embed.sh

mkdir -p data/vaults

echo "==> Building images"
docker compose build

echo "==> Starting stack"
docker compose up -d

echo "==> Done. Services:"
docker compose ps
echo
echo "Admin panel: http://<this-host>:\${ADMIN_PORT:-8090}  (see .env for ADMIN_USER/ADMIN_PASSWORD)"
