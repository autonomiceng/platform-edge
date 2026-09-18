#!/bin/sh
# Smoke Contract: disposable edge and three independently stoppable upstreams.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
for tool in docker python3 curl; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done
export COMPOSE_PROJECT_NAME="${SMOKE_PROJECT:-platform-edge-smoke}"
[ "$COMPOSE_PROJECT_NAME" != platform-edge ] || { echo 'refusing the installed project' >&2; exit 2; }
# Export every setting so an operator shell cannot select production endpoints.
integration=${SMOKE_INTEGRATION:-0}
if [ "$integration" = 1 ]; then
  export PE_PUBLIC_DOMAIN="${SMOKE_DOMAIN:-${PE_PUBLIC_DOMAIN:-localhost}}"
  export PE_PLATFORM_NETWORK="${PE_PLATFORM_NETWORK:-platform}"
else
  export PE_PUBLIC_DOMAIN=localhost PE_PLATFORM_NETWORK="$COMPOSE_PROJECT_NAME-platform"
fi
export PE_SCHEME=http PE_TLS_ISSUER=none PE_ACME_EMAIL='' PE_BACKUP_KEEP=7
export PE_METRICS_ALLOW="127.0.0.0/8 ::1"
export PE_VOLUME_PREFIX="$COMPOSE_PROJECT_NAME" PE_BACKUP_DIR=/tmp/unused-edge-smoke-backups
export PE_BIND_HOST=127.0.0.1 PE_HTTP_PORT="${SMOKE_HTTP_PORT:-18280}" PE_HTTPS_PORT="${SMOKE_HTTPS_PORT:-18643}"
unset COMPOSE_FILE COMPOSE_PROFILES
work=$(mktemp -d)
env_file="$work/.env"
pass=0
lg_stub='' bp_stub='' ob_stub='' network_created='' edge_started=''
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok: $*"; pass=$((pass + 1)); }
cleanup() {
  if [ -n "$edge_started" ]; then
    docker compose --env-file "$env_file" down --remove-orphans >/dev/null 2>&1 || true
    docker volume rm "${PE_VOLUME_PREFIX}_edge-data" "${PE_VOLUME_PREFIX}_edge-config" >/dev/null 2>&1 || true
  fi
  for stub in "$lg_stub" "$bp_stub" "$ob_stub"; do
    if [ -n "$stub" ]; then docker rm -f -v "$stub" >/dev/null 2>&1 || true; fi
  done
  if [ -n "$network_created" ]; then docker network rm "$PE_PLATFORM_NETWORK" >/dev/null 2>&1 || true; fi
  rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
# Refuse existing smoke resources rather than adopting or deleting them.
existing=$(docker ps -aq --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME")
[ -z "$existing" ] || fail "project $COMPOSE_PROJECT_NAME already has containers"
existing=$(docker volume ls -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME")
[ -z "$existing" ] || fail "project $COMPOSE_PROJECT_NAME already has volumes"
for volume in "${PE_VOLUME_PREFIX}_edge-data" "${PE_VOLUME_PREFIX}_edge-config"; do
  if docker volume inspect "$volume" >/dev/null 2>&1; then fail "volume $volume already exists"; fi
done
if [ "$integration" = 1 ]; then
  result=0
  python3 scripts/integration_smoke.py --check-network || result=$?
  if [ "$result" = 77 ]; then exit 0; fi
  [ "$result" = 0 ] || exit "$result"
else
  docker network create "$PE_PLATFORM_NETWORK" >/dev/null
  network_created=1
fi
if [ "$integration" = 1 ]; then
  # Do not shadow the installed edge's shared network alias during acceptance.
  cat > "$work/integration.yaml" <<'YAML'
services:
  caddy:
    networks:
      platform:
        aliases: !override [pe-integration-smoke]
YAML
  export COMPOSE_FILE="$root/compose.yaml:$work/integration.yaml"
fi
PE_METRICS_ALLOW="$PE_METRICS_ALLOW $(docker network inspect "$PE_PLATFORM_NETWORK" --format '{{range .IPAM.Config}}{{.Gateway}} {{end}}')"
export PE_METRICS_ALLOW
python3 scripts/bootstrap.py --env-file "$env_file" --render-only >/dev/null
caddy_image=$(docker compose --env-file "$env_file" config --images)
if [ "$integration" != 1 ]; then
cat > "$work/stub.caddy" <<'CADDY'
{
	admin off
}
:80, :3000 {
	respond "{$STUB_ALIAS}|{host}|{http.request.header.X-Forwarded-Proto}"
}
CADDY
lg_stub=$(docker run -d --network "$PE_PLATFORM_NETWORK" --network-alias lg-gateway -e STUB_ALIAS=lg-gateway \
  -v "$work/stub.caddy:/etc/caddy/Caddyfile:ro" "$caddy_image")
bp_stub=$(docker run -d --network "$PE_PLATFORM_NETWORK" --network-alias bp-server -e STUB_ALIAS=bp-server \
  -v "$work/stub.caddy:/etc/caddy/Caddyfile:ro" "$caddy_image")
ob_stub=$(docker run -d --network "$PE_PLATFORM_NETWORK" --network-alias ob-gateway -e STUB_ALIAS=ob-gateway \
  -v "$work/stub.caddy:/etc/caddy/Caddyfile:ro" "$caddy_image")
fi
edge_started=1
python3 scripts/bootstrap.py --env-file "$env_file"
ok 'bootstrap reached readiness'

for scheme in http https; do
  if [ "$scheme" = https ]; then
    export PE_SCHEME=https PE_TLS_ISSUER=internal
    python3 scripts/bootstrap.py --env-file "$env_file"
    docker compose --env-file "$env_file" cp caddy:/data/caddy/pki/authorities/local/root.crt "$work/root.crt"
    ok 'internal TLS bootstrap verified certificate, SNI and HTTPS readiness'
  fi
  if [ "$integration" = 1 ]; then
    python3 scripts/integration_smoke.py --ca-file "$work/root.crt"
    ok "$scheme integration: six real application health routes"
    continue
  fi
  for host in localhost litellm.localhost langfuse.localhost s3.localhost backplane.localhost grafana.localhost; do
    case "$host" in
      backplane.*) upstream=bp-server ;;
      grafana.*) upstream=ob-gateway ;;
      *) upstream=lg-gateway ;;
    esac
    if [ "$scheme" = http ]; then
      body=$(curl -D "$work/headers" --noproxy '*' --max-time 10 --retry 10 --retry-delay 1 -fsS \
        -H "Host: $host" -H 'X-Forwarded-Proto: forged' "http://127.0.0.1:$PE_HTTP_PORT/")
    else
      # SNI needs the hostname; --resolve avoids relying on *.localhost DNS.
      body=$(curl -D "$work/headers" --noproxy '*' --max-time 10 --retry 10 --retry-delay 1 --cacert "$work/root.crt" -fsS \
        --resolve "$host:$PE_HTTPS_PORT:127.0.0.1" -H "Host: $host" -H 'X-Forwarded-Proto: forged' \
        "https://$host:$PE_HTTPS_PORT/")
    fi
    [ "$body" = "$upstream|$host|$scheme" ] || fail "$host forwarded '$body'"
    ok "$scheme $host: upstream, Host and X-Forwarded-Proto"
    if [ "$scheme" = https ]; then
      grep -iq '^Strict-Transport-Security: max-age=31536000' "$work/headers" || fail "$host missing HSTS"
      ok "$host: HSTS"
    fi
  done
  code=$(curl --noproxy '*' --max-time 10 -sS -o /dev/null -w '%{http_code}' -H 'Host: localhost' \
    "http://127.0.0.1:$PE_HTTP_PORT/health")
  [ "$code" = 200 ] || fail "HTTP health in $scheme mode returned $code"
  ok "HTTP /health is 200 in $scheme mode"
  for host in unknown.invalid rustfs.localhost; do
    code=$(curl --noproxy '*' --max-time 10 -sS -o /dev/null -w '%{http_code}' -H "Host: $host" \
      "http://127.0.0.1:$PE_HTTP_PORT/")
    [ "$code" = 404 ] || fail "unknown HTTP host $host in $scheme mode returned $code"
    ok "unknown HTTP host $host is 404 in $scheme mode"
  done
