#!/usr/bin/env bash
# Start an image with synthetic configuration and check its HTTP contract:
# anonymous /healthz, bearer auth on /mcp, and a read-only non-root runtime.
set -euo pipefail

image=${1:?usage: check_container.sh IMAGE}
name=health-mcp-check-$$
container=
cleanup() {
  if [ -n "$container" ]; then
    docker rm --force "$container" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

container=$(
  docker run --detach --name "$name" --network none --read-only \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,noexec,nosuid \
    --env CACHE_DIR=/tmp/cache \
    --env PROFILE_DIR=/tmp/profile \
    --env MCP_TRANSPORT=http \
    --env MCP_BEARER_TOKEN=synthetic-ci-token-never-used-for-runtime \
    --env OW_API_KEY=synthetic-ci-key \
    --env OW_USER_ID=synthetic-ci-user \
    --env OW_BASE_URL=http://127.0.0.1:9 \
    "$image"
)

attempts=60
until docker exec "$container" python -c "
import urllib.request
urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).read()
" >/dev/null 2>&1; do
  attempts=$((attempts - 1))
  if [ "$attempts" -le 0 ]; then
    echo "Container did not become healthy; logs follow." >&2
    docker logs "$container" >&2 || true
    exit 1
  fi
  sleep 1
done

docker exec --interactive "$container" python - <<'PY'
import sys
import urllib.error
import urllib.request

token = "synthetic-ci-token-never-used-for-runtime"


def status(path, *, token=None, method="GET"):
    request = urllib.request.Request(f"http://127.0.0.1:8000{path}", method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


expected = {
    "anonymous /healthz": (status("/healthz"), 200),
    "/mcp without a token": (status("/mcp"), 401),
    "/mcp with a wrong token": (status("/mcp", token="wrong-token"), 401),
    "POST /healthz": (status("/healthz", method="POST"), 405),
}
for name, (actual, wanted) in expected.items():
    if actual != wanted:
        print(f"FAIL: {name} returned {actual}, wanted {wanted}", file=sys.stderr)
        sys.exit(1)

authenticated = status("/mcp", token=token)
if authenticated == 401:
    print("FAIL: /mcp rejected the configured bearer token", file=sys.stderr)
    sys.exit(1)

print(f"Container contract OK (authenticated /mcp returned {authenticated}).")
PY
