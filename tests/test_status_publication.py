"""Host I/O safety, bounded subprocesses and opt-in scheduler installation."""

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import install_status_timer as installer
import status_io as io
AT = "2026-09-20T12:00:00Z"


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_existing_restrictive_directory_is_preserved_and_publication_is_atomic(self):
        target = self.root / 'console'
        target.mkdir(mode=0o700)
        with io.directory(target) as fd:
            io.publish(fd, 'status.json', {'first': True})
            with patch.object(io.os, 'replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    io.publish(fd, 'status.json', {'second': True})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        self.assertEqual(json.loads((target / 'status.json').read_text()), {'first': True})
        self.assertEqual([p.name for p in target.iterdir()], ['status.json'])
        state = self.root / 'private-state'
        mask = os.umask(0o077)
        try:
            with io.directory(state / 'status', 0o700):
                pass
            with io.directory(state / 'console') as fd:
                io.publish(fd, 'status.json', {'public': True})
        finally:
            os.umask(mask)
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((state / 'status').stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((state / 'console').stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((state / 'console/status.json').stat().st_mode), 0o644)
        with self.assertRaises(io.Unavailable):
            with io.directory(state / 'writable', 0o775):
                pass
        self.assertFalse((state / 'writable').exists())


    def test_symlink_directory_and_file_are_refused_without_touching_target(self):
        real = self.root / 'real'
        real.mkdir(mode=0o755)
        link = self.root / 'link'
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(OSError):
            with io.directory(link / 'nested'):
                pass
        self.assertFalse((real / 'nested').exists())
        private = self.root / 'private'
        private.write_text('credentials')
        (real / 'status.json').symlink_to(private)
        with io.directory(real) as fd, self.assertRaises(io.Unavailable):
            io.publish(fd, 'status.json', {'public': True})
        self.assertEqual(private.read_text(), 'credentials')

    def test_fifo_and_hardlink_and_writable_public_directory_are_refused(self):
        directory = self.root / 'console'
        directory.mkdir(mode=0o755)
        target = directory / 'status.json'
        os.mkfifo(target)
        with io.directory(directory) as fd, self.assertRaises(io.Unavailable):
            io.publish(fd, 'status.json', {})
        target.unlink()
        other = self.root / 'other'
        other.write_text('private')
        target.hardlink_to(other)
        with io.directory(directory) as fd, self.assertRaises(io.Unavailable):
            io.publish(fd, 'status.json', {})
        directory.chmod(0o777)
        with self.assertRaises(io.Unavailable):
            with io.directory(directory):
                pass

    def test_bad_json_and_size_limits_do_not_publish_a_partial_document(self):
        for text in ('{"a":1,"a":2}', 'NaN', '1e999', '-1e999', '{broken', '[' * 5000, '"' + 'x' * 65536 + '"'):
            with self.subTest(text=text[:20]), self.assertRaises(io.Unavailable):
                io.read_json(text)
        with io.directory(self.root) as fd:
            io.publish(fd, 'status.json', {'original': True})
            with self.assertRaises(io.Unavailable):
                io.publish(fd, 'status.json', {'large': 'x' * 65536})
        self.assertEqual(json.loads((self.root / 'status.json').read_text()), {'original': True})

    def test_timeout_stdout_stderr_and_exit_failures_are_bounded_and_silent(self):
        for script, options in (
            ('import time; time.sleep(20)', {'timeout': 0.1}),
            ('print("x" * 100000)', {'limit': 1000}),
            ('import sys; sys.stderr.write("secret" * 100000)', {'limit': 1000}),
            ('raise SystemExit(1)', {}),
            ('import os; os.write(1, b"\\xff")', {}),
        ):
            started = time.monotonic()
            with self.subTest(script=script), self.assertRaises(io.Unavailable) as raised:
                io.run([sys.executable, '-c', script], **options)
            self.assertEqual(str(raised.exception), '')
            self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(io.run([sys.executable, '-c', 'print("ok")']), 'ok\n')
        for code in (125, 126, 127):
            with self.subTest(code=code), self.assertRaises(io.Unsupported):
                io.run([sys.executable, '-c', f'raise SystemExit({code})'])
        with self.assertRaises(io.Unsupported):
            io.run([str(self.root / 'missing-executable')])

    def test_deadline_stops_descendant_after_direct_child_exits(self):
        marker = self.root / 'descendant-heartbeat'
        child = "import pathlib,time; p=pathlib.Path(" + repr(str(marker)) + "); " + "\nfor i in range(200): p.write_text(str(i)); time.sleep(.02)"
        parent = "import subprocess,sys; subprocess.Popen([sys.executable, '-c', " + repr(child) + "])"
        with self.assertRaises(io.Unavailable):
            io.run([sys.executable, '-c', parent], timeout=.3)
        self.assertTrue(marker.exists(), 'descendant must execute before the deadline')
        before = marker.read_text()
        time.sleep(.15)
        self.assertEqual(marker.read_text(), before, 'descendant continued after probe cleanup')

    def test_task_records_are_private_and_malformed_records_do_not_break_siblings(self):
        env = self.root / '.env'
        io.task_record(self.root, env, AT, 'healthy')
        record = self.root / 'data/status/bootstrap.json'
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(record.parent.stat().st_mode), 0o700)
        self.assertEqual(io.read_task(self.root, env)['lastExecutionAt'], AT)
        record.write_bytes(b'\xff')
        with self.assertRaises(io.Unavailable):
            io.read_task(self.root, env)

    def test_installer_selects_explicit_paths_and_is_reversible_without_overwriting(self):
        root = self.root / 'checkout café with $money% and "quotes"'
        (root / 'scripts').mkdir(parents=True)
        (root / 'scripts/status_observer.py').touch()
        (root / 'compose.yaml').touch()
        env = root / 'selected.env'
        env.touch()
        unit_dir = self.root / 'units'
        calls = []
        def runner(argv, **_):
            calls.append(argv)
            return 'FragmentPath=' + str(unit_dir / argv[3]) + '\nDropInPaths=\n' if 'show' in argv else ''
        installer.install(root, env, unit_dir, runner)
        self.assertIn(['systemctl', '--user', 'enable', '--now', 'platform-edge-status.timer'], calls)
        self.assertEqual(calls[-1], ['systemctl', '--user', 'is-active', 'platform-edge-status.timer'])
        text = (unit_dir / 'platform-edge-status.service').read_text()
        self.assertIn('$$money%%', text)
        self.assertIn('\\"quotes\\"', text)
        self.assertIn('--env-file', text)
        self.assertIn('selected.env', text)
        self.assertNotIn('EnvironmentFile', text)
        self.assertIn('OnUnitInactiveSec=30s', (unit_dir / 'platform-edge-status.timer').read_text())
        calls.clear()
        before = {path: path.stat().st_mtime_ns for path in unit_dir.iterdir()}
        installer.install(root, env, unit_dir, runner)
        self.assertEqual(before, {path: path.stat().st_mtime_ns for path in unit_dir.iterdir()})
        for value in ('path\nExecStart=bad', 'path\x00bad'):
            with self.assertRaises(io.Unavailable):
                installer.quote(value)

    def test_partial_unit_write_rolls_back_without_enabling_timer(self):
        (self.root / 'scripts').mkdir()
        (self.root / 'scripts/status_observer.py').touch()
        (self.root / 'compose.yaml').touch()
        env = self.root / '.env'
        env.touch()
        unit_dir = self.root / 'units'
        original = os.open
        def opened(path, *args, **kwargs):
            if path == installer.NAME + '.timer':
                raise OSError('injected second write failure')
            return original(path, *args, **kwargs)
        calls = []
        with patch.object(installer.os, 'open', side_effect=opened), self.assertRaises(OSError):
            installer.install(self.root, env, unit_dir, lambda *args, **kwargs: calls.append(args) or '')
        self.assertEqual(list(unit_dir.iterdir()), [])
        self.assertTrue(all(call[0][2] in ('show', 'list-unit-files') for call in calls))

    def test_activation_failure_retains_units_for_exact_pair_recovery(self):
        (self.root / 'scripts').mkdir()
        (self.root / 'scripts/status_observer.py').touch()
        (self.root / 'compose.yaml').touch()
        env = self.root / '.env'
        env.touch()
        unit_dir = self.root / 'units'

        def fail_enable(argv, **_):
            if 'enable' in argv:
                raise io.Unavailable()
            return 'FragmentPath=' + str(unit_dir / argv[3]) + '\nDropInPaths=\n' if 'show' in argv else ''

        with self.assertRaises(io.Unavailable):
            installer.install(self.root, env, unit_dir, fail_enable)
        self.assertEqual(sorted(path.name for path in unit_dir.iterdir()), [
            'platform-edge-status.service', 'platform-edge-status.timer'])
        with self.assertRaises(io.Unavailable):
            installer.install(self.root, env, unit_dir, lambda *_args, **_kwargs: None)

    def test_installer_reports_unicode_failures_without_traceback(self):
        argv = ['installer', '--checkout', str(self.root), '--env-file', str(self.root / '.env'), '--install']
        with patch.object(sys, 'argv', argv), patch.object(installer, 'install', side_effect=UnicodeError()), \
                patch('builtins.print') as printed:
            self.assertEqual(installer.main(), 1)
        self.assertIn('preserve units', printed.call_args.args[0])

    def test_xdg_relative_and_empty_values_use_home_config(self):
        argv = ['installer', '--checkout', str(self.root), '--env-file', str(self.root / '.env'), '--install']
        for value in ('', 'relative', str(self.root / 'absolute')):
            with patch.dict(os.environ, {'XDG_CONFIG_HOME': value}), patch.object(sys, 'argv', argv), \
                    patch.object(installer, 'install') as install, patch('builtins.print'):
                self.assertEqual(installer.main(), 0)
            expected = Path(value) if Path(value).is_absolute() else Path.home() / '.config'
            self.assertEqual(install.call_args.args[2], expected / 'systemd/user')


if __name__ == '__main__':
    unittest.main()
    def test_absent_unit_enumeration_falls_back_to_show_without_writes(self):
        (self.root / 'scripts').mkdir()
        (self.root / 'scripts/status_observer.py').touch()
        (self.root / 'compose.yaml').touch()
        env = self.root / '.env'
        unit_dir = self.root / 'config/systemd/user'
        calls = []
        evidence = 'LoadState=not-found\nFragmentPath=\nDropInPaths=\n'
        failure = ''

        def runner(argv, **options):
            calls.append(argv)
            if argv[2:] == ['show', '--property=Version']:
                if failure == 'manager':
                    raise io.Unavailable()
                return 'Version=255\n'
            if argv[2] == 'list-unit-files':
                # Actual absent-unit exit 1, empty stdout/stderr, mapped by status_io.run.
                raise io.Unavailable()
            if argv[2] == 'show':
                if failure == 'unit-show':
                    raise io.Unavailable()
                if (unit_dir / argv[3]).is_file():
                    return ('FragmentPath=' + str(unit_dir / argv[3]) + '\nDropInPaths=\n' +
                            ('LoadState=loaded\n' if '--property=LoadState' in argv else ''))
                return evidence
            if argv[2] in ('daemon-reload', 'enable', 'is-enabled', 'is-active'):
                return ''
            raise AssertionError(argv)

        installer.check(self.root, env, unit_dir, runner)
        self.assertFalse(env.exists())
        self.assertFalse(unit_dir.parent.parent.exists())
        for suffix in ('.service', '.timer'):
            self.assertTrue(any(call[2:4] == ['show', installer.NAME + suffix]
                                and '--property=LoadState' in call for call in calls))
        env.touch(mode=0o600)
        absent = evidence
        for evidence, failure in ((absent, 'manager'), (absent, 'unit-show'),
                                  ('LoadState=loaded\nFragmentPath=/foreign.service\nDropInPaths=\n', ''),
                                  (absent.replace('DropInPaths=', 'DropInPaths=/run/override.conf'), '')):
            with self.subTest(evidence=evidence, failure=failure):
                before = {str(path): (path.lstat().st_mode, path.lstat().st_mtime_ns) for path in self.root.rglob('*')}
                for action in (installer.check, installer.install):
                    with self.assertRaises(io.Unavailable):
                        action(self.root, env, unit_dir, runner)
                    self.assertEqual(before, {str(path): (path.lstat().st_mode, path.lstat().st_mtime_ns) for path in self.root.rglob('*')})
        evidence, failure = absent, ''
        installer.install(self.root, env, unit_dir, runner)
        self.assertEqual({path.name for path in unit_dir.iterdir()},
                         {installer.NAME + '.service', installer.NAME + '.timer'})

    def test_timer_failure_diagnostic_names_requested_action(self):
        for flag, action, diagnostic in (('--check', 'check', 'check'), ('--install', 'install', 'installation')):
            argv = ['install_status_timer.py', flag, '--checkout', str(self.root), '--env-file', str(self.root / '.env')]
            with self.subTest(flag=flag), patch.object(sys, 'argv', argv), \
                    patch.object(installer, action, side_effect=io.Unavailable()), patch('builtins.print') as printed:
                self.assertEqual(installer.main(), 1)
            self.assertTrue(printed.call_args.args[0].startswith('status timer ' + diagnostic + ' failed;'))
