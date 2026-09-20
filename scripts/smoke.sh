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
export PE_CADDY_IMAGE='' PE_ACCESS_MODE=local PE_SCHEME=http PE_ACME_EMAIL='' PE_BACKUP_KEEP=7 PE_TAILSCALE_HOST=''
export PE_METRICS_ALLOW="127.0.0.0/8 ::1"
export PE_VOLUME_PREFIX="$COMPOSE_PROJECT_NAME" PE_BACKUP_DIR=/tmp/unused-edge-smoke-backups
export PE_BIND_HOST=127.0.0.1 PE_HTTP_PORT="${SMOKE_HTTP_PORT:-18280}" PE_HTTPS_PORT="${SMOKE_HTTPS_PORT:-18643}"
unset COMPOSE_FILE COMPOSE_ENV_FILES COMPOSE_PROFILES
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
# Status transport reads a frozen fixture owned by this smoke, never local observations.
mkdir "$work/status"
printf '%s\n' '{"schemaVersion":1,"stack":"edge","generatedAt":"2026-09-20T00:00:00Z","components":[]}' > "$work/status/status.json"
cat > "$work/status.yaml" <<YAML
services:
  caddy:
    volumes:
      - $work/status:/srv/state:ro
YAML
export COMPOSE_FILE="${COMPOSE_FILE:-$root/compose.yaml}:$work/status.yaml"
# Disposable loopback-only smoke uses the host relay; never use this allowance in an installation.
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
	header X-Smoke-Forwarded-For {http.request.header.X-Forwarded-For}
	header X-Smoke-Forwarded-Host {http.request.header.X-Forwarded-Host}
	header X-Smoke-Authorization {http.request.header.Authorization}
	header X-Smoke-Cookie {http.request.header.Cookie}
	# Probe responses suppress upstream headers, so leaked credentials must also fail status.
	@probe_credentials {
		header X-Smoke-Probe true
		header Authorization *
	}
	respond @probe_credentials 401
	handle /status.json {
		route {
			@authorization header Authorization *
			respond @authorization "credential leaked" 401
			@cookie header Cookie *
			respond @cookie "cookie leaked" 401
			@missing header X-Smoke-Status missing
			respond @missing "private diagnostic" 404
			@failure header X-Smoke-Status failure
			respond @failure "private diagnostic" 503
			@html header X-Smoke-Status html
			handle @html {
				header Content-Type text/html
				respond "<html>fallback</html>" 200
			}
			import {$STUB_STATUS_HANDLER:/etc/caddy/respond-status.caddy}
		}
	}
	header /versions.json Content-Type application/json
	respond /versions.json `{"images":{"litellm":"1.2.3"}}`
	respond /authority "{$STUB_ALIAS}|{http.request.hostport}|{http.request.header.X-Forwarded-Proto}"
	respond "{$STUB_ALIAS}|{host}|{http.request.header.X-Forwarded-Proto}"
}
CADDY
cat > "$work/respond-status.caddy" <<'CADDY'
header Content-Type application/json
respond `{"producer":"{$STUB_ALIAS}","host":"{http.request.host}","scheme":"{http.request.header.X-Forwarded-Proto}"}`
CADDY
cat > "$work/file-status.caddy" <<'CADDY'
root * /srv/stub-status
file_server
CADDY
mkdir "$work/stub-status"
printf '%s\n' '{"producer":"ob-gateway","host":"localhost","scheme":"http"}' > "$work/stub-status/status.json"
lg_stub=$(docker run -d --network "$PE_PLATFORM_NETWORK" --network-alias lg-gateway -e STUB_ALIAS=lg-gateway \
  -v "$work/stub.caddy:/etc/caddy/Caddyfile:ro" -v "$work/respond-status.caddy:/etc/caddy/respond-status.caddy:ro" \
  "$caddy_image")
bp_stub=$(docker run -d --network "$PE_PLATFORM_NETWORK" --network-alias bp-gateway -e STUB_ALIAS=bp-gateway \
  -v "$work/stub.caddy:/etc/caddy/Caddyfile:ro" -v "$work/respond-status.caddy:/etc/caddy/respond-status.caddy:ro" \
  "$caddy_image")