done

if [ "$integration" = 1 ]; then
  echo 'SHARED-HOST ACCEPTANCE PASSED (12 application health routes: 6 HTTP, 6 verified HTTPS)'
  exit 0
fi
for path in gateway backplane observability; do
  code=$(curl --noproxy '*' --max-time 10 --cacert "$work/root.crt" -sS \
    --resolve "localhost:$PE_HTTPS_PORT:127.0.0.1" -H 'Host: localhost' \
    -D "$work/headers" -o "$work/body" -w '%{http_code}' "https://localhost:$PE_HTTPS_PORT/health/$path")
  [ "$code" = 200 ] && [ ! -s "$work/body" ] || fail "$path health leaked a body or wrong status"
  grep -iq '^Cache-Control: no-store' "$work/headers" || fail "$path health can be cached"
  ok "$path: empty uncached probe"
done
curl --noproxy '*' --max-time 10 --cacert "$work/root.crt" -fsS \
  --resolve "localhost:$PE_HTTPS_PORT:127.0.0.1" "https://localhost:$PE_HTTPS_PORT/metrics" > "$work/metrics"
grep -q '^caddy_http_requests_total' "$work/metrics" || fail 'Caddy metrics missing'
grep -q '^pe_certificate_not_after_seconds ' "$work/metrics" || fail 'certificate expiry metric missing'
ok 'allowlisted metrics include Caddy and certificate expiry'

