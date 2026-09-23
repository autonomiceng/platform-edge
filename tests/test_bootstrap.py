"""Bootstrap contract; the runner never calls Docker."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("bootstrap", ROOT / "scripts/bootstrap.py")
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class CommandDeadlineTests(unittest.TestCase):
    def test_bootstrap_timeout_is_sanitized_and_startup_has_a_larger_budget(self):
        for argv, budget in [(["docker", "info"], 120), (["docker", "compose", "up", "--wait"], 360)]:
            with self.subTest(argv=argv), patch.object(bootstrap, "run_detached", side_effect=subprocess.TimeoutExpired(argv, budget, stderr="private-token")) as child:
                with self.assertRaises(bootstrap.Refused) as caught:
                    bootstrap.run(argv)
                self.assertEqual(caught.exception.code, "docker_timeout")
                self.assertNotIn("private-token", str(caught.exception))
                self.assertEqual(child.call_args.kwargs["timeout"], budget)


CONTRACT_IPAM = [{"Subnet": "172.30.0.0/24", "IPRange": "172.30.0.128/25", "Gateway": "172.30.0.1"}]


class FakeRunner:
    def __init__(self, *, containers=(), network_exists=True, ipam=CONTRACT_IPAM, create_fails=False):
        self.containers = containers
        self.network_exists = network_exists
        self.ipam = ipam
        self.create_fails = create_fails
        self.calls = []

    def __call__(self, argv, **options):
        self.calls.append(argv)
        if argv == ["docker", "ps", "--format", "json"]:
            return subprocess.CompletedProcess(argv, 0, "\n".join(map(json.dumps, self.containers)), "")
        if argv[:3] == ["docker", "network", "inspect"]:
            if not self.network_exists:
                return subprocess.CompletedProcess(argv, 1, "", "No such network")
            output = json.dumps(self.ipam if "--format" in argv else [{"IPAM": {"Config": self.ipam}}])
            return subprocess.CompletedProcess(argv, 0, output, "")
        if argv[:3] == ["docker", "network", "create"]:
            # A concurrent sibling bootstrap wins the race; inspect then succeeds.
            self.network_exists = True
            return subprocess.CompletedProcess(argv, 1 if self.create_fails else 0, "", "already exists" if self.create_fails else "")
        if "run" in argv:
            return subprocess.CompletedProcess(argv, 0, "clean\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class BootstrapTests(unittest.TestCase):
    def test_state_check_distinguishes_restore_marker_from_execution_failure(self):
        for code, output, expected in ((1, "", "state_check_failed"),
                                       (0, "", "state_check_failed"),
                                       (1, "clean\n", "state_check_failed"),
                                       (0, "compose message\nmarker\n", "restore_incomplete")):
            with self.subTest(code=code, output=output), tempfile.TemporaryDirectory() as directory:
                calls = []
                def runner(argv):
                    calls.append(argv)
                    if "run" in argv:
                        isolated = Path(argv[argv.index("run") - 1]).read_text()
                        self.assertIn("networks: !reset []", isolated)
                        self.assertIn("network_mode: none", isolated)
                        return subprocess.CompletedProcess(argv, code, output, "daemon failure" if code else "")
                    if argv[:3] == ["docker", "network", "inspect"]:
                        return subprocess.CompletedProcess(argv, 0, json.dumps(CONTRACT_IPAM), "")
                    return subprocess.CompletedProcess(argv, 0, "", "")
                with patch.dict(os.environ, {}, clear=True), \
                        patch.object(bootstrap.shutil, "which", return_value="docker"), \
                        patch.object(bootstrap, "wait_ready", return_value={}), \
                        self.assertRaises(bootstrap.Refused) as caught:
                    bootstrap.bootstrap(["--env-file", str(Path(directory) / ".env")], runner)
                self.assertEqual(caught.exception.code, expected)
                self.assertFalse(any("up" in argv for argv in calls))
                if expected == "state_check_failed":
                    self.assertNotIn("fresh prefix", caught.exception.detail)

    def test_render_writes_only_env(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env = Path(directory) / ".env"
            runner = FakeRunner()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(bootstrap.bootstrap(["--env-file", str(env), "--render-only"], runner), 0)
                original = env.read_bytes()
                self.assertEqual(bootstrap.bootstrap(["--env-file", str(env), "--render-only"], runner), 0)
            self.assertEqual(original, (ROOT / ".env.example").read_bytes())
            self.assertEqual(env.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [env])
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)
            self.assertEqual(bootstrap.read_env(ROOT / ".env.example")["PE_CADDY_IMAGE"], "")
            edited = original.replace(b"PE_CADDY_IMAGE=\n", b"PE_CADDY_IMAGE=local/edge:experiment\n") + b"\r\n# Kept verbatim\r\nCUSTOM_SETTING=value\r\n"
            env.write_bytes(edited)
            with contextlib.redirect_stdout(io.StringIO()):
                bootstrap.bootstrap(["--env-file", str(env), "--render-only"], runner)
            self.assertEqual(env.read_bytes(), edited)
            self.assertEqual(bootstrap.read_env(env)["PE_CADDY_IMAGE"], "local/edge:experiment")
            env.write_text('PE_TRUSTED_PROXIES="192.0.2.1 2001:db8::1"\n')
            self.assertEqual(bootstrap.read_env(env)["PE_TRUSTED_PROXIES"], "192.0.2.1 2001:db8::1")
            self.assertEqual(runner.calls, [])
            self.assertEqual(json.loads(output.getvalue().splitlines()[0])["generated"], [])

    def test_port_conflict_refuses_before_start_and_names_container(self):
        for ports in ("0.0.0.0:80->80/tcp", "[::]:443->443/tcp", ":::80-81->80-81/tcp"):
            with self.subTest(ports=ports), tempfile.TemporaryDirectory() as directory:
                runner = FakeRunner(containers=[{"ID": "abc123", "Names": "old-gateway", "Ports": ports, "Labels": ""}])
                with patch.dict(os.environ, {}, clear=True), patch.object(bootstrap.shutil, "which", return_value="docker"):
                    with self.assertRaises(bootstrap.Refused) as caught:
                        bootstrap.bootstrap(["--env-file", str(Path(directory) / ".env")], runner)
                self.assertEqual(caught.exception.code, "port_conflict")
                self.assertIn("old-gateway", caught.exception.detail)
                self.assertEqual(runner.calls, [["docker", "ps", "--format", "json"]])
        own = FakeRunner(containers=[{"ID": "edge", "Names": "edge-caddy", "Ports": "0.0.0.0:80->80/tcp",
                                      "Labels": "com.docker.compose.project=platform-edge,com.docker.compose.service=caddy"}])
        bootstrap.check_ports(own, bootstrap.DEFAULTS, bootstrap.PROJECT)

    def start(self, runner, environment, directory):
        output = io.StringIO()
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(bootstrap.shutil, "which", return_value="docker"), \
                patch.object(bootstrap, "wait_ready", return_value={}), \
                patch.object(bootstrap, "directory", return_value=contextlib.nullcontext()), \
                patch.object(bootstrap, "task_record"), contextlib.redirect_stdout(output):
            return bootstrap.bootstrap(["--env-file", str(Path(directory) / ".env")], runner)

    def test_missing_network_is_created_with_the_contract_allocation(self):
        for environment, expected in ((
                {}, ["--subnet", "172.30.0.0/24", "--ip-range", "172.30.0.128/25", "--gateway", "172.30.0.1", "isolated"]), (
                {"PE_PLATFORM_SUBNET": "10.9.0.0/16", "PE_PLATFORM_IP_RANGE": "10.9.128.0/17", "PE_EDGE_IP": "10.9.0.2"},
                ["--subnet", "10.9.0.0/16", "--ip-range", "10.9.128.0/17", "--gateway", "10.9.0.1", "isolated"])):
            with self.subTest(environment=environment), tempfile.TemporaryDirectory() as directory:
                ipam = [{"Subnet": expected[1], "IPRange": expected[3], "Gateway": expected[5]}]
                runner = FakeRunner(network_exists=False, ipam=ipam)
                self.assertEqual(self.start(runner, dict(environment, PE_PLATFORM_NETWORK="isolated"), directory), 0)
                creates = [c for c in runner.calls if c[:3] == ["docker", "network", "create"]]
                self.assertEqual(creates, [["docker", "network", "create", "--driver", "bridge"] + expected])
                probe = ["docker", "network", "inspect", "--format", "{{json .IPAM.Config}}", "isolated"]
                self.assertEqual([c for c in runner.calls if c[:3] == ["docker", "network", "inspect"]], [probe, probe])
                self.assertTrue(any("up" in call and "--wait" in call for call in runner.calls))

    def test_existing_network_with_matching_allocation_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner(ipam=[{"Subnet": "172.30.0.0/24", "IPRange": "172.30.0.128/25", "Gateway": "172.30.0.1"}])
            self.assertEqual(self.start(runner, {"PE_PLATFORM_NETWORK": "isolated"}, directory), 0)
            self.assertFalse([c for c in runner.calls if c[:3] == ["docker", "network", "create"]])
            self.assertTrue(any("up" in call and "--wait" in call for call in runner.calls))

    def test_existing_network_with_different_allocation_is_refused_with_both_values(self):
        for ipam, observed in (([{"Subnet": "172.18.0.0/16", "Gateway": "172.18.0.1"}], "172.18.0.0/16"),
                               ([{"Subnet": "172.30.0.0/24", "IPRange": "172.30.0.0/25", "Gateway": "172.30.0.1"}], "172.30.0.0/25"),
                               ([{"Subnet": "172.30.0.0/24", "IPRange": "172.30.0.128/25", "Gateway": "172.30.0.2"}], "gateway 172.30.0.2"),
                               ([{"Subnet": "172.30.0.0/24", "IPRange": "172.30.0.128/25"}], "gateway none"),
                               ([], "none"), (None, "none")):
            with self.subTest(ipam=ipam), tempfile.TemporaryDirectory() as directory:
                runner = FakeRunner(ipam=ipam)
                with self.assertRaises(bootstrap.Refused) as caught:
                    self.start(runner, {"PE_PLATFORM_NETWORK": "isolated"}, directory)
                self.assertEqual(caught.exception.code, "platform_network_mismatch")
                for text in (observed, "172.30.0.0/24", "172.30.0.128/25", "gateway 172.30.0.1", "docker network rm isolated"):
                    self.assertIn(text, caught.exception.detail)
                self.assertFalse(any("up" in call for call in runner.calls))

    def test_lost_creation_race_validates_the_winner_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner(network_exists=False, create_fails=True)
            self.assertEqual(self.start(runner, {"PE_PLATFORM_NETWORK": "isolated"}, directory), 0)
            self.assertEqual(len([c for c in runner.calls if c[:3] == ["docker", "network", "create"]]), 1)
            self.assertTrue(any("up" in call and "--wait" in call for call in runner.calls))
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner(network_exists=False, create_fails=True, ipam=[{"Subnet": "172.18.0.0/16"}])
            with self.assertRaises(bootstrap.Refused) as caught:
                self.start(runner, {"PE_PLATFORM_NETWORK": "isolated"}, directory)
            self.assertEqual(caught.exception.code, "platform_network_mismatch")

    def test_allocation_settings_and_retired_peer_pinning_are_validated(self):
        with patch.dict(os.environ, {}, clear=True):
            bootstrap.settings_for({"PE_PLATFORM_SUBNET": "10.9.0.0/16", "PE_PLATFORM_IP_RANGE": "10.9.128.0/17", "PE_EDGE_IP": "10.9.0.2"})
            bootstrap.settings_for({"PE_TAILSCALE_EDGE_IP": "172.30.0.2"})
            for invalid in ({"PE_EDGE_IP": "172.30.0.200"}, {"PE_EDGE_IP": "172.30.0.1"}, {"PE_EDGE_IP": "172.30.1.2"},
                            {"PE_EDGE_IP": "172.30.0.0"}, {"PE_EDGE_IP": "172.30.0.255"}, {"PE_EDGE_IP": "edge"},
                            {"PE_PLATFORM_IP_RANGE": "172.31.0.0/25"}, {"PE_PLATFORM_SUBNET": "172.30.0.0/25"},
                            {"PE_PLATFORM_SUBNET": "172.30.0.5/24"}, {"PE_PLATFORM_SUBNET": "fd00::/64"}):
                with self.subTest(invalid=invalid), self.assertRaises(bootstrap.Refused) as caught:
                    bootstrap.settings_for(invalid)
                self.assertEqual(caught.exception.code, "invalid_settings")
            with self.assertRaises(bootstrap.Refused) as caught:
                bootstrap.settings_for({"PE_TAILSCALE_EDGE_IP": "172.18.0.7"})
            self.assertEqual(caught.exception.code, "legacy_setting")
            self.assertIn("172.30.0.2", caught.exception.detail)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env = Path(directory) / ".env"
            env.write_text("# keep\nCOMPOSE_FILE='compose.yaml:custom.yaml:compose.tailscale.yaml'\nPE_TAILSCALE_EDGE_IP=\nCUSTOM=1\n")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as log:
                self.assertEqual(bootstrap.bootstrap(["--env-file", str(env), "--render-only"], FakeRunner()), 0)
            self.assertEqual(env.read_text(), "# keep\nCOMPOSE_FILE='compose.yaml:custom.yaml'\nPE_TAILSCALE_EDGE_IP=\nCUSTOM=1\n")
            self.assertIn("compose.tailscale.yaml", log.getvalue())
            self.assertEqual(bootstrap.compose_command(ROOT, env)[-4:], ["-f", str(ROOT / "compose.yaml"), "-f", str(ROOT / "custom.yaml")])
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as log:
                self.assertEqual(bootstrap.bootstrap(["--env-file", str(env), "--render-only"], FakeRunner()), 0)
            self.assertEqual(log.getvalue(), "")
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"COMPOSE_FILE": "compose.yaml:compose.tailscale.yaml"}, clear=True), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(bootstrap.Refused) as caught:
            bootstrap.bootstrap(["--env-file", str(Path(directory) / ".env"), "--render-only"], FakeRunner())
        self.assertEqual(caught.exception.code, "legacy_setting")

    def test_network_created_only_when_missing(self):
        for exists in (True, False):
            with self.subTest(exists=exists), tempfile.TemporaryDirectory() as directory:
                runner = FakeRunner(network_exists=exists)
                output = io.StringIO()
                with patch.dict(os.environ, {"PE_PLATFORM_NETWORK": "isolated", "PE_HTTP_PORT": "18280"}, clear=True), \
                        patch.object(bootstrap.shutil, "which", return_value="docker"), \
                        patch.object(bootstrap, "wait_ready", return_value={}) as ready, \
                        patch.object(bootstrap, "directory", return_value=contextlib.nullcontext()), \
                        patch.object(bootstrap, "task_record") as recorded, contextlib.redirect_stdout(output):
                    bootstrap.bootstrap(["--env-file", str(Path(directory) / ".env")], runner)
                creates = [c for c in runner.calls if c[:3] == ["docker", "network", "create"]]
                self.assertEqual(len(creates), 0 if exists else 1)
                self.assertIn(["docker", "network", "inspect", "--format", "{{json .IPAM.Config}}", "isolated"], runner.calls)
                self.assertTrue(any("up" in call and "--wait" in call for call in runner.calls))
                self.assertTrue(any(str(ROOT / "scripts/status_observer.py") in call for call in runner.calls))
                self.assertEqual([call.args[3] for call in recorded.call_args_list], ["unknown", "healthy"])
                ready.assert_called_once()
                self.assertEqual(ready.call_args.args[0]["PE_HTTP_PORT"], "18280")
                self.assertEqual(ready.call_args.args[0]["PE_PUBLIC_DOMAIN"], "localhost")
                self.assertEqual(len(json.loads(output.getvalue())["hostnames"]), 7)

    def test_status_timeout_preserves_successful_bootstrap(self):
        base = FakeRunner()
        def runner(argv, **options):
            if str(ROOT / 'scripts/status_observer.py') in argv:
                self.assertEqual(options, {'timeout': 120})
                raise bootstrap.Refused('docker_timeout', 'private diagnostic')
            return base(argv)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), \
                patch.object(bootstrap.shutil, 'which', return_value='docker'), \
                patch.object(bootstrap, 'directory', return_value=contextlib.nullcontext()), \
                patch.object(bootstrap, 'task_record'), patch.object(bootstrap, 'wait_ready', return_value={}), \
                contextlib.redirect_stderr(io.StringIO()) as warning, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap.bootstrap(['--env-file', str(Path(directory) / '.env')], runner), 0)
        self.assertIn('Status observation failed', warning.getvalue())
        self.assertNotIn('private diagnostic', warning.getvalue())

    def test_status_permissions_do_not_mask_bootstrap_readiness(self):
        for failure in (None, bootstrap.Refused('not_ready', 'original readiness failure')):
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), \
                    patch.object(bootstrap.shutil, 'which', return_value='docker'), \
                    patch.object(bootstrap, 'directory', side_effect=bootstrap.Unavailable()), \
                    patch.object(bootstrap, 'wait_ready', side_effect=failure, return_value={}), \
                    contextlib.redirect_stderr(io.StringIO()) as warning, contextlib.redirect_stdout(io.StringIO()):
                if failure:
                    with self.assertRaises(bootstrap.Refused) as raised:
                        bootstrap.bootstrap(['--env-file', str(Path(directory) / '.env')], FakeRunner())
                    self.assertIs(raised.exception, failure)
                else:
                    self.assertEqual(bootstrap.bootstrap(['--env-file', str(Path(directory) / '.env')], FakeRunner()), 0)
                self.assertIn('Status execution record unavailable', warning.getvalue())

    def test_hostnames_come_from_domain_and_route_files(self):
        expected = ["example.test", "litellm.example.test", "langfuse.example.test", "s3.example.test",
                    "backplane.example.test", "grafana.example.test", "rustfs.example.test"]
        self.assertEqual(bootstrap.routed_hostnames(ROOT / "routes.d", "example.test"), sorted(expected))
        with tempfile.TemporaryDirectory() as directory:
            routes = Path(directory)
            (routes / "custom.caddy").write_text('http://custom.{$PE_PUBLIC_DOMAIN} {\n}\n')
            self.assertEqual(bootstrap.routed_hostnames(routes, "other.test"), ["custom.other.test"])


class AccessModeTests(unittest.TestCase):
    def test_modes_derive_issuers_and_schemes(self):
        with patch.dict(os.environ, {}, clear=True):
            for mode, scheme, issuer in [("local", "http", "internal"), ("public", "https", "acme"),
                                         ("proxy", "https", "none")]:
                settings = bootstrap.settings_for({"PE_ACCESS_MODE": mode, "PE_PUBLIC_DOMAIN": "example.com"})
                self.assertEqual((settings["PE_SCHEME"], settings["PE_TLS_ISSUER"]), (scheme, issuer))
            for invalid in ({"PE_ACCESS_MODE": "typo"}, {"PE_PUBLIC_DOMAIN": "pe-edge"}):
                with self.assertRaises(bootstrap.Refused):
                    bootstrap.settings_for(invalid)

    def test_proxy_does_not_claim_https_port_and_selects_override(self):
        settings = dict(bootstrap.DEFAULTS, PE_ACCESS_MODE="proxy")
        runner = FakeRunner(containers=[{"Names": "other", "ID": "other", "Ports": "0.0.0.0:443->443/tcp"}])
        bootstrap.check_ports(runner, settings, "platform-edge")
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env = Path(directory) / ".env"
            env.write_text("PE_ACCESS_MODE=proxy\n")
            self.assertEqual(bootstrap.compose_command(ROOT, env)[-4:],
                             ["-f", str(ROOT / "compose.yaml"), "-f", str(ROOT / "compose.proxy.yaml")])

    def test_public_empty_scheme_selects_https_compose_default(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env = Path(directory) / ".env"
            env.write_text("PE_ACCESS_MODE=public\nPE_PUBLIC_DOMAIN=example.com\nPE_SCHEME=\n")
            self.assertEqual(bootstrap.settings_for(bootstrap.read_env(env))["PE_SCHEME"], "https")
            self.assertEqual(bootstrap.compose_command(ROOT, env)[-4:],
                             ["-f", str(ROOT / "compose.yaml"), "-f", str(ROOT / "compose.public.yaml")])

    def test_proxy_allows_matching_ports_but_compose_still_requires_port_syntax(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = bootstrap.settings_for({"PE_ACCESS_MODE": "proxy", "PE_HTTP_PORT": "443"})
            self.assertEqual(settings["PE_HTTP_PORT"], settings["PE_HTTPS_PORT"])
            for values in ({"PE_HTTP_PORT": "443"},
                           {"PE_ACCESS_MODE": "proxy", "PE_HTTPS_PORT": "invalid"}):
                with self.assertRaises(bootstrap.Refused):
                    bootstrap.settings_for(values)

    def test_local_readiness_checks_both_protocols_before_ready(self):
        settings = dict(bootstrap.DEFAULTS, PE_ACCESS_MODE="local", PE_TLS_ISSUER="internal")
        def runner(argv):
            return subprocess.CompletedProcess(argv, 0, "root", "")
        certificate = {"not_after_seconds": 2000000000}
        with patch.object(bootstrap, "probe", side_effect=[{}, certificate]) as probe, \
                patch.object(bootstrap, "ca_fingerprint", return_value="fingerprint"):
            result = bootstrap.wait_ready(settings, ROOT, ROOT / "absent-env", runner)
        self.assertEqual([c.args[0]["PE_SCHEME"] for c in probe.call_args_list], ["http", "https"])
        self.assertEqual(result["ca_sha256"], "fingerprint")


if __name__ == "__main__":
    unittest.main()
