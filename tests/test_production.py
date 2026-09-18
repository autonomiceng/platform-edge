"""Production-readiness regression tests; no Docker calls."""
import contextlib
import hashlib
import io
import json
import os
import ssl
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import bootstrap
import checkpoint
import integration_smoke


class ProductionTests(unittest.TestCase):
    PUBLIC_HEALTH_RESPONSES = ["\n200", "\n200", "\n200", "\n200",
                               '{"status":"ready"}\n200', '{"database":"ok"}\n200']

    def test_public_gateway_health_accepts_empty_successful_responses(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"PE_PLATFORM_NETWORK": "platform", "PE_PUBLIC_DOMAIN": "example.test",
                                     "PE_SCHEME": "https", "PE_HTTPS_PORT": "443"}, clear=True), \
                patch("sys.argv", ["integration_smoke.py", "--ca-file", "public-ca.crt"]), \
                patch.object(integration_smoke, "checked", side_effect=self.PUBLIC_HEALTH_RESPONSES), \
                contextlib.redirect_stdout(output):
            self.assertEqual(integration_smoke.main(), 0)
        self.assertEqual(len(output.getvalue().splitlines()), 6)

    def test_public_gateway_health_rejects_nonempty_successful_responses(self):
        for route in (0, 1, 2):
            for body in ('unrelated response', '{"status":"unhealthy"}'):
                responses = self.PUBLIC_HEALTH_RESPONSES.copy()
                responses[route] = body + "\n200"
                with self.subTest(route=route, body=body), \
                        patch.dict(os.environ, {"PE_PLATFORM_NETWORK": "platform", "PE_PUBLIC_DOMAIN": "example.test",
                                                "PE_SCHEME": "https", "PE_HTTPS_PORT": "443"}, clear=True), \
                        patch("sys.argv", ["integration_smoke.py", "--ca-file", "public-ca.crt"]), \
                        patch.object(integration_smoke, "checked", side_effect=responses), \
                        contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(AssertionError, "public health must have an empty body"):
                        integration_smoke.main()

    def test_public_health_rejects_unsuccessful_status(self):
        for route in range(6):
            for status in ("301", "503"):
                responses = self.PUBLIC_HEALTH_RESPONSES.copy()
                responses[route] = "\n" + status
                with self.subTest(route=route, status=status), \
                        patch.dict(os.environ, {"PE_PLATFORM_NETWORK": "platform", "PE_PUBLIC_DOMAIN": "example.test",
                                                "PE_SCHEME": "https", "PE_HTTPS_PORT": "443"}, clear=True), \
                        patch("sys.argv", ["integration_smoke.py", "--ca-file", "public-ca.crt"]), \
                        patch.object(integration_smoke, "checked", side_effect=responses), \
                        contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(AssertionError, f"expected 200, got {status}"):
                        integration_smoke.main()

    def test_volume_names_follow_prefix(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(bootstrap.volume_names(bootstrap.settings_for({})),
                             ["platform-edge_edge-data", "platform-edge_edge-config"])
            settings = bootstrap.settings_for({"PE_VOLUME_PREFIX": "disposable"})
            calls = []
            def runner(argv):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, "", "")
            bootstrap.ensure_volumes(runner, settings)
            self.assertEqual(calls, [["docker", "volume", "create", "disposable_edge-data"],
                                     ["docker", "volume", "create", "disposable_edge-config"]])
            with self.assertRaises(bootstrap.Refused):
                bootstrap.settings_for({"PE_VOLUME_PREFIX": "../bad"})

    def test_destroy_requires_exact_typed_project(self):
        for answer in ("", "yes\n", "another-project\n", " platform-edge\n"):
            with self.subTest(answer=answer), patch("sys.stdin", io.StringIO(answer)), \
                    contextlib.redirect_stderr(io.StringIO()), patch.object(checkpoint, "checked") as command:
                with self.assertRaisesRegex(ValueError, "destroy refused"):
                    checkpoint.confirm_destroy("platform-edge", ["platform-edge_edge-data"])
                command.assert_not_called()
        with patch("sys.stdin", io.StringIO("platform-edge\n")), contextlib.redirect_stderr(io.StringIO()):
            checkpoint.confirm_destroy("platform-edge", ["platform-edge_edge-data"])

    def test_manifest_fingerprint_pins_checksums_and_no_secrets(self):
        public_der = b"public certificate fixture"
        pem = ssl.DER_cert_to_PEM_cert(public_der).encode()
        secret = b"PRIVATE-KEY-MUST-NOT-APPEAR-IN-MANIFEST"
        images = {"caddy": "caddy:2.11.4@sha256:" + "a" * 64}
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            for name in checkpoint.ARTIFACTS:
                with tarfile.open(directory / name, "w") as archive:
                    entries = [("config.json", b"{}")] if name == "edge-config.tar" else [
                        (checkpoint.CA_PATH, pem), ("caddy/pki/authorities/local/root.key", secret)]
                    for path, data in entries:
                        info = tarfile.TarInfo("./" + path)
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))
            document = checkpoint.manifest(directory, images, "commit")
            self.assertEqual(document["ca_sha256"], hashlib.sha256(public_der).hexdigest())
            self.assertEqual(document["images"], images)
            self.assertTrue(document["caddy_stopped"])
            for name, item in document["artifacts"].items():
                data = (directory / name).read_bytes()
                self.assertEqual(item, {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            self.assertNotIn(secret.decode(), json.dumps(document))
            self.assertNotIn("BEGIN CERTIFICATE", json.dumps(document))
            self.assertNotIn("environment", document)

    def test_argparse_error_is_one_json_line_and_exit_two(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/bootstrap.py"), "--unknown"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertEqual(json.loads(result.stderr)["error"], "bad_usage")


if __name__ == "__main__":
    unittest.main()
