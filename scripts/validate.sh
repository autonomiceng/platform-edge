#!/bin/sh
# Static gates and image-provided validation. No installed stack is started.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
# Validate the shipped default independently of operator image and Compose overrides.
export PE_CADDY_IMAGE=''
unset COMPOSE_FILE COMPOSE_ENV_FILES COMPOSE_PROFILES PE_PLATFORM_SUBNET PE_PLATFORM_IP_RANGE PE_EDGE_IP
unset PE_TLS_ISSUER PE_TLS_DIR PE_TLS_CA PE_ACME_CA PE_ACME_CA_ROOT PE_ACME_EAB_KEY_ID PE_ACME_EAB_HMAC
for tool in python3 shellcheck node openssl; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
trap 'exit 1' HUP INT TERM
python3 scripts/bootstrap.py --env-file "$work/.env" --render-only >/dev/null
echo 'env render: PASS'
shellcheck scripts/*.sh
echo 'shellcheck: PASS'
python3 -m py_compile scripts/*.py tests/*.py
echo 'python: PASS'
python3 -c 'import json,sys; [json.load(open(path)) for path in sys.argv[1:]]' docs/operations/status-fixtures/*.json
echo 'status fixtures parse: PASS'
scripts/sync-conventions.sh --check >/dev/null
echo 'conventions canonical (no vendoring header): PASS'
node --test tests/status*.test.cjs
command -v docker >/dev/null || { echo 'missing tool: docker' >&2; exit 1; }
docker compose version >/dev/null
docker compose --env-file "$work/.env" -f compose.yaml config -q
docker compose --env-file "$work/.env" -f compose.yaml config --format json > "$work/config.json"
python3 - "$work/config.json" <<'PY'
import json, re, sys
config = json.load(open(sys.argv[1]))
services = config['services']
assert set(services) == {'caddy'}, f'unexpected services: {sorted(services)}'
assert {name for name, svc in services.items() if svc.get('ports')} == {'caddy'}, 'only caddy publishes'
caddy = services['caddy']
assert set(caddy['networks']) == {'platform'}, 'caddy joins only platform'
assert caddy['networks']['platform']['aliases'] == ['pe-edge'], 'edge alias changed'
assert caddy['networks']['platform']['ipv4_address'] == '172.30.0.2', 'fixed Edge address changed'
assert set(config['networks']) == {'platform'} and config['networks']['platform']['external'], 'external network required'
assert set(config['volumes']) == {'edge-data', 'edge-config'}, 'volume names changed'
assert all(v['external'] for v in config['volumes'].values()), 'durable volumes must be external'
assert re.fullmatch(r'.+:[^:@]+@sha256:[0-9a-f]{64}', caddy['image']) and ':latest@' not in caddy['image'], 'pin tag and digest'
assert int(caddy['mem_limit']) == 256 * 1024 * 1024, 'memory limit'
assert int(caddy['mem_reservation']) == 64 * 1024 * 1024, 'memory reservation'
assert caddy['pids_limit'] == 256 and float(caddy['cpus']) == 1, 'PID and CPU limits'
assert caddy['read_only'] and caddy['tmpfs'] == ['/tmp'], 'writable-root policy'
assert caddy['cap_drop'] == ['ALL'] and caddy['cap_add'] == ['NET_BIND_SERVICE'], 'capabilities'
assert caddy['security_opt'] == ['no-new-privileges:true'], 'privilege escalation'
assert caddy['logging'] == {'driver': 'journald', 'options': {'cache-disabled': 'true'}}, 'journald without Docker cache'
assert not caddy.get('env_file'), 'list environment explicitly'
assert {p['target'] for p in caddy['ports']} == {80, 443}, 'HTTP and HTTPS only'
PY
echo 'compose config: PASS'
caddy_image=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["services"]["caddy"]["image"])' "$work/config.json")
python3 - "$caddy_image" "$work" <<'PY'
import os, subprocess, sys
from pathlib import Path
env = dict(os.environ)
env.pop('PE_CADDY_IMAGE', None)
assert ('    image: ${PE_CADDY_IMAGE:-' + sys.argv[1] + '}') in Path('compose.yaml').read_text().splitlines(), \
    'keep the full image reference in one Renovate-extractable default'
path = Path(sys.argv[2]) / 'image.env'
for value in ('local/edge:experiment', 'registry.example:5000/team/caddy:test', '', None):
    path.write_text('' if value is None else 'PE_CADDY_IMAGE=' + value + '\n')
    actual = subprocess.check_output(['docker', 'compose', '--env-file', str(path),
                                      '-f', 'compose.yaml', 'config', '--images'], env=env, text=True).strip()
    assert actual == (value or sys.argv[1]), 'native image override or fallback differs'
PY
echo 'native image override and empty/unset fallback (4 cases): PASS'
# Throwaway certificate inputs for the files issuer and the private ACME trust file.
mkdir "$work/certs"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -noenc -keyout "$work/certs/tls.key" -out "$work/certs/tls.crt" \
  -subj /CN=example.com -addext 'subjectAltName=DNS:example.com,DNS:*.example.com,DNS:localhost,DNS:*.localhost' -days 2 2>/dev/null
cp "$work/certs/tls.crt" "$work/acme-ca-root.crt"
validate_caddyfile() {
  docker run --rm "$@" \
    -v "$root/Caddyfile:/etc/caddy/Caddyfile:ro" \
    -v "$root/routes.d:/etc/caddy/routes.d:ro" \
    -v "$root/docker/console:/srv/console:ro" \
    "$caddy_image" caddy validate --config /etc/caddy/Caddyfile
}
validate_caddyfile -e PE_ACCESS_MODE=local -e PE_SCHEME=http -e PE_PUBLIC_DOMAIN=localhost -e PE_TLS_ISSUER=internal
validate_caddyfile -e PE_ACCESS_MODE=local -e PE_SCHEME=http -e PE_PUBLIC_DOMAIN=localhost -e PE_TLS_ISSUER=files \
  -v "$work/certs:/certs:ro"
validate_caddyfile -e PE_ACCESS_MODE=public -e PE_SCHEME=https -e PE_PUBLIC_DOMAIN=example.com -e PE_TLS_ISSUER=acme \
  -e PE_ACME_EMAIL= -e PE_ACME_CA=
validate_caddyfile -e PE_ACCESS_MODE=public -e PE_SCHEME=https -e PE_PUBLIC_DOMAIN=example.com -e PE_TLS_ISSUER=acme \
  -e PE_ACME_EMAIL=ops@example.com -e PE_ACME_CA=https://ca.example.com/acme/acme/directory \
  -e PE_ACME_TRUST=file -v "$work/acme-ca-root.crt:/certs/acme-ca-root.crt:ro"
validate_caddyfile -e PE_ACCESS_MODE=public -e PE_SCHEME=https -e PE_PUBLIC_DOMAIN=example.com -e PE_TLS_ISSUER=acme \
  -e PE_ACME_EMAIL= -e PE_ACME_CA=https://ca.example.com/acme/acme/directory \
  -e PE_ACME_ACCOUNT=eab -e PE_ACME_EAB_KEY_ID=key-id -e PE_ACME_EAB_HMAC=bWFj
validate_caddyfile -e PE_ACCESS_MODE=public -e PE_SCHEME=https -e PE_PUBLIC_DOMAIN=example.com -e PE_TLS_ISSUER=files \
  -v "$work/certs:/certs:ro"
validate_caddyfile -e PE_ACCESS_MODE=proxy -e PE_SCHEME=https -e PE_PUBLIC_DOMAIN=example.com
echo 'Caddyfile (local-internal, local-files, public-acme, public-acme-ca, public-acme-eab, public-files, proxy): PASS'

PE_ACCESS_MODE=proxy docker compose --env-file "$work/.env" -f compose.yaml -f compose.proxy.yaml config --format json > "$work/proxy.json"
python3 - "$work/proxy.json" <<'PYCODE'
import json, sys
service = json.load(open(sys.argv[1]))['services']['caddy']
assert [p['target'] for p in service['ports']] == [80], 'proxy must publish HTTP only'
PYCODE
echo 'proxy publishes HTTP only: PASS'

PE_ACCESS_MODE=public PE_SCHEME='' docker compose --env-file "$work/.env" -f compose.yaml -f compose.public.yaml config --format json > "$work/public.json"
python3 - "$work/public.json" <<'PYCODE'
import json, sys
service = json.load(open(sys.argv[1]))['services']['caddy']
assert service['environment']['PE_SCHEME'] == 'https', 'public empty scheme must resolve to HTTPS'
PYCODE
echo 'public default scheme: PASS'

PE_ACCESS_MODE=public PE_ACME_CA_ROOT="$work/acme-ca-root.crt" PE_ACME_EAB_KEY_ID=key-id PE_ACME_EAB_HMAC=bWFj \
  docker compose --env-file "$work/.env" -f compose.yaml -f compose.public.yaml -f compose.acme-ca-root.yaml -f compose.acme-eab.yaml \
  config --format json > "$work/acme.json"
PE_TLS_ISSUER=files PE_TLS_DIR="$work/certs" docker compose --env-file "$work/.env" -f compose.yaml -f compose.files.yaml \
  config --format json > "$work/files.json"
python3 - "$work/acme.json" "$work/files.json" "$work" <<'PYCODE'
import json, sys
acme = json.load(open(sys.argv[1]))['services']['caddy']
files = json.load(open(sys.argv[2]))['services']['caddy']
work = sys.argv[3]
environment = acme['environment']
assert (environment['PE_TLS_ISSUER'], environment['PE_ACME_TRUST'], environment['PE_ACME_ACCOUNT']) == ('acme', 'file', 'eab'), environment
root = {m['target']: m for m in acme['volumes']}['/certs/acme-ca-root.crt']
assert root['source'] == work + '/acme-ca-root.crt' and root['read_only'] and not root['bind'].get('create_host_path', True), root
certs = {m['target']: m for m in files['volumes']}['/certs']
assert certs['source'] == work + '/certs' and certs['read_only'] and not certs['bind'].get('create_host_path', True), certs
assert files['environment']['PE_TLS_ISSUER'] == 'files', files['environment']
# Caddy applies snippet defaults only to unset variables; the base must not define these.
assert not {'PE_ACME_TRUST', 'PE_ACME_ACCOUNT'} & set(files['environment']), 'ACME discriminators leak into the base'
PYCODE
echo 'TLS overlays: read-only mounts without host path creation, discriminators only from overlays: PASS'
