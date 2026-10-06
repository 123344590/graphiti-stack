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

if [[ ! -f .env ]]; then
  echo "Missing .env — copy .env.example to .env and fill in your LLM/embedder settings first." >&2
  exit 1
fi

if [[ -d "${UPSTREAM_DIR}/.git" ]]; then
  echo "==> Updating existing upstream checkout in ${UPSTREAM_DIR}"
  git -C "${UPSTREAM_DIR}" fetch origin main
  git -C "${UPSTREAM_DIR}" checkout main
  git -C "${UPSTREAM_DIR}" reset --hard origin/main
else
  echo "==> Cloning Graphiti upstream into ${UPSTREAM_DIR}"
  git clone --depth 1 "${UPSTREAM_REPO}" "${UPSTREAM_DIR}"
fi

mkdir -p data/vaults

echo "==> Building images"
docker compose build

echo "==> Starting stack"
docker compose up -d

echo "==> Done. Services:"
docker compose ps
echo
echo "Admin panel: http://<this-host>:\${ADMIN_PORT:-8090}  (see .env for ADMIN_USER/ADMIN_PASSWORD)"
