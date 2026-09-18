"""Checkpoint capture, restore refusal and retention without Docker."""
import contextlib
import hashlib
import io
import json
import os
import signal
import shlex
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import bootstrap
import checkpoint
import backup_drill


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        settle = patch.object(checkpoint, "STOP_SETTLE_SECONDS", 0)
        settle.start()
        self.addCleanup(settle.stop)
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
        self.stack.env_file = self.repository / ".env"
        self.stack.settings = {"PE_BACKUP_KEEP": "7"}
        self.stack.diagnostics = self.repository / ".diagnostics"
        self.stack.helper.side_effect = lambda volume, command, **kwargs: [volume, command]
        self.document = checkpoint.manifest(self.source, self.stack.images, "commit")
        (self.source / "manifest.json").write_text(json.dumps(self.document))

    def test_hung_stop_or_transfer_is_reaped_before_resumption(self):
        real_run = bootstrap.run
        real_popen = subprocess.Popen
        for phase in ("stop", "transfer"):
            children = []
            starts = []
            # The descendant holds stderr open after its parent dies. Killing only
            # the direct child still blocks communicate until the descendant exits.
            command = [sys.executable, "-c",
                       "import os, signal, subprocess, sys, time; "
                       "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)']); "
                       "os.kill(os.getppid(), signal.SIGTERM); time.sleep(2); sys.exit(1)"]
            def popen(*args, **kwargs):
                child = real_popen(*args, **kwargs)
                children.append(child)
                return child
            def runner(argv, **kwargs):
                if "stop" in argv and phase == "stop":
                    return real_run(command, **kwargs)
                if "start" in argv:
                    self.assertTrue(children)
                    self.assertTrue(all(child.returncode == -signal.SIGKILL for child in children))
                    starts.append(argv)
                output = "container" if "ps" in argv else "4 /state" if argv[-1] == "du -sk /state" else ""
                return subprocess.CompletedProcess(argv, 0, output, "")
            self.stack.stream.side_effect = lambda argv, path: checkpoint.Stack.stream(self.stack, command, path)
            directory = self.repository / ("hung-" + phase)
            started = time.monotonic()
            with self.subTest(phase=phase), patch.object(bootstrap, "run", side_effect=runner), \
                    patch.object(subprocess, "Popen", side_effect=popen), \
                    patch.object(checkpoint, "COMMAND_TIMEOUT", 0.2), \
                    patch.object(checkpoint, "TRANSFER_TIMEOUT", 0.2), \
                    patch.object(bootstrap, "wait_ready", return_value={}), \
                    self.assertRaisesRegex(RuntimeError, "timed out"):
                checkpoint.backup(self.stack, directory)
            self.assertLess(time.monotonic() - started, 1.9)
            self.assertEqual(len(starts), 1)
            self.assertFalse((directory / "manifest.json").exists())

    def test_resumption_timeouts_exhaust_budget_without_masking_capture_failure(self):
        for phase in ("start", "readiness"):
            now = 0
            timeouts = []
            def sleep(seconds):
                nonlocal now
                now += seconds
            def runner(argv, **kwargs):
                if ("start" in argv and phase == "start") or "exec" in argv:
                    timeout = kwargs["timeout"]
                    self.assertGreater(timeout, 0)
                    self.assertLessEqual(timeout, 20)
                    timeouts.append(timeout)
                    sleep(timeout)
                    raise subprocess.TimeoutExpired(argv, timeout, stderr=self.secret)
                output = "container" if "ps" in argv else "4 /state" if argv[-1] == "du -sk /state" else ""
                return subprocess.CompletedProcess(argv, 0, output, "")
            def ready(settings, root, env_file, runner, timeout):
                runner(self.stack.dc + ["exec", "-T", "caddy", "cat", "root.crt"])
            self.stack.stream.side_effect = RuntimeError("capture failed")
            errors = io.StringIO()
            directory = self.repository / ("exhausted-" + phase)
            with self.subTest(phase=phase), patch.object(bootstrap, "run", side_effect=runner), \
                    patch.object(bootstrap, "wait_ready", side_effect=ready), \
                    patch.object(checkpoint.time, "monotonic", side_effect=lambda: now), \
                    patch.object(checkpoint.time, "sleep", side_effect=sleep), \
                    contextlib.redirect_stderr(errors), self.assertRaisesRegex(RuntimeError, "capture failed"):
                checkpoint.backup(self.stack, directory)
            self.assertEqual(now, 300)
            self.assertGreater(len(timeouts), 1)
            self.assertEqual(len(list(self.stack.diagnostics.glob("*.log"))), 1)
            for diagnostic in self.stack.diagnostics.glob("*.log"):
                diagnostic.unlink()
            self.assertLess(timeouts[-1], 20)
            self.assertIn("Caddy did not resume within 300 seconds", errors.getvalue())
            self.assertNotIn(self.secret.decode(), errors.getvalue())
            self.assertFalse((directory / "manifest.json").exists())

    def test_capture_manifest_has_ca_fingerprint_and_no_secrets(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            output = "running" if "ps" in argv else ""
            if argv[-1] == "du -sk /state":
                output = "4\t/state"
            if "rev-parse" in argv:
                output = "commit"
            if "--porcelain" in argv:
                output = " M routes.d/gateway.caddy"
            return subprocess.CompletedProcess(argv, 0, output, "")

        def capture(argv, path):
            self.assertIn(self.stack.dc + ["stop", "--timeout", "30", "caddy"], calls)
            shutil.copyfile(self.source / path.name, path)
            self.assertFalse((path.parent / "manifest.json").exists())

        self.stack.stream.side_effect = capture
        directory = self.repository / "20260917T010000000000Z"
        with patch.object(bootstrap, "run", side_effect=runner), \
                patch.object(bootstrap, "wait_ready", return_value={}), contextlib.redirect_stdout(io.StringIO()):
            checkpoint.backup(self.stack, directory)
        document = json.loads((directory / "manifest.json").read_text())
        self.assertEqual(document["ca_sha256"], hashlib.sha256(self.der).hexdigest())
        self.assertEqual(document["artifacts"], checkpoint.inventory(directory))
        self.assertEqual(document["images"], self.stack.images)
        self.assertNotIn(self.secret.decode(), json.dumps(document))
        self.assertNotIn("BEGIN CERTIFICATE", json.dumps(document))
        self.assertTrue(document["git_dirty"])
        # Both command paths must keep secret-bearing stderr out of reported errors.
        for stream in (False, True):
            with self.subTest(stream=stream):
                result = subprocess.CompletedProcess([], 1, "", self.secret if stream else self.secret.decode())
                with patch.object(bootstrap, "run", return_value=result), \
                        patch.object(bootstrap, "run_detached", return_value=result), \
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

    def test_capture_refuses_image_or_volume_drift_before_stopping_or_creating_archive(self):
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

            def runner(argv, **kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, int(argv[0] == occupied), "", "")

            with self.subTest(occupied=occupied), patch.object(bootstrap, "run", side_effect=runner):
                with self.assertRaisesRegex(ValueError, "restore refused: non-empty or unreadable volume"):
                    checkpoint.restore(self.stack, self.source)
                self.stack.stream.assert_not_called()
                self.assertIn([occupied, 'entries=$(ls -A /state); test -z "$entries"'], calls)

    def test_restore_pairs_each_archive_with_its_volume(self):
        volumes = {name: self.repository / name for name in self.stack.volumes}
        for path in volumes.values():
            path.mkdir()
        original = checkpoint.inventory(self.source)
        def runner(argv, **kwargs):
            if argv[0] in volumes:
                return subprocess.run(["sh", "-ec", argv[1].replace("/state", shlex.quote(str(volumes[argv[0]])))],
                                      capture_output=True, text=True)
            return subprocess.CompletedProcess(argv, 0, "", "")
        def extract(argv, path, restore):
            self.assertTrue(all((volume / ".pe-restore-incomplete").exists() for volume in volumes.values()))
            with tarfile.open(path, "r:") as archive:
                archive.extractall(volumes[argv[0]], filter="data")
        self.stack.stream.side_effect = extract
        with patch.object(bootstrap, "run", side_effect=runner), contextlib.redirect_stdout(io.StringIO()):
            checkpoint.restore(self.stack, self.source)
        self.assertTrue(all(not (path / ".pe-restore-incomplete").exists() for path in volumes.values()))
        self.assertEqual((volumes[self.stack.volumes[0]] / checkpoint.CA_PATH).read_text(), ssl.DER_cert_to_PEM_cert(self.der))
        self.assertEqual((volumes[self.stack.volumes[1]] / "config.json").read_bytes(), self.secret)
        self.assertEqual(checkpoint.inventory(self.source), original)

    def test_restore_rejects_unsafe_archives_before_any_docker_operation(self):
        for name, kind in [("/absolute", tarfile.REGTYPE), ("../escape", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)]:
            with tarfile.open(self.source / "edge-config.tar", "w") as archive:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = "../outside" if kind == tarfile.SYMTYPE else ""
                archive.addfile(member)
            self.document["artifacts"] = checkpoint.inventory(self.source)
            (self.source / "manifest.json").write_text(json.dumps(self.document))
            with self.subTest(name=name), patch.object(checkpoint, "checked") as checked:
                with self.assertRaisesRegex(ValueError, "unsafe archive"):
                    checkpoint.restore(self.stack, self.source)
                checked.assert_not_called()
                self.stack.stream.assert_not_called()

    def test_resume_failure_preserves_capture_failure_and_diagnostics(self):
        def checked(argv, diagnostics, **kwargs):
            if "start" in argv: raise RuntimeError("restart diagnostics")
            if "ps" in argv: return "running"
            if argv[-1] == "du -sk /state": return "4 /state"
            return ""
        self.stack.stream.side_effect = RuntimeError("capture diagnostics")
        errors = io.StringIO()
        now = 0
        def sleep(seconds):
            nonlocal now
            now += seconds
        with patch.object(checkpoint, "checked", side_effect=checked), \
                patch.object(bootstrap, "run", return_value=subprocess.CompletedProcess([], 1, "", "restart diagnostics")), \
                patch.object(checkpoint.time, "monotonic", side_effect=lambda: now), \
                patch.object(checkpoint.time, "sleep", side_effect=sleep), contextlib.redirect_stderr(errors):
            with self.assertRaisesRegex(RuntimeError, "capture diagnostics"):
                checkpoint.backup(self.stack, self.repository / "failed-capture")
        self.assertIn("Caddy resumption command failed", errors.getvalue())
        logs = list(self.stack.diagnostics.glob("*.log"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].read_text(), "restart diagnostics")
        self.assertNotIn("restart diagnostics", errors.getvalue())
        self.assertFalse((self.repository / "failed-capture/manifest.json").exists())

    def test_drill_cleanup_continues_and_preserves_primary_failure(self):
        for primary in (False, True):
            calls = []
            def run(argv, **kwargs):
                calls.append(argv)
                output, error, code = "", "", 0
                if argv[0] == "python3":
                    output = json.dumps({"certificate": {"ca_sha256": "same", "not_after_seconds": 9999999999}})
                elif argv[0] == "scripts/backup.sh":
                    output = json.dumps({"ca_sha256": "same", "checkpoint": "owned-checkpoint"})
                elif argv[0] == "scripts/destroy.sh" and primary:
                    error, code = "primary drill failure", 1
                elif "down" in argv:
                    error, code = "cleanup down failure", 1
                elif argv[:3] == ("docker", "volume", "ls") and any("down" in a for a in calls):
                    output = "owned-data\nowned-config"
                return subprocess.CompletedProcess(argv, code, output, error)
            errors = io.StringIO()
            with self.subTest(primary=primary), patch.object(backup_drill.subprocess, "run", side_effect=run), \
                    patch.object(bootstrap, "volume_names", return_value=["owned-data", "owned-config"]), \
                    contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "primary drill failure" if primary else "drill cleanup failed"):
                    backup_drill.main()
            self.assertIn(("docker", "volume", "rm", "owned-data"), calls)
            self.assertTrue(any(a[:3] == ("docker", "network", "rm") for a in calls))
            self.assertIn("cleanup down failure", errors.getvalue())

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

    def test_repeated_signals_resume_and_require_readiness(self):
        running = True
        healthy = False
        starts = 0

        def runner(argv, **kwargs):
            nonlocal running, starts
            output = ""
            if "stop" in argv:
                signal.raise_signal(signal.SIGTERM)
                running = False
            elif "start" in argv:
                signal.raise_signal(signal.SIGHUP)
                signal.raise_signal(signal.SIGINT)
                starts += 1
                running = starts > 1
            elif "ps" in argv:
                output = "container" if running else ""
            elif argv[-1] == "du -sk /state":
                output = "4 /state"
            return subprocess.CompletedProcess(argv, 0, output, "")

        def ready(*args, **kwargs):
            nonlocal healthy
            self.assertTrue(running)
            healthy = True
            return {}

        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
        def interrupted(signum, frame):
            raise RuntimeError("interrupted")
        for sig in handlers:
            signal.signal(sig, interrupted)
        self.addCleanup(lambda: [signal.signal(sig, handler) for sig, handler in handlers.items()])
        directory = self.repository / "interrupted"
        with patch.object(bootstrap, "run", side_effect=runner), \
                patch.object(bootstrap, "wait_ready", side_effect=ready), \
                patch.object(bootstrap.time, "sleep"), self.assertRaisesRegex(RuntimeError, "interrupted"):
            checkpoint.backup(self.stack, directory)
        self.assertTrue(running)
        self.assertTrue(healthy)
        self.assertFalse((directory / "manifest.json").exists())

        def quiet_runner(argv, **kwargs):
            output = "container" if "ps" in argv else "4 /state" if argv[-1] == "du -sk /state" else ""
            return subprocess.CompletedProcess(argv, 0, output, "")
        self.stack.stream.side_effect = lambda argv, path: shutil.copyfile(self.source / path.name, path)
        directory = self.repository / "unhealthy"
        now = 0
        def sleep(seconds):
            nonlocal now
            now += seconds
        def unhealthy(settings, root, env_file, runner, timeout):
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 20)
            sleep(timeout)
            raise bootstrap.Refused("not_ready")
        with patch.object(bootstrap, "run", side_effect=quiet_runner), \
                patch.object(bootstrap, "wait_ready", side_effect=unhealthy), \
                patch.object(checkpoint.time, "monotonic", side_effect=lambda: now), \
                patch.object(checkpoint.time, "sleep", side_effect=sleep), \
                self.assertRaisesRegex(RuntimeError, "Caddy did not resume within 300 seconds: not_ready"):
            checkpoint.backup(self.stack, directory)
        self.assertEqual(now, 300)
        self.assertFalse((directory / "manifest.json").exists())

    def test_late_stop_retries_readiness_and_post_readiness_running(self):
        for stopped_at in ("readiness", "post-readiness"):
            running, starts, polls = True, 0, 0
            def runner(argv, **kwargs):
                nonlocal running, starts, polls
                if "stop" in argv:
                    return subprocess.CompletedProcess(argv, 1, "", "stop CLI failed after dispatch")
                if "start" in argv:
                    starts += 1
                    running = True
                if "ps" in argv:
                    polls += 1
                    if polls == 3 and stopped_at == "post-readiness":
                        running = False
                    output = "container" if running else ""
                else:
                    output = "4 /state" if argv[-1] == "du -sk /state" else ""
                return subprocess.CompletedProcess(argv, 0, output, "")
            def ready(*args, **kwargs):
                nonlocal running
                if starts == 1 and stopped_at == "readiness":
                    running = False
                    raise bootstrap.Refused("not_ready", "late stop completed")
                return {}
            directory = self.repository / stopped_at
            errors = io.StringIO()
            with self.subTest(stopped_at=stopped_at), patch.object(bootstrap, "run", side_effect=runner), \
                    patch.object(bootstrap, "wait_ready", side_effect=ready), \
                    patch.object(checkpoint.time, "sleep"), contextlib.redirect_stderr(errors), \
                    self.assertRaisesRegex(RuntimeError, "command failed"):
                checkpoint.backup(self.stack, directory)
            self.assertTrue(running)
            self.assertEqual(starts, 2)
            self.assertEqual(errors.getvalue(), "")
            self.assertFalse((directory / "manifest.json").exists())

    def test_resume_waits_for_a_stop_that_finishes_after_initial_readiness(self):
        now = 0
        running = True
        starts = 0
        late_stop = False
        def sleep(seconds):
            nonlocal now, running, late_stop
            now += seconds
            if now >= 3 and not late_stop:
                running = False
                late_stop = True
        def runner(argv, **kwargs):
            nonlocal starts, running
            if "stop" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "stop submitted before CLI failure")
            if "start" in argv:
                starts += 1
                running = True
            output = "container" if "ps" in argv and running else "4 /state" if argv[-1] == "du -sk /state" else ""
            return subprocess.CompletedProcess(argv, 0, output, "")
        with patch.object(checkpoint, "STOP_SETTLE_SECONDS", 5), \
                patch.object(checkpoint.time, "monotonic", side_effect=lambda: now), \
                patch.object(checkpoint.time, "sleep", side_effect=sleep), \
                patch.object(bootstrap, "run", side_effect=runner), \
                patch.object(bootstrap, "wait_ready", return_value={}), \
                self.assertRaisesRegex(RuntimeError, "command failed"):
            checkpoint.backup(self.stack, self.repository / "late-healthy-stop")
        self.assertTrue(late_stop)
        self.assertTrue(running)
        self.assertGreaterEqual(starts, 2)
        self.assertGreaterEqual(now, 5)

    def test_transient_start_failure_retries_and_completes_capture(self):
        starts = 0
        running = True
        def runner(argv, **kwargs):
            nonlocal starts, running
            if "stop" in argv:
                running = False
            if "start" in argv:
                starts += 1
                if starts == 1:
                    return subprocess.CompletedProcess(argv, 1, "", "port still held")
                running = True
            output = "container" if "ps" in argv and running else ""
            if argv[-1] == "du -sk /state": output = "4 /state"
            return subprocess.CompletedProcess(argv, 0, output, "")
        self.stack.stream.side_effect = lambda argv, path: shutil.copyfile(self.source / path.name, path)
        directory = self.repository / "retry-start"
        with patch.object(bootstrap, "run", side_effect=runner), \
                patch.object(bootstrap, "wait_ready", return_value={}) as ready, \
                patch.object(checkpoint.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            checkpoint.backup(self.stack, directory)
        self.assertEqual(starts, 2)
        self.assertTrue(running)
        ready.assert_called_once()
        self.assertTrue((directory / "manifest.json").exists())

    def test_resume_readiness_children_detached_in_http_and_internal_tls(self):
        for issuer in ("none", "internal"):
            self.stack.settings = dict(bootstrap.DEFAULTS, PE_TLS_ISSUER=issuer,
                                       PE_SCHEME="https" if issuer == "internal" else "http")
            children = []
            def runner(argv, **kwargs):
                output = "container" if "ps" in argv else ""
                if argv[-1] == "du -sk /state": output = "4 /state"
                if "exec" in argv:
                    children.append(argv)
                    self.assertTrue(kwargs.get("start_new_session"), argv)
                    self.assertGreater(kwargs["timeout"], 0)
                    self.assertLessEqual(kwargs["timeout"], 20)
                    signal.raise_signal(signal.SIGINT)
                    if "cat" in argv: output = ssl.DER_cert_to_PEM_cert(self.der)
                return subprocess.CompletedProcess(argv, 0, output, "")
            self.stack.stream.side_effect = lambda argv, path: shutil.copyfile(self.source / path.name, path)
            directory = self.repository / issuer
            certificate = {"not_after_seconds": 9999999999} if issuer == "internal" else {}
            with self.subTest(issuer=issuer), patch.object(bootstrap, "run", side_effect=runner), \
                    patch.object(bootstrap.subprocess, "run", side_effect=runner), \
                    patch.object(bootstrap, "probe", return_value=certificate), \
                    self.assertRaisesRegex(RuntimeError, "interrupted; Caddy resumption completed"):
                checkpoint.backup(self.stack, directory)
            self.assertEqual(len(children), 2 if issuer == "internal" else 1)
            self.assertFalse((directory / "manifest.json").exists())

    def test_capture_refuses_restore_markers_or_helper_failure_before_outage(self):
        volumes = {name: self.repository / name for name in self.stack.volumes}
        for path in volumes.values(): path.mkdir()
        for defect in (*self.stack.volumes, "execution"):
            calls = []
            self.stack.stream.reset_mock()
            self.stack.stream.side_effect = RuntimeError("capture should not run")
            if defect in volumes:
                (volumes[defect] / bootstrap.RESTORE_MARKER).touch()
            def runner(argv, **kwargs):
                calls.append(argv)
                if argv[0] in volumes:
                    if defect == "execution":
                        return subprocess.CompletedProcess(argv, 1, "", "daemon unavailable")
                    command = argv[1].replace("/state", shlex.quote(str(volumes[argv[0]])))
                    return subprocess.run(["sh", "-ec", command], capture_output=True, text=True)
                return subprocess.CompletedProcess(argv, 0, "container" if "ps" in argv else "", "")
            directory = self.repository / ("rejected-" + defect)
            with self.subTest(defect=defect), patch.object(bootstrap, "run", side_effect=runner), \
                    patch.object(bootstrap, "wait_ready", return_value={}), \
                    self.assertRaises((ValueError, RuntimeError)):
                checkpoint.backup(self.stack, directory)
            self.assertFalse(directory.exists())
            self.assertFalse(any("stop" in argv for argv in calls))
            self.stack.stream.assert_not_called()
            if defect in volumes:
                (volumes[defect] / bootstrap.RESTORE_MARKER).unlink()

    def test_git_preflight_and_failed_capture_preserve_evidence(self):
        running = True
        git_failed = True

        def runner(argv, **kwargs):
            nonlocal running
            if "rev-parse" in argv and git_failed:
                return subprocess.CompletedProcess(argv, 1, "", "git ownership refusal")
            if "stop" in argv:
                running = False
            if "start" in argv:
                running = True
            output = "container" if "ps" in argv and running else ""
            if argv[-1] == "du -sk /state":
                output = "4 /state"
            return subprocess.CompletedProcess(argv, 0, output, "")

        directory = self.repository / "preflight"
        with patch.object(bootstrap, "run", side_effect=runner), self.assertRaises(RuntimeError):
            checkpoint.backup(self.stack, directory)
        self.assertTrue(running)
        self.assertFalse(directory.exists())
        git_failed = False
        def capture(argv, path):
            path.write_bytes(b"partial archive evidence")
            raise RuntimeError("capture failed")
        self.stack.stream.side_effect = capture
        with patch.object(bootstrap, "run", side_effect=runner), \
                patch.object(bootstrap, "wait_ready", return_value={}), self.assertRaisesRegex(RuntimeError, "capture failed"):
            checkpoint.backup(self.stack, directory)
        self.assertTrue(running)
        self.assertEqual((directory / "edge-data.tar").read_bytes(), b"partial archive evidence")
        self.assertFalse((directory / "manifest.json").exists())

    def test_partial_restore_blocks_bootstrap_and_preserves_ca(self):
        volumes = {name: self.repository / name for name in self.stack.volumes}
        for path in volumes.values():
            path.mkdir()
        original = checkpoint.inventory(self.source)
        env = self.stack.env_file
        env.write_text("PE_VOLUME_PREFIX=test\n")

        def runner(argv, **kwargs):
            if argv[0] in volumes:
                return subprocess.run(["sh", "-ec", argv[1].replace("/state", shlex.quote(str(volumes[argv[0]])))],
                                      capture_output=True, text=True)
            if "run" in argv:
                command = argv[-1]
                for suffix, volume in zip(("data", "config"), volumes.values()):
                    command = command.replace("/" + suffix, shlex.quote(str(volume)))
                return subprocess.run(["sh", "-ec", command], capture_output=True, text=True)
            if "up" in argv:
                self.fail("bootstrap started Caddy with an incomplete restore")
            return subprocess.CompletedProcess(argv, 0, "", "")

        def extract(argv, path, restore):
            if path.name == "edge-config.tar":
                raise RuntimeError("extraction interrupted")
            with tarfile.open(path, "r:") as archive:
                archive.extractall(volumes[argv[0]], filter="data")
        self.stack.stream.side_effect = extract
        with patch.object(bootstrap, "run", side_effect=runner), self.assertRaisesRegex(RuntimeError, "extraction interrupted"):
            checkpoint.restore(self.stack, self.source)
        self.assertTrue(all((path / ".pe-restore-incomplete").exists() for path in volumes.values()))
        ca = volumes[self.stack.volumes[0]] / checkpoint.CA_PATH
        self.assertEqual(bootstrap.ca_fingerprint(ca.read_text()), self.document["ca_sha256"])
        with patch.dict(os.environ, {}, clear=True), patch.object(bootstrap.shutil, "which", return_value="docker"), \
                self.assertRaises(bootstrap.Refused) as caught:
            bootstrap.bootstrap(["--env-file", str(env)], runner)
        self.assertEqual(caught.exception.code, "restore_incomplete")
        self.assertEqual(checkpoint.inventory(self.source), original)
        self.assertEqual(bootstrap.ca_fingerprint(ca.read_text()), self.document["ca_sha256"])

    def test_malformed_archives_and_empty_du_refuse_cleanly(self):
        for mode, size in (("w:gz", 1), ("w", 65537)):
            directory = self.repository / mode.replace(":", "-")
            shutil.copytree(self.source, directory)
            with tarfile.open(directory / "edge-data.tar", mode) as archive:
                info = tarfile.TarInfo(checkpoint.CA_PATH)
                der = self.der if mode == "w:gz" else b"x" * size
                pem = ssl.DER_cert_to_PEM_cert(der).encode()
                info.size = len(pem)
                archive.addfile(info, io.BytesIO(pem))
            document = dict(self.document, artifacts=checkpoint.inventory(directory), ca_sha256=hashlib.sha256(der).hexdigest())
            (directory / "manifest.json").write_text(json.dumps(document))
            with self.subTest(mode=mode), \
                    patch.object(bootstrap, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises((ValueError, tarfile.TarError)):
                checkpoint.restore(self.stack, directory)
        directory = self.repository / "empty-du"
        with patch.object(bootstrap, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                self.assertRaisesRegex(ValueError, "volume size"):
            checkpoint.backup(self.stack, directory)
        self.assertFalse(directory.exists())


if __name__ == "__main__":
    unittest.main()
