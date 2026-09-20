"""Selected preflight contract. Files are real fixtures; Docker is never invoked."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import bootstrap
import installation


class FakeRunner:
    def __init__(self, listeners="", occupied=False, publications=""):
        self.listeners = listeners
        self.occupied = occupied
        self.publications = publications

    def __call__(self, argv):
        if argv == ["ss", "-H", "-ltn"]:
            output = self.listeners
        elif argv[:3] == ["docker", "context", "inspect"]:
            output = "unix:///var/run/docker.sock"
        elif argv[:2] == ["docker", "info"]:
            output = "28.0.0"
        elif argv[:3] == ["docker", "compose", "version"]:
            output = "2.24.4"
        elif argv == ["docker", "ps", "--format", "{{.Ports}}"]:
            output = self.publications
        elif argv[:3] == ["docker", "network", "ls"]:
            output = ""
        elif argv[:3] == ["docker", "ps", "-aq"] or argv[:3] == ["docker", "volume", "ls"]:
            output = "existing" if self.occupied else ""
        else:
            raise AssertionError("unexpected or mutating inspection: " + repr(argv))
        return subprocess.CompletedProcess(argv, 0, output, "private diagnostic must not escape")


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.host = Path(self.temporary.name)
        self.root = self.host / "platform-edge"
        for name, (_, directory, entrypoint) in installation.STACKS.items():
            checkout = self.host / directory
            for filename in {entrypoint, "scripts/install_status_timer.py", ".env.example", "compose.yaml", "compose.proxy.yaml",
                             "compose.edge.yaml", "compose.gateway.yaml", "package.json", "bun.lock", "Caddyfile"}:
                path = checkout / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("")
            if name == "edge":
                (checkout / ".env.example").write_bytes((ROOT / ".env.example").read_bytes())
        self.backup = self.host / "backups"
        self.backup.mkdir()
        self.capability = self.host / "capability"
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {}, clear=True).start()
        patch.object(bootstrap, "__file__", str(self.root / "scripts/bootstrap.py")).start()
        patch.object(installation.shutil, "which", return_value="/usr/bin/tool").start()

    def snapshot(self):
        return {str(path): (path.read_bytes(), path.stat().st_mode) for path in self.host.rglob("*") if path.is_file()}

    def invoke(self, *args, runner=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = bootstrap.bootstrap(list(args), runner or FakeRunner())
        return code, json.loads(output.getvalue())

    def test_empty_selection_retains_render_and_selected_execution_refuses_before_writing(self):
        code, rendered = self.invoke("--render-only")
        self.assertEqual(code, 0)
        self.assertEqual(rendered["generated"], [])
        self.assertEqual((self.root / ".env").read_bytes(), (self.root / ".env.example").read_bytes())
        (self.root / ".env").unlink()
        before = self.snapshot()
        with self.assertRaises(bootstrap.Refused) as caught:
            self.invoke("--stack", "edge")
        self.assertEqual(caught.exception.code, "installation_execution_not_implemented")
        self.assertEqual(self.snapshot(), before)

    def test_exact_subset_does_not_read_other_sibling_configuration(self):
        original = Path.read_text
        def selected_only(path, *args, **kwargs):
            if any(path.is_relative_to(self.host / directory) for directory in ("agent-backplane", "llm-gateway-stack")):
                raise AssertionError("unselected sibling read")
            return original(path, *args, **kwargs)
        before = self.snapshot()
        with patch.object(Path, "read_text", selected_only):
            code, plan = self.invoke("--stack", "observability", "--stack", "observability", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(plan["selected"], ["edge", "observability"])
        self.assertEqual([action["stack"] for action in plan["actions"]], plan["selected"])
        self.assertEqual(plan["actions"][1]["listeners"], ["127.0.0.1:18180"])
        self.assertEqual(self.snapshot(), before)

    def test_missing_prerequisites_do_not_write_or_disclose_diagnostics(self):
        before = self.snapshot()
        with patch.object(installation.shutil, "which", return_value=None):
            code, plan = self.invoke("--stack", "backplane", "--stack", "gateway", "--dry-run")
        self.assertEqual(code, 1)
        self.assertEqual({issue["stack"] for issue in plan["conflicts"]}, {"edge", "gateway", "backplane"})
        self.assertEqual(plan["infrastructure"], "unverified")
        self.assertEqual(plan["enrollment"], "unverified")
        self.assertNotIn("private diagnostic", json.dumps(plan))
        self.assertEqual(self.snapshot(), before)
        with patch.dict(os.environ, {"DOCKER_HOST": "ssh://private-diagnostic"}):
            code, plan = self.invoke("--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("docker_unavailable", {issue["code"] for issue in plan["conflicts"]})
        self.assertNotIn("private-diagnostic", json.dumps(plan))
        self.assertEqual(self.snapshot(), before)

    def test_recorded_selection_survives_conflicting_mode_network_and_existing_state(self):
        checkout = self.host / "agent-backplane"
        (checkout / "operator.yaml").write_text("# preserve ordered custom overlay\n")
        selection = "compose.yaml:operator.yaml:compose.gateway.yaml"
        (checkout / ".env").write_text("COMPOSE_FILE=" + selection + "\nCOMPOSE_PROFILES=gateway\n"
            "COMPOSE_PROJECT_NAME=retained\nBP_VOLUME_PREFIX=retained-data\nBP_PLATFORM_NETWORK=other\n"
            "BP_POSTGRES_PASSWORD=private-secret\nBP_BACKUP_DIR=" + str(self.backup) + "\n")
        before = self.snapshot()
        code, plan = self.invoke("--stack", "backplane", "--backplane-mode", "full", "--capability-file", str(self.capability), "--dry-run", runner=FakeRunner(occupied=True))
        self.assertEqual(code, 1)
        action = plan["actions"][1]
        self.assertEqual(action["compose_selection"], {"COMPOSE_FILE": selection, "COMPOSE_PROFILES": "gateway"})
        self.assertEqual((action["project"], action["volume_prefix"]), ("retained", "retained-data"))
        self.assertTrue({"mode_conflict", "network_conflict", "checkpoint_review_required"} <= {issue["code"] for issue in plan["conflicts"]})
        self.assertNotIn("private-secret", json.dumps(plan))
        self.assertEqual(self.snapshot(), before)

    def test_origins_listener_collisions_and_backup_storage_are_checked(self):
        bp_args = ("--stack", "backplane", "--backplane-backup-dir", str(self.backup), "--capability-file", str(self.capability), "--dry-run")
        code, plan = self.invoke(*bp_args)
        self.assertEqual(code, 0)
        self.assertEqual(plan["actions"][1]["origin"], "https://backplane.localhost")
        code, plan = self.invoke(*bp_args, runner=FakeRunner(listeners="LISTEN 0 128 0.0.0.0:3000 0.0.0.0:*"))
        self.assertEqual(code, 1)
        self.assertIn("port_conflict", {issue["code"] for issue in plan["conflicts"]})
        code, plan = self.invoke(*bp_args, runner=FakeRunner(publications="0.0.0.0:9000->80/tcp\n0.0.0.0:3000->3000/tcp"))
        self.assertEqual(code, 1)
        self.assertIn("port_conflict", {issue["code"] for issue in plan["conflicts"]})
        code, plan = self.invoke("--stack", "gateway", "--gateway-backup-dir", str(self.backup), "--gateway-email", "operator@company.test", "--dry-run")
        self.assertEqual(code, 1)
        self.assertTrue(any("separate filesystem" in issue["detail"] for issue in plan["conflicts"]))
        (self.root / ".env.example").write_text("PE_ACCESS_MODE=public\nPE_PUBLIC_DOMAIN=company.test\n")
        code, plan = self.invoke(*bp_args)
        self.assertEqual(code, 0)
        self.assertEqual(plan["actions"][1]["origin"], "https://backplane.company.test")
        code, plan = self.invoke(*bp_args, "--tailscale")
        self.assertEqual(code, 1)
        self.assertIn("tailscale_access", {issue["code"] for issue in plan["conflicts"]})

    def test_add_stack_plan_preserves_existing_routes_and_tailscale_apps(self):
        (self.root / ".env").write_text("PE_TAILSCALE_HOST=machine.tailnet.ts.net\nPE_TAILSCALE_APPS=litellm,backplane\n")
        route = self.root / "routes.d" / "operator.caddy"
        route.parent.mkdir()
        route.write_text("# existing operator routing\n")
        before = self.snapshot()
        code, plan = self.invoke("--stack", "observability", "--tailscale", "--status-timers", "--dry-run")
        self.assertEqual(code, 1)  # Installed Edge requires identity verification in H-EXEC.
        self.assertEqual(plan["selected"], ["edge", "observability"])
        self.assertEqual(plan["actions"][1]["origin"], "https://machine.tailnet.ts.net:8447")
        self.assertIn("preserve existing PE_TAILSCALE_APPS", plan["actions"][2]["action"])
        self.assertEqual(plan["actions"][3]["depends_on"], plan["selected"])
        self.assertFalse(plan["execution_supported"])
        self.assertEqual(self.snapshot(), before)
