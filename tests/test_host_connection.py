"""Selected connection and timer recovery. All process and HTTPS calls are fakes."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

import test_installation as fixtures
import installation
import install_status_timer as timer
import tailscale_serve as tailscale
from status_io import Unavailable


class ConnectionRunner(fixtures.FakeRunner):
    def __init__(self):
        super().__init__()
        self.serve = {}
        self.calls = []
        self.deny_serve = False
        self.serve_error = None

    def __call__(self, argv, **options):
        self.calls.append(argv)
        if argv[:3] == ['tailscale', 'serve', 'status']:
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.serve), '')
        if argv[:2] == ['tailscale', 'serve']:
            if self.serve_error and '--https=8447' in argv:
                if isinstance(self.serve_error, Exception):
                    raise self.serve_error
                return subprocess.CompletedProcess(argv, 1, 'private-key' * 1000, self.serve_error)
            if self.deny_serve:
                return subprocess.CompletedProcess(argv, 1, '', 'permission denied')
            port = next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--https='))
            self.serve.setdefault('TCP', {})[port] = {'HTTPS': True}
            self.serve.setdefault('Web', {})['machine.tailnet.ts.net:' + port] = {'Handlers': {'/': {'Proxy': argv[-1]}}}
            return subprocess.CompletedProcess(argv, 0, '', '')
        if argv[:3] == ['docker', 'network', 'inspect']:
            return subprocess.CompletedProcess(argv, 0, json.dumps([{'IPAM': {'Config': [{'Gateway': '172.30.0.1'}]}}]), '')
        result = super().__call__(argv, **options)
        if argv[:2] == ['docker', 'compose'] and 'config' in argv:
            root = Path(argv[argv.index('--project-directory') + 1])
            if root.name == 'platform-edge' and str(root / 'compose.proxy.yaml') in argv:
                rendered = json.loads(result.stdout)
                rendered['services']['caddy']['ports'] = [port for port in rendered['services']['caddy']['ports'] if port['target'] == 80]
                result.stdout = json.dumps(rendered)
        return result


def verified(name, endpoint, **options):
    return 'access_denied' if name == 'protected' else 'reachable'


class HostConnectionTests(unittest.TestCase):
    setUp = fixtures.InstallationTests.setUp
    invoke = fixtures.InstallationTests.invoke
    snapshot = fixtures.InstallationTests.snapshot

    def test_selected_subset_never_accesses_missing_omitted_checkouts(self):
        omitted = [self.host / directory for directory in ('agent-backplane', 'llm-gateway-stack')]
        for root in omitted:
            shutil.rmtree(root)
        original = Path.resolve
        def resolve(path, *args, **kwargs):
            if any(path.is_relative_to(root) for root in omitted):
                raise AssertionError('omitted checkout accessed')
            return original(path, *args, **kwargs)
        runner = ConnectionRunner()
        with patch.object(Path, 'resolve', resolve), patch.object(tailscale, 'verify_application', side_effect=verified):
            code, report = self.invoke('--stack', 'observability', '--tailscale', runner=runner)
        self.assertEqual((code, report['completed']), (0, ['edge', 'observability']))
        self.assertEqual(set(report['connection']['links']), {'Platform Edge', 'grafana'})
        settings = installation.read_settings(self.host / 'observability-stack/.env')
        self.assertEqual(settings['OB_GRAFANA_URL'], 'https://machine.tailnet.ts.net:8447')
        self.assertEqual(settings['OB_TRUSTED_PROXIES'], '172.30.0.2/32')
        self.assertEqual(installation.read_settings(self.root / '.env')['PE_TRUSTED_PROXIES'], '172.30.0.1')
        # Console inclusion uses only the supplied owning selection.
        edge = tailscale.bootstrap.settings_for({})
        status = json.loads(runner(['tailscale', 'status', '--json']).stdout)
        for enabled in ('false', 'true'):
            plan = tailscale.selected_plan(edge, {'observability': {'OB_RUSTFS_CONSOLE': enabled}}, status, {})
            self.assertEqual('observability_rustfs' in plan['endpoints'], enabled == 'true')

    def test_omitted_routes_listeners_and_ports_survive_selected_execution(self):
        env = self.root / '.env'
        env.write_text('PE_TAILSCALE_HOST=machine.tailnet.ts.net\nPE_TAILSCALE_APPS=backplane\nPE_TAILSCALE_BACKPLANE_PORT=9448\n')
        env.chmod(0o600)
        route = self.root / 'routes.d/operator.caddy'
        route.parent.mkdir()
        route.write_text('# independent route\n')
        runner = ConnectionRunner()
        runner.serve = {'TCP': {'9448': {'HTTPS': True}}, 'Web': {
            'machine.tailnet.ts.net:9448': {'Handlers': {'/': {'Proxy': 'http://localhost:80'}, '/custom': {'Text': 'keep'}}}}}
        prior = copy.deepcopy(runner.serve)
        runner.listeners = 'LISTEN 0 128 100.64.0.2:9448 0.0.0.0:*'
        with patch.object(tailscale, 'verify_application', side_effect=verified):
            code, report = self.invoke('--stack', 'observability', '--tailscale', runner=runner)
        self.assertEqual(code, 0, report)
        self.assertEqual(prior['Web']['machine.tailnet.ts.net:9448'], runner.serve['Web']['machine.tailnet.ts.net:9448'])
        self.assertEqual(route.read_text(), '# independent route\n')
        settings = installation.read_settings(env)
        self.assertEqual(settings['PE_TAILSCALE_BACKPLANE_PORT'], '9448')
        self.assertEqual(settings['PE_TAILSCALE_APPS'], 'backplane,grafana')

    def test_matching_serve_is_read_only_and_admin_retry_verifies_https_and_auth(self):
        runner = ConnectionRunner()
        runner.deny_serve = True
        with patch.object(tailscale, 'verify_application', side_effect=verified) as probe:
            code, report = self.invoke('--stack', 'observability', '--tailscale', runner=runner)
            self.assertEqual((code, report['stopped_at']), (3, 'tailscale'))
            self.assertEqual(report['connection']['command'], 'sudo tailscale serve --bg --https=443 --yes http://127.0.0.1:80')
            probe.assert_not_called()
            for port in ('443', '8447', '8450'):
                runner.serve.setdefault('TCP', {})[port] = {'HTTPS': True}
                runner.serve.setdefault('Web', {})['machine.tailnet.ts.net:' + port] = {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:80'}}}
            runner.calls.clear()
            code, report = self.invoke('--stack', 'observability', '--tailscale', runner=runner)
            self.assertEqual((code, report['connection']['state']), (0, 'verified'))
            self.assertFalse(any(argv[:2] == ['tailscale', 'serve'] and 'status' not in argv for argv in runner.calls))
            self.assertTrue(any(call.args[0] == 'protected' and call.args[1]['url'].endswith('/api/user') for call in probe.call_args_list))
            status = json.loads(runner(['tailscale', 'status', '--json']).stdout)
            console = tailscale.plan(status, runner.serve, 8450, 80)
            self.assertTrue(console['matching'])
            runner.calls.clear()
            result = tailscale.connect({'backplane_rustfs': console}, lambda argv: runner(argv).stdout)
            self.assertEqual(result['state'], 'verified')
            self.assertEqual(runner.calls, [['tailscale', 'status', '--json'], ['tailscale', 'serve', 'status', '--json']])
        with patch.object(tailscale, 'verify_application', return_value='reachable'):
            code, report = self.invoke('--stack', 'observability', '--tailscale', runner=runner)
        self.assertEqual((code, report['stopped_at']), (3, 'tailscale'))
        self.assertEqual(report['connection']['state'], 'unverified')

    def test_selected_foreign_ports_handlers_and_funnel_refuse_before_mutation(self):
        cases = [({'AllowFunnel': {'machine.tailnet.ts.net:8447': True}}, ''),
                 ({'TCP': {'8447': {'TCPForward': 'localhost:9000'}}}, ''),
                 ({'TCP': {'8447': {'HTTPS': True}}}, ''),
                 ({'Foreground': {'other': {'TCP': {'8447': {'HTTPS': True}}}}}, ''),
                 ({'Web': {'machine.tailnet.ts.net:8447': {'Handlers': {'/custom': {'Text': 'foreign'}}}}}, ''),
                 ({'Web': {'foreign.ts.net:8447': {'Handlers': {'/': {'Proxy': 'http://localhost:80'}}}}}, ''),
                 ({}, 'LISTEN 0 128 127.0.0.1:8447 0.0.0.0:*'),
                 ({}, 'LISTEN 0 128 0.0.0.0:8447 0.0.0.0:*')]
        before = self.snapshot()
        for serve, listener in cases:
            with self.subTest(serve=serve, listener=listener):
                runner = ConnectionRunner()
                runner.serve, runner.listeners = serve, listener
                code, report = self.invoke('--stack', 'observability', '--tailscale', runner=runner)
                self.assertEqual(code, 1, report)
                self.assertEqual(runner.started, [])
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(any(argv[:2] == ['tailscale', 'serve'] and 'status' not in argv for argv in runner.calls))

    def test_proxy_to_local_qualifies_https_before_writes(self):
        env = self.root / '.env'
        env.write_text('PE_ACCESS_MODE=proxy\nCOMPOSE_FILE=compose.yaml:compose.proxy.yaml\n')
        env.chmod(0o600)
        runner = ConnectionRunner()
        runner.listeners = 'LISTEN 0 128 127.0.0.1:443 0.0.0.0:*'
        before = self.snapshot()
        code, report = self.invoke('--stack', 'edge', '--tailscale', runner=runner)
        self.assertEqual(code, 1, report)
        self.assertIn('port_conflict', {item['code'] for item in report['conflicts']})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(runner.started, [])
        renders = [argv for argv in runner.calls if argv[:2] == ['docker', 'compose'] and 'config' in argv]
        self.assertTrue(renders)
        self.assertTrue(all(str(self.root / 'compose.proxy.yaml') not in argv for argv in renders))
        runner.listeners = ''
        with patch.object(tailscale, 'verify_application', side_effect=verified):
            code, report = self.invoke('--stack', 'edge', '--tailscale', runner=runner)
        self.assertEqual(code, 0, report)
        self.assertEqual(installation.read_settings(env)['PE_ACCESS_MODE'], 'local')
        self.assertNotIn('compose.proxy.yaml', installation.read_settings(env)['COMPOSE_FILE'])

    def test_public_origins_refuse_but_recorded_local_and_tailnet_origins_retry(self):
        env = self.host / 'observability-stack/.env'
        for settings in ('OB_ACCESS_MODE=public\nOB_GRAFANA_URL=https://grafana.company.test\n',
                         'OB_ACCESS_MODE=proxy\nOB_GRAFANA_URL=https://grafana.company.test\n',
                         'OB_SCHEME=https\nOB_PUBLIC_PORT_SUFFIX=:9443\n'):
            with self.subTest(settings=settings):
                env.write_text(settings)
                env.chmod(0o600)
                before = self.snapshot()
                runner = ConnectionRunner()
                code, report = self.invoke('--stack', 'observability', '--tailscale', runner=runner)
                self.assertEqual(code, 1, report)
                self.assertIn('origin_conflict', {item['code'] for item in report['conflicts']})
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(runner.started, [])
        env.unlink()
        runner = ConnectionRunner()
        code, report = self.invoke('--stack', 'observability', runner=runner)
        self.assertEqual(code, 0, report)
        self.assertEqual(installation.read_settings(env)['OB_GRAFANA_URL'], 'http://grafana.localhost')
        with patch.object(tailscale, 'verify_application', side_effect=verified):
            for _ in range(2):
                code, report = self.invoke('--stack', 'observability', '--tailscale', runner=runner)
                self.assertEqual(code, 0, report)
                self.assertEqual(installation.read_settings(env)['OB_GRAFANA_URL'], 'https://machine.tailnet.ts.net:8447')

    def test_effective_backplane_console_requires_exact_peer_and_native_storage(self):
        root = self.host / 'agent-backplane'
        (root / '.env.example').write_text('BP_RUSTFS_CONSOLE=true\n')
        env = root / '.env'
        args = ('--stack', 'backplane', '--tailscale', '--backplane-backup-dir', str(self.backup),
                '--capability-file', str(self.capability))
        for settings, extra in (('BP_TRUSTED_PROXIES=172.30.0.0/16\n', ()), ('', ('--backplane-mode', 'minimal'))):
            env.write_text(settings)
            env.chmod(0o600)
            before = self.snapshot()
            runner = ConnectionRunner()
            code, report = self.invoke(*args, *extra, runner=runner)
            self.assertEqual(code, 1, report)
            self.assertEqual(runner.started, [])
            self.assertEqual(self.snapshot(), before)
        env.write_text('')
        runner = ConnectionRunner()
        with patch.object(tailscale, 'verify_application', side_effect=verified):
            code, report = self.invoke(*args, runner=runner)
        self.assertEqual(code, 0, report)
        self.assertIn('backplane_rustfs', report['connection']['links'])
        self.assertEqual(installation.read_settings(env)['BP_TRUSTED_PROXIES'], '172.30.0.2/32')

    def test_malformed_tailscale_json_refuses_without_mutation_or_raw_diagnostics(self):
        status = {'BackendState': 'Running', 'Self': {'DNSName': 'machine.tailnet.ts.net.'}}
        cases = [(value, {}) for value in ([], None, {'Self': []}, {'Self': {'DNSName': []}},
                 dict(status, Self=dict(status['Self'], TailscaleIPs=[{}])),
                 dict(status, Self=dict(status['Self'], DNSName='private-key\n.ts.net')))]
        cases += [(status, value) for value in ([], False, 0, {'Foreground': {'other': {'TCP': None}}},
                  {'TCP': []}, {'Web': {'machine.tailnet.ts.net:443': {'Handlers': []}}})]
        self.assertEqual(tailscale.serve_status("null"), {})
        self.assertEqual(tailscale.serve_status("{}"), {})
        before = self.snapshot()
        for state, serve in cases:
            with self.subTest(status=state, serve=serve):
                runner = ConnectionRunner()
                def malformed(argv, **options):
                    if argv[:2] == ['tailscale', 'status']:
                        return subprocess.CompletedProcess(argv, 0, json.dumps(state), '')
                    if argv[:3] == ['tailscale', 'serve', 'status']:
                        return subprocess.CompletedProcess(argv, 0, json.dumps(serve), '')
                    return runner(argv, **options)
                code, report = self.invoke('--stack', 'edge', '--tailscale', runner=malformed)
                self.assertEqual(code, 1, report)
                self.assertEqual(runner.started, [])
                self.assertEqual(self.snapshot(), before)
                self.assertNotIn('private-key', json.dumps(report))

    def test_serve_failures_report_safe_cause_command_and_partial_completion(self):
        cases = [('permission denied private-key', 'administrator_action', 'permission'),
                 ('HTTPS is not enabled private-key', 'command_failed', 'certificates'),
                 ('failed to connect to local tailscaled private-key', 'command_failed', 'daemon'),
                 ('unknown private-key' * 1000, 'command_failed', 'failed'),
                 (subprocess.TimeoutExpired(['private-key'], 1, output='private-key'), 'command_failed', 'timed out'),
                 (OSError('private-key'), 'command_failed', 'could not complete')]
        for error, state, detail in cases:
            with self.subTest(error=type(error), state=state, detail=detail):
                runner = ConnectionRunner()
                runner.serve_error = error
                with patch.object(tailscale, 'verify_application') as probe:
                    code, report = self.invoke('--stack', 'observability', '--tailscale', runner=runner)
                self.assertEqual((code, report['stopped_at']), (3, 'tailscale'))
                result = report['connection']
                self.assertEqual((result['state'], result['serve_changes_completed'], result['remaining']), (state, 1, 1))
                self.assertIn(detail, result['detail'])
                self.assertEqual(result['command'], ('sudo ' if state == 'administrator_action' else '') +
                                 'tailscale serve --bg --https=8447 --yes http://127.0.0.1:80')
                self.assertNotIn('private-key', json.dumps(report))
                self.assertLess(len(json.dumps(result)), 800)
                probe.assert_not_called()
                if isinstance(error, str):
                    with patch.object(tailscale.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', error)):
                        with self.assertRaises(tailscale.ServeFailure) as caught:
                            tailscale.checked(['tailscale', 'serve', '--bg', '--https=8447', '--yes', 'http://127.0.0.1:80'])
                    self.assertEqual(caught.exception.permission, state == 'administrator_action')
                    self.assertNotIn('private-key', str(caught.exception))

    def test_exact_timer_pair_retry_recovers_partial_activation_without_rewriting(self):
        root, unit_dir = self.root, self.host / 'units'
        (root / 'scripts/status_observer.py').touch()
        env = root / '.env'
        env.touch(mode=0o600)
        calls = []
        failing = True
        def runner(argv, **options):
            calls.append(argv)
            if failing and 'is-active' in argv:
                raise Unavailable()
            return 'FragmentPath=' + str(unit_dir / argv[3]) + '\nDropInPaths=\n' if 'show' in argv else ''
        with self.assertRaises(Unavailable):
            timer.install(root, env, unit_dir, runner)
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in unit_dir.iterdir()}
        failing = False
        timer.check(root, env, unit_dir, runner)
        timer.install(root, env, unit_dir, runner)
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in unit_dir.iterdir()})
        self.assertEqual(calls[-2:], [['systemctl', '--user', 'is-enabled', timer.NAME + '.timer'],
                                    ['systemctl', '--user', 'is-active', timer.NAME + '.timer']])

    def test_foreign_partial_and_unsafe_timer_selection_refuses_without_writes(self):
        root, unit_dir = self.root, self.host / 'units'
        env = root / '.env'
        env.touch(mode=0o600)
        (root / 'scripts/status_observer.py').touch()
        unit_dir.mkdir(mode=0o700)
        expected = timer.units(root, env)
        for name, contents in expected.items():
            (unit_dir / name).write_text(contents)
            (unit_dir / name).chmod(0o600)
        service = unit_dir / (timer.NAME + '.service')
        original = service.read_text()
        for selection in ('foreign', 'partial', 'permissions', 'symlink'):
            with self.subTest(selection=selection):
                if selection == 'foreign':
                    service.write_text(original.replace(str(env), str(root / 'another.env')))
                elif selection == 'partial':
                    service.unlink()
                elif selection == 'permissions':
                    service.chmod(0o644)
                else:
                    service.unlink()
                    service.symlink_to(root / 'compose.yaml')
                before = {path.name: (path.lstat().st_mode, path.read_bytes() if not path.is_symlink() else str(path.readlink())) for path in unit_dir.iterdir()}
                calls = []
                with self.assertRaises(Unavailable):
                    timer.install(root, env, unit_dir, lambda *args, **kwargs: calls.append(args))
                self.assertEqual(calls, [])
                self.assertEqual(before, {path.name: (path.lstat().st_mode, path.read_bytes() if not path.is_symlink() else str(path.readlink())) for path in unit_dir.iterdir()})
                service.unlink(missing_ok=True)
                service.write_text(original)
                service.chmod(0o600)
        with self.assertRaises(Unavailable):
            timer.check(root, env, unit_dir, lambda *args, **kwargs: 'FragmentPath=/foreign.service\nDropInPaths=\n')
        absent = self.host / 'absent-units'
        with self.assertRaises(Unavailable):
            timer.install(root, env, absent, lambda *args, **kwargs: 'FragmentPath=/foreign.service\nDropInPaths=\n')
        self.assertFalse(absent.exists())
        before = self.snapshot()
        runner = ConnectionRunner()
        code, report = self.invoke('--stack', 'observability', '--status-timers', runner=runner)
        self.assertEqual(code, 1)
        self.assertEqual({error['code'] for error in report['conflicts']}, {'timer_preflight'})
        self.assertEqual(runner.started, [])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any('llm-gateway-stack' in str(argv) or 'agent-backplane' in str(argv) for argv in runner.calls))