ob_stub=$(docker run -d --network "$PE_PLATFORM_NETWORK" --network-alias ob-gateway -e STUB_ALIAS=ob-gateway \
  -e STUB_STATUS_HANDLER=/etc/caddy/file-status.caddy -v "$work/stub.caddy:/etc/caddy/Caddyfile:ro" \
  -v "$work/file-status.caddy:/etc/caddy/file-status.caddy:ro" -v "$work/stub-status:/srv/stub-status:ro" \
  "$caddy_image")
fi
edge_started=1
python3 scripts/bootstrap.py --env-file "$env_file"
ok 'bootstrap reached readiness'

if [ "$integration" != 1 ]; then
  curl --noproxy '*' --max-time 10 -fsS -D "$work/version.headers" \
    -H 'Host: private.test.ts.net' -H 'Authorization: Bearer smoke-token' -H 'Cookie: smoke-session=private' \
    "http://127.0.0.1:$PE_HTTP_PORT/stack-versions/gateway" > "$work/versions.json"
  python3 - "$work/versions.json" "$work/version.headers" <<'PYVERSIONS'
import json, sys
from pathlib import Path
assert json.loads(Path(sys.argv[1]).read_text())["images"]["litellm"] == "1.2.3"
headers = dict(line.lower().split(":", 1) for line in Path(sys.argv[2]).read_text().splitlines() if ":" in line)
assert headers["cache-control"].strip() == "no-store"
assert not headers.get("x-smoke-authorization", "").strip()
assert not headers.get("x-smoke-cookie", "").strip()
PYVERSIONS
  ok 'console version metadata is uncached and forwards no browser credentials'
  python3 tests/status_proxy.py "http://127.0.0.1:$PE_HTTP_PORT" --edge-status
  ok 'status proxy methods, credential stripping, content type and body suppression'
fi


for scheme in http https; do
  if [ "$scheme" = https ]; then
    export PE_SCHEME=https
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
      backplane.*) upstream=bp-gateway ;;
      grafana.*) upstream=ob-gateway ;;
      *) upstream=lg-gateway ;;
    esac
    probe_path=/
    [ "$host" != localhost ] || probe_path=/authority
    if [ "$scheme" = http ]; then
      body=$(curl -D "$work/headers" --noproxy '*' --max-time 10 --retry 10 --retry-delay 1 -fsS \
        -H "Host: $host" -H 'X-Forwarded-Proto: forged' -H 'X-Forwarded-For: 198.51.100.9' \
        -H 'X-Forwarded-Host: forged.invalid' -H 'Authorization: Bearer smoke-operator-token' "http://127.0.0.1:$PE_HTTP_PORT$probe_path")
    else
      # SNI needs the hostname; --resolve avoids relying on *.localhost DNS.
      body=$(curl -D "$work/headers" --noproxy '*' --max-time 10 --retry 10 --retry-delay 1 --cacert "$work/root.crt" -fsS \
        --resolve "$host:$PE_HTTPS_PORT:127.0.0.1" -H "Host: $host" -H 'X-Forwarded-Proto: forged' \
        -H 'X-Forwarded-For: 198.51.100.9' -H 'X-Forwarded-Host: forged.invalid' \
        -H 'Authorization: Bearer smoke-operator-token' \
        "https://$host:$PE_HTTPS_PORT$probe_path")
    fi
    [ "$body" = "$upstream|$host|$scheme" ] || fail "$host forwarded '$body'"
    python3 - "$work/headers" "$host" <<'PY'
