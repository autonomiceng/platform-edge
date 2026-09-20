#!/usr/bin/env python3
"""Bounded host observation of Edge, without giving Caddy Docker access."""
import argparse
import fcntl
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from status_io import Unavailable, Unsupported, directory, now, publish, read_json, read_task, regular, run

TTL = 120
LIMIT = 1024 * 1024


def environment():
    allowed = {'PATH', 'HOME', 'USER', 'XDG_CONFIG_HOME', 'XDG_RUNTIME_DIR', 'SSH_AUTH_SOCK'}
    return {key: value for key, value in os.environ.items()
            if key in allowed or key.startswith('DOCKER_')}


def version(value):
    match = re.fullmatch(r'v?(\d{1,4}\.\d{1,4}\.\d{1,4})(?:-alpine)?', value, re.ASCII) if isinstance(value, str) else None
    return match[1] if match else None


def task_time(value, at):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z', value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        ceiling = datetime.fromisoformat(at.replace('Z', '+00:00'))
        if parsed.year < 1970 or (parsed - ceiling).total_seconds() > 5:
            return None
        return parsed.isoformat().replace('+00:00', 'Z')
    except ValueError:
        return None


def inspect_caddy(config, runner, clock):
    row = {'id': 'caddy', 'kind': 'service', 'configured': None, 'state': 'unknown',
           'observedAt': None, 'validForSeconds': TTL}
    service = config['services'].get('caddy')
    if not isinstance(service, dict):
        return row
    row['configured'] = True
    ref = service.get('image')
    if isinstance(ref, str):
        reference, _, digest = ref.partition('@')
        tag = reference.rsplit('/', 1)[-1].partition(':')[2]
        if tag:
            row['configuredVersion'] = version(tag) or 'custom'
        if re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            row['configuredDigest'] = digest
    at = clock()
    try:
        found = runner(['docker', 'ps', '--all', '--no-trunc', '--filter',
                        'label=com.docker.compose.project=' + config['name'], '--filter',
                        'label=com.docker.compose.service=caddy', '--filter',
                        'label=com.docker.compose.oneoff=False', '--format', '{{.ID}}'],
                       timeout=4, limit=65536).splitlines()
        if not found:
            return row  # An empty selection cannot distinguish a missing installation from a wrong project.
        if len(found) != 1 or not re.fullmatch(r'[0-9a-f]{64}', found[0]):
            return row
        docs = read_json(runner(['docker', 'inspect', found[0]], timeout=4, limit=LIMIT), LIMIT)
        if not isinstance(docs, list) or len(docs) != 1:
            raise Unavailable()
        doc = docs[0]
        labels = doc['Config']['Labels']
        if (doc['Id'] != found[0] or labels['com.docker.compose.project'] != config['name']
                or labels['com.docker.compose.service'] != 'caddy'
                or str(labels.get('com.docker.compose.oneoff', '')).lower() != 'false'):
            raise Unavailable()
        state = doc['State']
        if not isinstance(state, dict):
            raise Unavailable()
        row['observedAt'] = at
        if re.fullmatch(r'sha256:[0-9a-f]{64}', str(doc.get('Image', ''))):
            row['observedImageId'] = doc['Image']
        if state.get('Status') in ('dead', 'exited'):
            row['state'] = 'unavailable'
        elif state.get('Status') == 'restarting':
            row['state'] = 'starting'
        elif state.get('Status') == 'running' and not state.get('Paused'):
            command = ['docker', 'exec', found[0], 'timeout', '-s', 'KILL', '3']
            try:
                # This fixed listener exists in every access mode. It proves only Caddy HTTP readiness.
                runner(command + ['wget', '-qO-', 'http://127.0.0.1/health'], timeout=4, limit=65536)
                row['state'] = 'healthy'
            except Unsupported:
                pass
            except Unavailable:
                row['state'] = 'unavailable'
            try:
                release = version(runner(command + ['caddy', 'version'], timeout=4, limit=65536).strip().split(' ', 1)[0])
                if release:
                    row['observedVersion'] = release
            except Unavailable:
                pass
        return row
    except (Unavailable, KeyError, TypeError, ValueError, AttributeError):
        row.pop('observedImageId', None)
        row.update(state='unknown', observedAt=None)
        return row


def collect(root, env_file, runner=run, clock=now):
    at = clock()
    caddy = {'id': 'caddy', 'kind': 'service', 'configured': None, 'state': 'unknown',
             'observedAt': None, 'validForSeconds': TTL}
    task = {'id': 'bootstrap', 'kind': 'task', 'configured': None, 'state': 'unknown',
            'observedAt': None, 'validForSeconds': TTL, 'lastExecutionAt': None}
    try:
        config = read_json(runner(['docker', 'compose', '--project-directory', str(root),
                                  '--env-file', str(env_file), 'config', '--format', 'json'],
                                 timeout=10, limit=LIMIT, cwd=root, env=environment()), LIMIT)
        if (not isinstance(config, dict) or not isinstance(config.get('services'), dict)
                or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,127}', config.get('name', ''))):
            raise Unavailable()
    except (Unavailable, TypeError, ValueError):
        at = None
    else:
        caddy = inspect_caddy(config, runner, clock)
        try:
            record = read_task(root, env_file)
            observed = clock()
            started = task_time(record.get('lastExecutionAt'), observed)
            if started and record.get('state') in ('healthy', 'unavailable', 'unknown'):
                task.update(configured=True, state=record['state'], observedAt=observed, lastExecutionAt=started)
        except (OSError, Unavailable):
            pass
    return {'schemaVersion': 1, 'stack': 'edge', 'generatedAt': clock(),
            'configurationObservedAt': at, 'configurationValidForSeconds': TTL,
            'telemetry': 'unknown', 'components': [caddy, task]}


def observe(root, env_file, runner=run, clock=now):
    with directory(root / 'data/console') as fd:
        regular(fd, '.status.lock')
        lock = os.open('.status.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            document = collect(root, env_file, runner, clock)
            publish(fd, 'status.json', document)
        finally:
            os.close(lock)
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', required=True, type=Path)
    parser.add_argument('--env-file', required=True, type=Path)
    args = parser.parse_args()
    root, env_file = Path(os.path.abspath(args.checkout)), Path(os.path.abspath(args.env_file))
    if not (root / 'compose.yaml').is_file() or not env_file.is_file():
        print('status observer: checkout or env file unavailable', file=sys.stderr)
        return 1
    try:
        observe(root, env_file)
    except (OSError, Unavailable):
        print('status observer: observation or publication failed', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
