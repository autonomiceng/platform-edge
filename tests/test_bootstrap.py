"""Bootstrap contract; the runner never calls Docker."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
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


class FakeRunner:
    def __init__(self, *, containers=(), network_exists=True):
        self.containers = containers
        self.network_exists = network_exists
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv == ["docker", "ps", "--format", "json"]:
            return subprocess.CompletedProcess(argv, 0, "\n".join(map(json.dumps, self.containers)), "")
        if argv[:3] == ["docker", "network", "inspect"]:
            return subprocess.CompletedProcess(argv, 0 if self.network_exists else 1, "", "")
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
            edited = original + b"\r\n# Kept verbatim\r\nCUSTOM_SETTING=value\r\n"
            env.write_bytes(edited)
            with contextlib.redirect_stdout(io.StringIO()):
                bootstrap.bootstrap(["--env-file", str(env), "--render-only"], runner)
            self.assertEqual(env.read_bytes(), edited)
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

    def test_network_created_only_when_missing(self):
        for exists in (True, False):
            with self.subTest(exists=exists), tempfile.TemporaryDirectory() as directory:
                runner = FakeRunner(network_exists=exists)
                output = io.StringIO()
                with patch.dict(os.environ, {"PE_PLATFORM_NETWORK": "isolated", "PE_HTTP_PORT": "18280"}, clear=True), \
                        patch.object(bootstrap.shutil, "which", return_value="docker"), \
                        patch.object(bootstrap, "wait_ready", return_value={}) as ready, contextlib.redirect_stdout(output):
                    bootstrap.bootstrap(["--env-file", str(Path(directory) / ".env")], runner)
                creates = [c for c in runner.calls if c[:3] == ["docker", "network", "create"]]
                self.assertEqual(creates, [] if exists else [["docker", "network", "create", "isolated"]])
                self.assertIn(["docker", "network", "inspect", "isolated"], runner.calls)
                self.assertIn("--wait", runner.calls[-1])
                ready.assert_called_once()
                self.assertEqual(ready.call_args.args[0]["PE_HTTP_PORT"], "18280")
                self.assertEqual(ready.call_args.args[0]["PE_PUBLIC_DOMAIN"], "localhost")
                self.assertEqual(len(json.loads(output.getvalue())["hostnames"]), 6)

    def test_hostnames_come_from_domain_and_route_files(self):
        expected = ["example.test", "litellm.example.test", "langfuse.example.test", "s3.example.test",
                    "backplane.example.test", "grafana.example.test"]
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