import ipaddress, sys
from pathlib import Path
headers = dict(line.lower().split(':', 1) for line in Path(sys.argv[1]).read_text().splitlines() if ':' in line)
peer = headers['x-smoke-forwarded-for'].strip()
assert str(ipaddress.ip_address(peer)) != '198.51.100.9', peer
assert headers['x-smoke-forwarded-host'].strip() == sys.argv[2], headers
assert headers['x-smoke-authorization'].strip() == 'bearer smoke-operator-token', headers
PY
    ok "$scheme $host: upstream, Host and unspoofed X-Forwarded-Proto/For/Host"
    if [ "$scheme" = https ]; then
      if grep -iq '^Strict-Transport-Security:' "$work/headers"; then fail "local $host enables HSTS"; fi
      ok "$host: local HTTPS without HSTS"
    fi
  done
  for path in /health/ready /health/ready/ /health/ready. //HEALTH//ready; do
    if [ "$scheme" = http ]; then
      body=$(curl --path-as-is --noproxy '*' --max-time 10 -fsS -D "$work/headers" \
        -H 'Host: backplane.localhost' -H 'Authorization: Bearer smoke-operator-token' \
        "http://127.0.0.1:$PE_HTTP_PORT$path")
    else
      body=$(curl --path-as-is --noproxy '*' --max-time 10 -fsS -D "$work/headers" --cacert "$work/root.crt" \
        --resolve "backplane.localhost:$PE_HTTPS_PORT:127.0.0.1" -H 'Host: backplane.localhost' \
        -H 'Authorization: Bearer smoke-operator-token' "https://backplane.localhost:$PE_HTTPS_PORT$path")
    fi
    [ "$body" = "bp-gateway|backplane.localhost|$scheme" ] || fail "readiness variant $path did not reach backplane"
    if grep -iq 'smoke-operator-token' "$work/headers"; then fail "readiness variant $path retained Authorization"; fi
  done
  ok "$scheme readiness variants strip Authorization"
  code=$(curl --noproxy '*' --max-time 10 -sS -o /dev/null -w '%{http_code}' -H 'Host: localhost' \
    "http://127.0.0.1:$PE_HTTP_PORT/health")
  [ "$code" = 200 ] || fail "HTTP health in $scheme mode returned $code"
  ok "HTTP /health is 200 in $scheme mode"
  for host in unknown.invalid rustfs.localhost; do
    code=$(curl --noproxy '*' --max-time 10 -sS -o /dev/null -w '%{http_code}' -H "Host: $host" \
      "http://127.0.0.1:$PE_HTTP_PORT/")
    [ "$code" = 200 ] || fail "unknown HTTP host $host in $scheme mode returned $code"
    ok "unknown HTTP host $host serves the console in $scheme mode"
  done
done

if [ "$integration" = 1 ]; then
  echo 'SHARED-HOST ACCEPTANCE PASSED (12 application health routes: 6 HTTP, 6 verified HTTPS)'
  exit 0
fi
for path in gateway backplane observability; do
  code=$(curl --noproxy '*' --max-time 10 --cacert "$work/root.crt" -sS \
    --resolve "localhost:$PE_HTTPS_PORT:127.0.0.1" -H 'Host: localhost' \
    -H 'X-Smoke-Probe: true' -H 'Authorization: Bearer smoke-operator-token' \
    -D "$work/headers" -o "$work/body" -w '%{http_code}' "https://localhost:$PE_HTTPS_PORT/health/$path")
  [ "$code" = 200 ] && [ ! -s "$work/body" ] || fail "$path health leaked a body or wrong status"
  grep -iq '^Cache-Control: no-store' "$work/headers" || fail "$path health can be cached"
  if grep -iq '^X-Smoke-Authorization:.*smoke-operator-token' "$work/headers"; then
    fail "$path health retained Authorization"
  fi
  ok "$path: empty uncached probe strips Authorization"
done
curl --noproxy '*' --max-time 10 --cacert "$work/root.crt" -fsS \
  --resolve "localhost:$PE_HTTPS_PORT:127.0.0.1" "https://localhost:$PE_HTTPS_PORT/metrics" > "$work/metrics"
grep -q '^caddy_http_requests_total' "$work/metrics" || fail 'Caddy metrics missing'
grep -q '^pe_certificate_not_after_seconds ' "$work/metrics" || fail 'certificate expiry metric missing'
ok 'allowlisted metrics include Caddy and certificate expiry'
curl --noproxy '*' --max-time 10 -fsS -H 'Host: pe-edge' "http://127.0.0.1:$PE_HTTP_PORT/metrics" > "$work/internal-metrics"
grep -q '^pe_certificate_not_after_seconds ' "$work/internal-metrics" || fail 'internal metrics hostname is unavailable'
ok 'stable internal metrics hostname serves the allowlisted scraper'

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
python3 tests/status_proxy.py "http://127.0.0.1:$PE_HTTP_PORT" --absent-observability --edge-status
rm "$work/status/status.json"
python3 tests/status_proxy.py "http://127.0.0.1:$PE_HTTP_PORT" --absent-observability
ok 'status producer connection failure is empty and independent'
body=$(curl --noproxy '*' --max-time 10 --cacert "$work/root.crt" -fsS --resolve "litellm.localhost:$PE_HTTPS_PORT:127.0.0.1" \
  -H 'Host: litellm.localhost' "https://litellm.localhost:$PE_HTTPS_PORT/")
