"""Checkpoint capture, restore refusal and retention without Docker."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import bootstrap
import checkpoint


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.repository = Path(self.work.name)
        self.source = self.repository / "source"
        self.source.mkdir()
        self.secret = b"SECRET-PRIVATE-KEY-AND-COMMAND-OUTPUT"
        self.der = b"public certificate fixture"
        for name in checkpoint.ARTIFACTS:
            with tarfile.open(self.source / name, "w") as archive:
                entries = [("config.json", self.secret)] if name == "edge-config.tar" else [
                    (checkpoint.CA_PATH, ssl.DER_cert_to_PEM_cert(self.der).encode()),
                    ("caddy/pki/authorities/local/root.key", self.secret)]
                for path, data in entries:
                    info = tarfile.TarInfo("./" + path)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
        self.stack = Mock(spec=checkpoint.Stack)
        self.stack.images = {"caddy": "caddy:2.11.4@sha256:" + "a" * 64}
        self.stack.volumes = ["test_edge-data", "test_edge-config"]
        self.stack.dc = ["docker", "compose"]
        self.stack.settings = {"PE_BACKUP_KEEP": "7"}
        self.stack.diagnostics = self.repository / ".diagnostics"
        self.stack.helper.side_effect = lambda volume, command, **kwargs: [volume, command]
        self.document = checkpoint.manifest(self.source, self.stack.images, "commit")
        (self.source / "manifest.json").write_text(json.dumps(self.document))

    def test_capture_manifest_has_ca_fingerprint_and_no_secrets(self):
        calls = []

        def runner(argv):
            calls.append(argv)
            output = "running" if "ps" in argv else ""
            if argv[-1] == "du -sk /state":
                output = "4\t/state"
            if "rev-parse" in argv:
                output = "commit"
            return subprocess.CompletedProcess(argv, 0, output, "")

        def capture(argv, path):
            self.assertIn(self.stack.dc + ["stop", "--timeout", "30", "caddy"], calls)
            shutil.copyfile(self.source / path.name, path)
            self.assertFalse((path.parent / "manifest.json").exists())

        self.stack.stream.side_effect = capture
        directory = self.repository / "20260917T010000000000Z"
        with patch.object(bootstrap, "run", side_effect=runner), contextlib.redirect_stdout(io.StringIO()):
            checkpoint.backup(self.stack, directory)
        document = json.loads((directory / "manifest.json").read_text())
        self.assertEqual(document["ca_sha256"], hashlib.sha256(self.der).hexdigest())
        self.assertEqual(document["artifacts"], checkpoint.inventory(directory))
        self.assertEqual(document["images"], self.stack.images)
        self.assertNotIn(self.secret.decode(), json.dumps(document))
        self.assertNotIn("BEGIN CERTIFICATE", json.dumps(document))
        self.assertEqual(calls[-1], self.stack.dc + ["start", "caddy"])
        # Both command paths must keep secret-bearing stderr out of reported errors.
        for stream in (False, True):
            with self.subTest(stream=stream):
                result = subprocess.CompletedProcess([], 1, "", self.secret if stream else self.secret.decode())
                with patch.object(bootstrap, "run", return_value=result), \
                        patch.object(checkpoint.subprocess, "run", return_value=result), \
                        self.assertRaises(RuntimeError) as caught:
                    if stream:
                        checkpoint.Stack.stream(self.stack, ["transfer"], self.repository / "failed.tar")
                    else:
                        checkpoint.checked(["command"], self.stack.diagnostics)
                self.assertNotIn(self.secret.decode(), str(caught.exception))
                path = Path(str(caught.exception).split("diagnostics: ", 1)[1])
                self.assertEqual(path.read_bytes(), self.secret)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_capture_refuses_checkout_drift_before_stopping_or_creating_archive(self):
        stack = object.__new__(checkpoint.Stack)
        stack.dc = self.stack.dc
        stack.diagnostics = self.stack.diagnostics
        stack.image = self.stack.images["caddy"]
        stack.volumes = self.stack.volumes
        expected = {"Config": {"Image": stack.image}, "Mounts": [
            {"Type": "volume", "Destination": "/data", "Name": stack.volumes[0]},
            {"Type": "volume", "Destination": "/config", "Name": stack.volumes[1]}]}
        for defect in ("image", "volume", "bind", "missing"):
            container = json.loads(json.dumps(expected))
            if defect == "image": container["Config"]["Image"] = "caddy:old"
            if defect == "volume": container["Mounts"][0]["Name"] = "old_edge-data"
            if defect == "bind": container["Mounts"][0]["Type"] = "bind"
            calls = []
            def checked(argv, diagnostics):
                calls.append(argv)
                return ("" if defect == "missing" else "container-id") if "ps" in argv else json.dumps([container])
            directory = self.repository / ("rejected-" + defect)
            with self.subTest(defect=defect), patch.object(checkpoint, "checked", side_effect=checked):
                with self.assertRaises(ValueError): checkpoint.backup(stack, directory)
            self.assertFalse(directory.exists())
            self.assertFalse(any("stop" in argv or "run" in argv for argv in calls))
        with patch.object(checkpoint, "checked", side_effect=["container-id", json.dumps([expected])]):
            stack.require_capture_provenance()

    def test_restore_refuses_either_nonempty_volume_before_any_write(self):
        for occupied in self.stack.volumes:
            calls = []

            def runner(argv):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, int(argv[0] == occupied), "", "")

            with self.subTest(occupied=occupied), patch.object(bootstrap, "run", side_effect=runner):
                with self.assertRaisesRegex(ValueError, "restore refused: non-empty or unreadable volume"):
                    checkpoint.restore(self.stack, self.source)
                self.stack.stream.assert_not_called()
                self.assertIn([occupied, 'entries=$(ls -A /state); test -z "$entries"'], calls)

    def test_retention_prunes_only_old_complete_sets(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(bootstrap.settings_for({})["PE_BACKUP_KEEP"], "7")
            for value in ("0", "-1", "invalid"):
                with self.subTest(value=value), self.assertRaises(bootstrap.Refused):
                    bootstrap.settings_for({"PE_BACKUP_KEEP": value})
        complete = []
        for day in range(1, 10):
            path = self.repository / f"202609{day:02d}T010000000000Z"
            shutil.copytree(self.source, path)
            complete.append(path)
        incomplete = self.repository / "20260910T010000000000Z"
        incomplete.mkdir()
        (incomplete / "edge-data.tar").write_bytes(b"partial")
        corrupt = self.repository / "20260911T010000000000Z"
        shutil.copytree(self.source, corrupt)
        (corrupt / "edge-config.tar").write_bytes(b"corrupt")
        link = self.repository / "20260801T010000000000Z"
        link.symlink_to(self.source, target_is_directory=True)
        checkpoint.prune(self.repository, 7)
        self.assertTrue(all(not p.exists() for p in complete[:2]))
        self.assertTrue(all(p.exists() for p in complete[2:]))
        checkpoint.prune(self.repository, 1)
        self.assertTrue(complete[-1].exists())
        self.assertTrue(all(not p.exists() for p in complete[:-1]))
        for preserved in (self.source, incomplete, corrupt, link):
            self.assertTrue(preserved.exists())
        with self.assertRaises(ValueError):
            checkpoint.prune(self.repository, 0)


if __name__ == "__main__":
    unittest.main()