edge_id=$(docker compose --env-file "$env_file" ps -q caddy)
docker inspect "$edge_id" "$lg_stub" "$bp_stub" "$ob_stub" > "$work/containers.json"
python3 - "$work/containers.json" "$edge_id" <<'PY'
import json, sys
containers = json.load(open(sys.argv[1]))
assert len(containers) == 4
for c in containers:
    ports = c['HostConfig']['PortBindings'] or {}
    if c['Id'] == sys.argv[2]:
        assert set(ports) == {'80/tcp', '443/tcp'}, ports
        assert c['State']['Health']['Status'] == 'healthy'
        limits = c['HostConfig']
        assert limits['ReadonlyRootfs']
        assert limits['Memory'] == 256 * 1024 * 1024
        assert limits['MemoryReservation'] == 64 * 1024 * 1024
        assert limits['PidsLimit'] == 256 and limits['NanoCpus'] == 1000000000
        # Docker reports capabilities with or without the CAP_ prefix depending on version.
        caps = lambda values: {v.upper().removeprefix('CAP_') for v in (values or [])}
        assert caps(limits['CapDrop']) == {'ALL'} and caps(limits['CapAdd']) == {'NET_BIND_SERVICE'}, (limits['CapDrop'], limits['CapAdd'])
        assert 'no-new-privileges:true' in limits['SecurityOpt']
    else:
        assert not ports, f"stub {c['Name']} publishes ports"
PY
ok 'only the healthy edge publishes ports'

docker stop "$ob_stub" >/dev/null
# Restart with an absent alias to prove lazy resolution does not block startup.
docker compose --env-file "$env_file" up -d --force-recreate --wait --wait-timeout 120 >/dev/null
code=$(curl --noproxy '*' --max-time 10 --cacert "$work/root.crt" -sS --resolve "grafana.localhost:$PE_HTTPS_PORT:127.0.0.1" \
  -H 'Host: grafana.localhost' -o /dev/null -w '%{http_code}' "https://grafana.localhost:$PE_HTTPS_PORT/")
[ "$code" = 502 ] || fail "absent observability returned $code"
ok 'absent observability returns 502 after edge restart'
code=$(curl --noproxy '*' --max-time 10 --cacert "$work/root.crt" -sS \
  --resolve "localhost:$PE_HTTPS_PORT:127.0.0.1" -H 'Host: localhost' -D "$work/headers" \
  -o "$work/body" -w '%{http_code}' "https://localhost:$PE_HTTPS_PORT/health/observability")
[ "$code" = 502 ] && [ ! -s "$work/body" ] || fail 'failed probe leaked a body or hid failure'
grep -iq '^Cache-Control: no-store' "$work/headers" || fail 'failed probe can be cached'
ok 'failed console probe is empty and uncached'
body=$(curl --noproxy '*' --max-time 10 --cacert "$work/root.crt" -fsS --resolve "litellm.localhost:$PE_HTTPS_PORT:127.0.0.1" \
  -H 'Host: litellm.localhost' "https://litellm.localhost:$PE_HTTPS_PORT/")
[ "$body" = 'lg-gateway|litellm.localhost|https' ] || fail 'gateway failed with observability absent'
ok 'gateway still answers with observability absent'

docker stop "$lg_stub" >/dev/null
code=$(curl --noproxy '*' --max-time 10 --cacert "$work/root.crt" -sS --resolve "localhost:$PE_HTTPS_PORT:127.0.0.1" \
  -H 'Host: localhost' -o "$work/console.html" -w '%{http_code}' "https://localhost:$PE_HTTPS_PORT/")
[ "$code" = 502 ] || fail "fallback hid upstream failure: $code"
grep -q 'Platform Edge' "$work/console.html" || fail 'fallback console missing'
ok 'absent gateway serves the fallback page with status 502'
body=$(curl --noproxy '*' --max-time 10 --cacert "$work/root.crt" -fsS --resolve "backplane.localhost:$PE_HTTPS_PORT:127.0.0.1" \
  -H 'Host: backplane.localhost' "https://backplane.localhost:$PE_HTTPS_PORT/")
[ "$body" = 'bp-server|backplane.localhost|https' ] || fail 'backplane failed with other stacks absent'
for path in /metrics /health/operations /METRICS/ //health//operations; do
  code=$(curl --noproxy '*' --max-time 10 --cacert "$work/root.crt" -s -o /dev/null -w '%{http_code}' --resolve "backplane.localhost:$PE_HTTPS_PORT:127.0.0.1" \
    -H 'Host: backplane.localhost' "https://backplane.localhost:$PE_HTTPS_PORT$path")
  [ "$code" = 404 ] || fail "backplane operator path $path returned $code through the edge"
done
ok 'backplane operator paths are 404 at the edge'
ok 'backplane still answers with both other stacks absent'
code=$(curl --noproxy '*' --max-time 10 -sS -o /dev/null -w '%{http_code}' \
  "http://127.0.0.1:$PE_HTTP_PORT/health")
[ "$code" = 200 ] || fail 'edge health depends on an upstream'
ok 'edge health survives absent stacks'
echo "SMOKE CONTRACT PASSED ($pass checks)"