[ "$body" = 'lg-gateway|litellm.localhost|https' ] || fail 'gateway failed with observability absent'
ok 'gateway still answers with observability absent'

docker stop "$lg_stub" >/dev/null
code=$(curl --noproxy '*' --max-time 10 --cacert "$work/root.crt" -sS --resolve "localhost:$PE_HTTPS_PORT:127.0.0.1" \
  -H 'Host: localhost' -D "$work/console.headers" -o "$work/console.html" -w '%{http_code}' "https://localhost:$PE_HTTPS_PORT/")
[ "$code" = 200 ] || fail "Edge console depends on absent gateway: $code"
grep -q 'Platform Edge' "$work/console.html" || fail 'console missing'
grep -qi 'Cache-Control: no-cache' "$work/console.headers" || fail 'console HTML must revalidate'
ok 'Edge console remains available when Gateway is absent'
for asset in app.js catalog.js status.js style.css icons/caddy.svg; do
  curl --noproxy '*' --max-time 10 -fsS -H 'Host: localhost' "http://127.0.0.1:$PE_HTTP_PORT/console/$asset" > "$work/asset"
  [ -s "$work/asset" ] || fail "empty console asset: $asset"
  ok "console asset $asset remains available without Gateway"
done
curl --noproxy '*' --max-time 10 -fsS -D "$work/config.headers" -H 'Host: localhost' \
  "http://127.0.0.1:$PE_HTTP_PORT/edge-config.json" > "$work/edge-config.json"
python3 - "$work/edge-config.json" <<'PYCONFIG'
import json, sys
config = json.load(open(sys.argv[1]))
assert config["domain"] == "localhost"
assert "backplane" in config["ports"]
PYCONFIG
grep -qi 'Cache-Control: no-store' "$work/config.headers" || fail 'console settings may be cached'
ok 'console settings remain available and uncached when the gateway is absent'

body=$(curl --noproxy '*' --max-time 10 --cacert "$work/root.crt" -fsS --resolve "backplane.localhost:$PE_HTTPS_PORT:127.0.0.1" \
  -H 'Host: backplane.localhost' "https://backplane.localhost:$PE_HTTPS_PORT/")
[ "$body" = 'bp-gateway|backplane.localhost|https' ] || fail 'backplane failed with other stacks absent'
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

# Local mode must keep both protocols usable without a redirect or HSTS.
export PE_ACCESS_MODE=local PE_SCHEME=http
python3 scripts/bootstrap.py --env-file "$env_file" >/dev/null
docker compose --env-file "$env_file" cp caddy:/data/caddy/pki/authorities/local/root.crt "$work/root.crt" >/dev/null
for protocol in http https; do
  if [ "$protocol" = http ]; then
    code=$(curl --noproxy '*' --max-time 10 -sS -D "$work/headers" -o "$work/body" -w '%{http_code}' \
      -H 'Host: backplane.localhost' "http://127.0.0.1:$PE_HTTP_PORT/")
  else
    code=$(curl --noproxy '*' --max-time 10 --cacert "$work/root.crt" -sS -D "$work/headers" -o "$work/body" -w '%{http_code}' \
      --resolve "backplane.localhost:$PE_HTTPS_PORT:127.0.0.1" "https://backplane.localhost:$PE_HTTPS_PORT/")
  fi
  [ "$code" = 200 ] || fail "local $protocol application returned $code"
  if grep -iq '^Strict-Transport-Security:\|^Location:' "$work/headers"; then fail "local $protocol forces HTTPS"; fi
  ok "local $protocol application without redirect or HSTS"
done
for host in 127.0.0.1 example.tail123.ts.net; do
  code=$(curl --noproxy '*' --max-time 10 -sS -H "Host: $host" -o "$work/body" -w '%{http_code}' "http://127.0.0.1:$PE_HTTP_PORT/")
  [ "$code" = 200 ] || fail "alias console $host returned $code"
  grep -q 'Platform Edge' "$work/body" || fail 'alias console missing'
  grep -q '"domain": "localhost"' "$work/body" || fail 'console domain template not rendered'
  ok "HTTP console accepts $host"
