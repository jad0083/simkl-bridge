#!/usr/bin/env bash
# Smoke-test a built image the ways different users will run it.
#
#   tests/container/smoke.sh [image]      (default: builds simkl-bridge:smoke)
set -euo pipefail
cd "$(dirname "$0")/../.."
IMG="${1:-simkl-bridge:smoke}"
[ $# -ge 1 ] || docker build -q -t "$IMG" . >/dev/null
NET="smoke-$$"
fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "ok   $*"; }
cleanup() { docker rm -f "$NET-simkl" "$NET-bridge" "$NET-ro" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# 1. Runs as the unprivileged user it declares.
[ "$(docker run --rm --entrypoint id "$IMG" -u)" = 10001 ] || fail "not running as uid 10001"
ok "runs as uid 10001"

# 2. Bad configuration fails fast with a sentence, not a traceback.
out=$(docker run --rm "$IMG" 2>&1 || true)
echo "$out" | grep -q "SIMKL_CLIENT_ID is required" || fail "missing client id: $out"
echo "$out" | grep -q Traceback && fail "traceback on missing config"
out=$(docker run --rm -e SIMKL_CLIENT_ID=x -e SIMKL_REFRESH_TOKEN=y -e SONARR_URL=http://sonarr:8989 "$IMG" 2>&1 || true)
echo "$out" | grep -q "SONARR_API_KEY is not" || fail "half arr config accepted: $out"
out=$(docker run --rm -e SIMKL_CLIENT_ID=x -e SIMKL_API_BASE=http://nowhere.invalid "$IMG" auth 2>&1 || true)
echo "$out" | grep -q "could not reach Simkl" || fail "auth with Simkl unreachable: $out"
ok "bad configuration is reported clearly"

# 3. Serves a list from a stand-in Simkl, and goes healthy.
docker network create "$NET" >/dev/null
docker run -d --name "$NET-simkl" --network "$NET" -v "$PWD/tests/support:/support:ro" \
  python:3.13-slim python /support/fake_simkl.py --port 9000 --refresh-token smoke >/dev/null
bridge_env=(-e SIMKL_CLIENT_ID=smoke -e SIMKL_REFRESH_TOKEN=smoke -e SIMKL_API_BASE=http://$NET-simkl:9000)
docker run -d --name "$NET-bridge" --network "$NET" "${bridge_env[@]}" "$IMG" >/dev/null
seed() {
  docker run --rm --network "$NET" curlimages/curl:8.10.1 -sf -X POST -H 'Content-Type: application/json' \
    -d '{"media_type":"tv","items":[{"title":"A","type":"tv","ids":{"simkl_id":1,"tvdb":"121361","imdb":"tt0944947"}}]}' \
    "http://$NET-simkl:9000/_admin/list/42" >/dev/null
}
for _ in $(seq 30); do seed 2>/dev/null && break; sleep 1; done
fetch() { docker run --rm --network "$NET" curlimages/curl:8.10.1 -s "http://$1:8080/sonarr/42"; }
for _ in $(seq 30); do body=$(fetch "$NET-bridge" 2>/dev/null) && [ -n "$body" ] && break; sleep 1; done
echo "$body" | grep -q '"tvdbId": 121361' || fail "list not served: $body"
ok "serves a list through a stand-in Simkl"
for _ in $(seq 90); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$NET-bridge")" = healthy ] && break; sleep 1
done
[ "$(docker inspect -f '{{.State.Health.Status}}' "$NET-bridge")" = healthy ] || fail "never became healthy"
ok "Docker healthcheck passes"

# 4. Works with a read-only root filesystem and only /data writable.
docker run -d --name "$NET-ro" --network "$NET" --read-only --tmpfs /data:uid=10001,mode=0700 \
  --tmpfs /tmp "${bridge_env[@]}" "$IMG" >/dev/null
for _ in $(seq 30); do body=$(fetch "$NET-ro" 2>/dev/null) && [ -n "$body" ] && break; sleep 1; done
echo "$body" | grep -q '"tvdbId": 121361' || fail "read-only root: $body"
docker logs "$NET-ro" 2>&1 | grep -q Traceback && fail "traceback with a read-only root"
ok "runs with a read-only root filesystem"

# 5. No secret reaches the logs.
docker logs "$NET-bridge" 2>&1 | grep -q -E "fake_at_|smoke-refresh|Bearer" && fail "token in logs"
ok "no token in logs"
echo "container smoke: all checks passed"
