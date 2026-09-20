#!/usr/bin/env python3
"""Opt in to periodic status publication for one explicit checkout and env file."""

import argparse
import fcntl
import stat
import os
import sys
from pathlib import Path

from status_io import Unavailable, directory, regular, run

NAME = 'platform-edge-status'


def quote(value):
    # systemd performs specifier and environment expansion even without a shell.
    value = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise Unavailable()
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def units(root, env_file):
    command = ' '.join(quote(part) for part in (
        sys.executable, root / 'scripts/status_observer.py', '--checkout', root,
        '--env-file', env_file))
    service = f'''[Unit]
Description=Platform Edge public status observation

[Service]
Type=oneshot
ExecStart={command}
TimeoutStartSec=90
UMask=0022
NoNewPrivileges=true
StandardOutput=null
StandardError=journal
'''
    timer = f'''[Unit]
Description=Refresh Platform Edge public status observations

[Timer]
OnStartupSec=10s
OnUnitInactiveSec=30s
AccuracySec=1s
Unit={NAME}.service

[Install]
WantedBy=timers.target
'''
    return {NAME + '.service': service, NAME + '.timer': timer}


def matching_pair(fd, contents):
    present = 0
    for name, text in contents.items():
        try:
            os.stat(name + '.d', dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise Unavailable()
        regular(fd, name)
        try:
            handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            continue
        with os.fdopen(handle, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or stream.read(len(text.encode()) + 1) != text.encode()):
                raise Unavailable()
        present += 1
    if present not in (0, len(contents)):
        raise Unavailable()
    return bool(present)


def check(root, env_file, unit_dir, runner=None):
    """Inspect without creating directories, units, or reading the env contents."""
    contents = units(Path(os.path.abspath(root)), Path(os.path.abspath(env_file)))
    if runner is not None:
        runner(['systemctl', '--user', 'show', '--property=Version'], timeout=10)
        for name in contents:
            try:
                listed = runner(['systemctl', '--user', 'list-unit-files', name, '--no-legend', '--no-pager'], timeout=10)
            except Unavailable:
                # An absent unit can make enumeration exit 1; show must establish absence.
                listed = None
            if listed is not None and not isinstance(listed, str):
                raise Unavailable()
            if listed is not None and not listed.strip():
                continue
            evidence = runner(['systemctl', '--user', 'show', name, '--property=FragmentPath',
                               '--property=DropInPaths', '--property=LoadState'], timeout=10)
            if not isinstance(evidence, str) or set(evidence.strip().splitlines()) not in (
                    {'FragmentPath=', 'DropInPaths=', 'LoadState=not-found'},
                    {'FragmentPath=' + str(unit_dir / name), 'DropInPaths=', 'LoadState=loaded'}):
                raise Unavailable()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(os.path.abspath(unit_dir)).parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                return
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise Unavailable()
        matching_pair(fd, contents)
    finally:
        os.close(fd)


def install(root, env_file, unit_dir, runner=run):
    root, env_file = Path(os.path.abspath(root)), Path(os.path.abspath(env_file))
    if not (root / 'compose.yaml').is_file() or not env_file.is_file():
        raise Unavailable()
    if not (root / 'scripts/status_observer.py').is_file():
        raise Unavailable()
    contents = units(root, env_file)
    check(root, env_file, unit_dir)
    check(root, env_file, unit_dir, runner)
    with directory(unit_dir, 0o700) as fd:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not matching_pair(fd, contents):
            written = []
            try:
                for name, text in contents.items():
                    handle = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=fd)
                    written.append(name)
                    with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                        stream.write(text)
                        stream.flush()
                        os.fsync(stream.fileno())
                os.fsync(fd)
            except (OSError, UnicodeError):
                for name in written:
                    os.unlink(name, dir_fd=fd)
                os.fsync(fd)
                raise
        # Keep an exact pair after any partial activation; retry revalidates its bytes.
        runner(['systemctl', '--user', 'daemon-reload'], timeout=10)
        for name in contents:
            evidence = runner(['systemctl', '--user', 'show', name, '--property=FragmentPath',
                               '--property=DropInPaths'], timeout=10)
            expected = {'FragmentPath=' + str(unit_dir / name), 'DropInPaths='}
            if not isinstance(evidence, str) or set(evidence.strip().splitlines()) != expected:
                raise Unavailable()
        runner(['systemctl', '--user', 'enable', '--now', NAME + '.timer'], timeout=10)
        runner(['systemctl', '--user', 'is-enabled', NAME + '.timer'], timeout=10)
        runner(['systemctl', '--user', 'is-active', NAME + '.timer'], timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', required=True, type=Path)
    parser.add_argument('--env-file', required=True, type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--install', action='store_true', help='install or resume the exact generated pair')
    action.add_argument('--check', action='store_true', help='check unit custody without writing or activating')
    args = parser.parse_args()
    config = Path(os.environ.get('XDG_CONFIG_HOME', ''))
    if not config.is_absolute():
        config = Path.home() / '.config'
    unit_dir = config / 'systemd/user'
    try:
        if args.check:
            check(args.checkout, args.env_file, unit_dir, run)
            return 0
        install(args.checkout, args.env_file, unit_dir)
    except (OSError, UnicodeError, Unavailable):
        print(f"status timer {'check' if args.check else 'installation'} failed; preserve units and retry the same checkout/env. "
              'Foreign or partial pairs require owning recovery; no units were overwritten.', file=sys.stderr)
        return 1
    print('Status timer enabled. An active user manager with Docker access is required; '
          'enable lingering separately for observation after logout.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