done
curl --noproxy '*' --max-time 10 --cacert "$work/root.crt" -fsS "https://127.0.0.1:$PE_HTTPS_PORT/health" >/dev/null
ok 'verified HTTPS IP health'
curl --noproxy '*' --max-time 10 -fsS -H 'X-Api-Key: edge-secret-header' "http://127.0.0.1:$PE_HTTP_PORT/health?token=edge-secret-query" >/dev/null
docker compose --env-file "$env_file" logs --no-log-prefix caddy > "$work/logs" 2>&1
grep -q 'http.log.access' "$work/logs" || fail 'access logs unavailable through Docker journald reader'
grep -q '/health?REDACTED' "$work/logs" || fail 'query redaction was not observed'
if grep -q 'edge-secret-header\|edge-secret-query' "$work/logs"; then fail 'access logs contain credentials'; fi
ok 'journald access logs readable without Alloy; headers and query strings removed'
# Probe proxy mode through its explicit override; never publish its unused HTTPS port.
export PE_ACCESS_MODE=proxy PE_SCHEME=https
export COMPOSE_FILE="$root/compose.yaml:$root/compose.proxy.yaml"
python3 scripts/bootstrap.py --env-file "$env_file" >/dev/null
body=$(curl --noproxy '*' --max-time 10 -fsS -H 'Host: backplane.localhost' -H 'X-Forwarded-Proto: forged' "http://127.0.0.1:$PE_HTTP_PORT/")
[ "$body" = 'bp-gateway|backplane.localhost|https' ] || fail 'proxy lost configured public scheme'
docker compose --env-file "$env_file" ps --format json > "$work/proxy-ports.json"
python3 - "$work/proxy-ports.json" <<'PYCODE'
import json, sys
service = json.loads(open(sys.argv[1]).read())
assert [p['TargetPort'] for p in service['Publishers'] if p.get('PublishedPort')] == [80], service
PYCODE
ok 'proxy preserves public HTTPS and publishes HTTP only'
# One machine hostname must dispatch by port without losing signed S3 authorities.
export PE_TAILSCALE_HOST=example.tail123.ts.net PE_ACCESS_MODE=local PE_SCHEME=http
export COMPOSE_FILE="$root/compose.yaml"
python3 scripts/bootstrap.py --env-file "$env_file" >/dev/null
curl --noproxy '*' --max-time 10 -fsS "http://127.0.0.1:$PE_HTTP_PORT/health" >/dev/null
curl --noproxy '*' --max-time 10 --cacert "$work/root.crt" -fsS "https://127.0.0.1:$PE_HTTPS_PORT/health" >/dev/null
ok 'Tailscale routes coexist with local HTTP and verified self-signed HTTPS'
docker start "$lg_stub" "$ob_stub" >/dev/null
for item in '8443 lg-gateway' '8444 lg-gateway' '8445 lg-gateway' '8446 lg-gateway' '8447 ob-gateway' '8448 bp-gateway' '8449 lg-gateway'; do
  # shellcheck disable=SC2086
  set -- $item
  body=$(curl --noproxy '*' --retry 5 --retry-all-errors --retry-delay 1 --max-time 10 -fsS -H "Host: $PE_TAILSCALE_HOST:$1" -H 'X-Forwarded-Proto: forged' "http://127.0.0.1:$PE_HTTP_PORT/authority")
  [ "$body" = "$2|$PE_TAILSCALE_HOST:$1|https" ] || fail "Tailscale application $1 lost route, Host or scheme: $body"
  ok "Tailscale $1 reaches $2 with exact authority and HTTPS scheme"
done
code=$(curl --noproxy '*' --max-time 10 -sS -H "Host: $PE_TAILSCALE_HOST:8448" -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PE_HTTP_PORT/metrics")
[ "$code" = 404 ] || fail 'Tailscale backplane leaked operator endpoint'
ok 'Tailscale backplane retains operator endpoint restrictions'
echo "SMOKE CONTRACT PASSED ($pass checks)"
