#!/usr/bin/env bash
# End-to-end smoke test against a locally running stack. Not part of the
# deploy path -- a throwaway verification script for before/after a change,
# run manually: ./scripts/e2e_test.sh (expects `docker compose up -d` already
# done with a real .env in this directory).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

source .env
BASE="http://localhost:${ADMIN_PORT:-8090}"
PROXY="http://localhost:${PROXY_PORT:-8080}"
COOKIES=$(mktemp)
FAIL=0

pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1"; FAIL=1; }
extract_token() { perl -ne 'print "$1\n" if /code style="font-size:1\.05em">([^<]+)/'; }

echo "=== login flow ==="
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/")
[[ "$CODE" == "302" ]] && pass "unauthenticated GET / redirects (302)" || fail "unauthenticated GET / got $CODE, expected 302"

CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/login" -d "username=wrong&password=wrong")
[[ "$CODE" == "401" ]] && pass "wrong credentials return 401" || fail "wrong credentials got $CODE, expected 401"

curl -s -X POST "$BASE/login" -d "username=${ADMIN_USER}&password=${ADMIN_PASSWORD}" -c "$COOKIES" -o /dev/null
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/" -b "$COOKIES")
[[ "$CODE" == "200" ]] && pass "correct credentials + cookie -> 200" || fail "authenticated GET / got $CODE, expected 200"

# Idempotency: a previous (possibly failed) run of this script may have left
# e2e_miguel/e2e_ana behind, which would make the "create" calls below hit
# the duplicate-agent branch instead of actually creating anything.
curl -s -X POST "$BASE/delete" -d "group_id=e2e_miguel" -b "$COOKIES" -o /dev/null
curl -s -X POST "$BASE/delete" -d "group_id=e2e_ana" -b "$COOKIES" -o /dev/null

echo "=== agent creation ==="
RESP=$(curl -s -X POST "$BASE/create" -d "group_id=e2e_miguel" -b "$COOKIES")
TOKEN_MIGUEL=$(echo "$RESP" | extract_token)
[[ -n "$TOKEN_MIGUEL" ]] && pass "create e2e_miguel returns a token" || fail "no token in create response"

RESP2=$(curl -s -X POST "$BASE/create" -d "group_id=e2e_miguel" -b "$COOKIES")
echo "$RESP2" | grep -q 'ya existe' && pass "duplicate create shows inline error" || fail "duplicate create didn't show expected error"
echo "$RESP2" | grep -q '<html>' && pass "duplicate create still renders full page" || fail "duplicate create didn't render full page"

RESP3=$(curl -s -X POST "$BASE/create" -d "group_id=e2e_ana" -b "$COOKIES")
TOKEN_ANA=$(echo "$RESP3" | extract_token)
[[ -n "$TOKEN_ANA" ]] && pass "create e2e_ana returns a token" || fail "no token for e2e_ana"

echo "=== graph page has no iframe, has target=_blank link ==="
GRAPH_HTML=$(curl -s "$BASE/agents/e2e_miguel/graph" -b "$COOKIES")
echo "$GRAPH_HTML" | grep -q '<iframe' && fail "graph page still has an iframe" || pass "graph page has no iframe"
echo "$GRAPH_HTML" | grep -q 'target="_blank"' && pass "graph page has a new-tab link" || fail "graph page missing target=_blank link"

echo "=== vault: cross-agent isolation ==="
docker compose exec -T admin python3 -c "
import urllib.request, json
data = json.dumps({'path': 'secreto.md', 'content': 'solo miguel'}).encode()
req = urllib.request.Request('http://vault-service:8070/notes/write', data=data, method='PUT',
    headers={'Authorization': 'Bearer ${TOKEN_MIGUEL}', 'Content-Type': 'application/json'})
urllib.request.urlopen(req).read()
" > /dev/null

ANA_NOTES=$(docker compose exec -T admin python3 -c "
import urllib.request
req = urllib.request.Request('http://vault-service:8070/notes', headers={'Authorization': 'Bearer ${TOKEN_ANA}'})
print(urllib.request.urlopen(req).read().decode())
")
echo "$ANA_NOTES" | grep -q 'secreto' && fail "ana's token can see miguel's note!" || pass "ana's vault listing does not contain miguel's note"

ANA_READ_CODE=$(docker compose exec -T admin python3 -c "
import urllib.request, urllib.error
req = urllib.request.Request('http://vault-service:8070/notes/read?path=secreto.md', headers={'Authorization': 'Bearer ${TOKEN_ANA}'})
try:
    urllib.request.urlopen(req)
    print(200)
except urllib.error.HTTPError as e:
    print(e.code)
")
[[ "$ANA_READ_CODE" == "404" ]] && pass "ana reading miguel's note path returns 404" || fail "ana reading miguel's note path returned $ANA_READ_CODE, expected 404"

echo "=== vault: wikilink to nonexistent note opens empty editor (no 502) ==="
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/agents/e2e_miguel/vault?note=NoExiste.md" -b "$COOKIES")
[[ "$CODE" == "200" ]] && pass "opening nonexistent note returns 200" || fail "opening nonexistent note returned $CODE, expected 200"

echo "=== graphiti: group_id enforced even when the caller omits it ==="
INIT=$(curl -s -D- -X POST "$PROXY/mcp/" \
  -H "Authorization: Bearer ${TOKEN_ANA}" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"e2e","version":"1"}}}')
SESSION=$(echo "$INIT" | perl -ne 'print "$1\n" if /mcp-session-id: ([a-f0-9]+)/')
[[ -n "$SESSION" ]] && pass "MCP initialize handshake succeeds through the proxy" || fail "no mcp-session-id from initialize"

curl -s -X POST "$PROXY/mcp/" \
  -H "Authorization: Bearer ${TOKEN_ANA}" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -H "mcp-session-id: ${SESSION}" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"add_memory","arguments":{"name":"e2e","episode_body":"ana forgets group_id"}}}' \
  | grep -q "group 'e2e_ana'" && pass "add_memory without group_id lands in caller's own group" || fail "add_memory did not land in e2e_ana's group"

echo "=== unauthorized token is rejected by the proxy ==="
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$PROXY/mcp/" -H "Authorization: Bearer not-a-real-token" -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}')
[[ "$CODE" == "401" ]] && pass "invalid token rejected with 401" || fail "invalid token got $CODE, expected 401"

rm -f "$COOKIES"
echo
if [[ "$FAIL" == "0" ]]; then
  echo "ALL CHECKS PASSED"
else
  echo "SOME CHECKS FAILED"
  exit 1
fi
