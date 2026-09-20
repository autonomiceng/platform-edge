#!/bin/sh
# Static gates and image-provided validation. No installed stack is started.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
# Validate the shipped default independently of operator image and Compose overrides.
export PE_CADDY_IMAGE=''
unset COMPOSE_FILE COMPOSE_ENV_FILES COMPOSE_PROFILES
for tool in python3 shellcheck; do
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
for mode in 'local http localhost' 'public https example.com' 'proxy https example.com'; do
  # Each mode is a fixed three-word tuple.
  # shellcheck disable=SC2086
  set -- $mode
  docker run --rm -e "PE_ACCESS_MODE=$1" -e "PE_SCHEME=$2" -e "PE_PUBLIC_DOMAIN=$3" -e PE_ACME_EMAIL= \
    -v "$root/Caddyfile:/etc/caddy/Caddyfile:ro" \
    -v "$root/routes.d:/etc/caddy/routes.d:ro" \
    -v "$root/docker/console:/srv/console:ro" \
    "$caddy_image" caddy validate --config /etc/caddy/Caddyfile
done
echo 'Caddyfile (3 access modes): PASS'

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
