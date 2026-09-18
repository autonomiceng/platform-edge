"""Bootstrap contract, four tests; the runner never calls Docker."""

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
        return subprocess.CompletedProcess(argv, 0, "", "")


class BootstrapTests(unittest.TestCase):
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
            (routes / "custom.caddy").write_text('{$PE_SCHEME}://custom.{$PE_PUBLIC_DOMAIN} {\n}\n')
            self.assertEqual(bootstrap.routed_hostnames(routes, "other.test"), ["custom.other.test"])


if __name__ == "__main__":
    unittest.main()
