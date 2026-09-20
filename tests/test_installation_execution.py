"""Execution contract against owning process and Docker metadata fixtures, never databases."""

from pathlib import Path
import unittest
from unittest.mock import patch

import test_installation as fixtures
from test_installation import FakeRunner
import installation


class ExecutionTests(unittest.TestCase):
    setUp = fixtures.InstallationTests.setUp
    invoke = fixtures.InstallationTests.invoke
    snapshot = fixtures.InstallationTests.snapshot

    def bp_arguments(self):
        return ("--stack", "backplane", "--backplane-backup-dir", str(self.backup), "--capability-file", str(self.capability))

    def test_fresh_subset_uses_owning_mode_and_only_selected_proxy_installations(self):
        runner = FakeRunner()
        with (self.root / ".env.example").open("a") as template:
            template.write("COMPOSE_PROJECT_NAME=host-edge\nCOMPOSE_FILE=compose.yaml:operator.yaml\n")
        (self.root / "operator.yaml").write_text("# keep the operator overlay order\n")
        args = (*self.bp_arguments(), "--backplane-mode", "minimal", "--stack", "observability")
        before = self.snapshot()
        code, plan = self.invoke(*args, "--dry-run", runner=runner)
        self.assertEqual((code, plan["execution_supported"]), (0, True))
        self.assertEqual(self.snapshot(), before)
        original = Path.read_text
        def selected_only(path, *args, **kwargs):
            if path.is_relative_to(self.host / "llm-gateway-stack"):
                raise AssertionError("unselected Gateway read")
            return original(path, *args, **kwargs)
        with patch.object(Path, "read_text", selected_only):
            code, report = self.invoke(*args, runner=runner)
        self.assertEqual((code, report["completed"]), (0, ["edge", "backplane", "observability"]))
        bp = installation.read_settings(self.host / "agent-backplane/.env")
        ob = installation.read_settings(self.host / "observability-stack/.env")
        edge = installation.read_settings(self.root / ".env")
        self.assertEqual(bp["COMPOSE_PROFILES"], "gateway")
        self.assertEqual(bp["BP_BLOB_BACKEND"], "filesystem")
        self.assertEqual(bp["BP_PUBLIC_URL"], "https://backplane.localhost")
        self.assertNotIn("BP_SERVER_IMAGE", bp)
        self.assertNotIn("BP_WORKERD_IMAGE", bp)
        self.assertEqual((ob["OB_ACCESS_MODE"], ob["OB_HTTP_PORT"], ob["OB_PLATFORM_NETWORK"]), ("proxy", "18180", "platform"))
        self.assertEqual(ob["OB_TRUSTED_PROXIES"], edge["PE_TAILSCALE_EDGE_IP"] + "/32")
        self.assertEqual(edge["COMPOSE_PROJECT_NAME"], "host-edge")
        self.assertEqual([Path(file).name for file in edge["COMPOSE_FILE"].split(":")][:2], ["compose.yaml", "operator.yaml"])
        self.assertEqual(Path(edge["COMPOSE_FILE"].split(":")[-1]).name, "compose.tailscale.yaml")
        self.assertFalse((self.host / "llm-gateway-stack/.env").exists())
        self.assertNotIn("gateway", runner.started)
        self.assertEqual(report["enrollment"], "unverified")

    def test_matching_rerun_preserves_recorded_secrets_selection_and_stopped_mounts(self):
        runner = FakeRunner()
        self.assertEqual(self.invoke(*self.bp_arguments(), runner=runner)[0], 0)
        env = self.host / "agent-backplane/.env"
        with env.open("a") as handle:
            handle.write("# operator custody comment\nUNRELATED_SETTING=preserved\n")
        original = env.read_bytes()
        self.assertNotIn("BP_SERVER_IMAGE", installation.read_settings(env))
        self.assertNotIn("BP_WORKERD_IMAGE", installation.read_settings(env))
        before_mounts = {key: value["Mounts"] for key, value in runner.containers.items()}
        runner.containers["agent-backplane-server"]["Running"] = False
        runner.containers["platform-edge-caddy"]["Running"] = False
        runner.containers["platform-edge-caddy"]["Networks"]["platform"]["IPAddress"] = ""
        code, report = self.invoke(*self.bp_arguments(), runner=runner)
        self.assertEqual((code, report["completed"]), (0, ["edge", "backplane"]))
        self.assertEqual(env.read_bytes(), original)
        self.assertEqual({key: value["Mounts"] for key, value in runner.containers.items()}, before_mounts)
        self.assertEqual(list(self.backup.iterdir()), [])  # No retained Checkpoint is required for unchanged reuse.

    def test_foreign_image_mount_volume_and_port_refuse_before_env_or_owner_changes(self):
        runner = FakeRunner()
        self.assertEqual(self.invoke("--stack", "observability", runner=runner)[0], 0)
        before, started = self.snapshot(), list(runner.started)
        original_image = runner.images["fixture/observability:1"]
        runner.images["fixture/observability:1"] = "sha256:foreign"
        code, plan = self.invoke("--stack", "observability", runner=runner)
        self.assertEqual(code, 1)
        self.assertTrue(any("image ID differs" in error["detail"] for error in plan["conflicts"]))
        runner.images["fixture/observability:1"] = original_image
        mount = runner.containers["observability-stack-caddy"]["Mounts"][0]
        mount["Name"] = "foreign_data"
        code, plan = self.invoke("--stack", "observability", runner=runner)
        self.assertEqual(code, 1)
        self.assertTrue(any("mount or checkout differs" in error["detail"] for error in plan["conflicts"]))
        mount["Name"] = "observability-stack_data"
        runner.volumes["observability-stack_data"] = {"com.docker.compose.project": "foreign"}
        code, plan = self.invoke("--stack", "observability", runner=runner)
        self.assertEqual(code, 1)
        self.assertTrue(any("Volume ownership" in error["detail"] for error in plan["conflicts"]))
        runner.volumes["observability-stack_data"] = {"com.docker.compose.project": "observability-stack"}
        runner.volumes["observability-stack_unused"] = {"com.docker.compose.project": "foreign"}
        code, plan = self.invoke("--stack", "observability", runner=runner)
        self.assertEqual(code, 1)
        self.assertTrue(any("Volume ownership" in error["detail"] for error in plan["conflicts"]))
        del runner.volumes["observability-stack_unused"]
        runner.publications = "0.0.0.0:18180->80/tcp"
        code, plan = self.invoke("--stack", "observability", runner=runner)
        self.assertEqual(code, 1)
        self.assertIn("port_conflict", {error["code"] for error in plan["conflicts"]})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(runner.started, started)

    def test_existing_unlabelled_and_image_inherited_volumes_require_qualified_users(self):
        runner = FakeRunner()
        self.assertEqual(self.invoke(*self.bp_arguments(), runner=runner)[0], 0)
        runner.volumes["platform-edge_data"] = {}
        container = runner.containers["agent-backplane-server"]
        runner.image_volumes["fixture/backplane:1"] = {"/implicit": {}}
        runner.volumes["anonymous"] = {"com.docker.volume.anonymous": ""}
        container["Mounts"].append({"Type": "volume", "Name": "anonymous", "Destination": "/implicit"})
        before = self.snapshot()
        self.assertEqual(self.invoke(*self.bp_arguments(), "--dry-run", runner=runner)[0], 0)
        container["ExplicitMounts"] = [{"Target": "/implicit", "Source": "anonymous"}]
        self.assertEqual(self.invoke(*self.bp_arguments(), "--dry-run", runner=runner)[0], 1)
        del container["ExplicitMounts"]
        runner.containers["foreign"] = {**container, "Id": "foreign", "Labels": {"com.docker.compose.project": "foreign"}}
        self.assertEqual(self.invoke(*self.bp_arguments(), "--dry-run", runner=runner)[0], 1)
        del runner.containers["foreign"]
        runner.image_volumes["fixture/backplane:1"] = {}
        self.assertEqual(self.invoke(*self.bp_arguments(), "--dry-run", runner=runner)[0], 1)
        self.assertEqual(self.snapshot(), before)

    def test_unavailable_inventory_is_not_treated_as_fresh(self):
        runner = FakeRunner()
        runner.inventory_failed = True
        before = self.snapshot()
        code, plan = self.invoke("--stack", "observability", runner=runner)
        self.assertEqual(code, 1)
        self.assertTrue(plan["conflicts"])
        self.assertNotIn("private inventory error", str(plan))
        self.assertEqual(runner.started, [])
        self.assertEqual(self.snapshot(), before)

    def test_interrupted_volumes_and_partial_services_resume_after_explicit_stale_lock_recovery(self):
        runner = FakeRunner()
        runner.interrupted_backplane_services = ()
        code, report = self.invoke(*self.bp_arguments(), runner=runner)
        self.assertEqual((code, report["completed"], report["stopped_at"]), (3, ["edge"], "backplane"))
        env = self.host / "agent-backplane/.env"
        persisted = env.read_bytes()
        self.assertEqual(installation.read_settings(env)["COMPOSE_PROFILES"], "blobs,compute,gateway")
        self.assertEqual(runner.volumes["agent-backplane_data"], {"com.docker.compose.project": "agent-backplane"})
        self.assertNotIn("agent-backplane-server", runner.containers)
        lock = env.with_name(".env.lock")
        lock.write_text("confirmed by operator only\n")
        lock.chmod(0o600)
        before, started = self.snapshot(), list(runner.started)
        code, plan = self.invoke(*self.bp_arguments(), runner=runner)
        self.assertEqual(code, 1)
        self.assertIn("backplane_lock", {error["code"] for error in plan["conflicts"]})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(runner.started, started)
        lock.unlink()  # Explicit operator recovery, never installer cleanup of a pre-existing lock.
        runner.volumes["agent-backplane_data"] = {}
        before = self.snapshot()
        code, plan = self.invoke(*self.bp_arguments(), runner=runner)
        self.assertEqual(code, 1)
        self.assertTrue(any("Volume ownership" in error["detail"] for error in plan["conflicts"]))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(runner.started, started)
        runner.volumes["agent-backplane_data"] = {"com.docker.compose.project": "agent-backplane"}
        runner.interrupted_backplane_services = ("server",)
        code, report = self.invoke(*self.bp_arguments(), runner=runner)
        self.assertEqual((code, report["completed"], report["stopped_at"]), (3, ["edge"], "backplane"))
        self.assertIn("agent-backplane-server", runner.containers)
        self.assertNotIn("agent-backplane-workerd", runner.containers)
        self.assertEqual(env.read_bytes(), persisted)
        runner.interrupted_backplane_services = None
        code, report = self.invoke(*self.bp_arguments(), runner=runner)
        self.assertEqual((code, report["completed"]), (0, ["edge", "backplane"]))
        self.assertEqual(env.read_bytes(), persisted)
        self.assertIn("agent-backplane-workerd", runner.containers)

    def test_add_stack_preserves_routes_and_never_reads_omitted_sibling(self):
        runner = FakeRunner()
        self.assertEqual(self.invoke("--stack", "observability", runner=runner)[0], 0)
        env = self.host / "observability-stack/.env"
        original = env.read_bytes()
        route = self.root / "routes.d/operator.caddy"
        route.parent.mkdir()
        route.write_text("# operator routing stays intact\n")
        (self.host / "llm-gateway-stack/.env").write_text("LG_ALLOW_SAME_FILESYSTEM_BACKUP=true\n")
        (self.host / "llm-gateway-stack/.env").chmod(0o600)
        read = Path.read_text
        def selected_only(path, *args, **kwargs):
            if path.is_relative_to(self.host / "observability-stack"):
                raise AssertionError("omitted Observability read")
            return read(path, *args, **kwargs)
        with patch.object(Path, "read_text", selected_only):
            code, report = self.invoke("--stack", "gateway", "--gateway-backup-dir", str(self.backup), "--gateway-email", "operator@company.test", runner=runner)
        self.assertEqual((code, report["completed"]), (0, ["edge", "gateway"]))
        self.assertEqual(env.read_bytes(), original)
        self.assertEqual(route.read_text(), "# operator routing stays intact\n")
        gateway = installation.read_settings(self.host / "llm-gateway-stack/.env")
        self.assertEqual(gateway["LG_LANGFUSE_URL"], "http://langfuse.localhost")
        self.assertIn("observability-stack-caddy", runner.containers)
        before, started = self.snapshot(), list(runner.started)
        code, plan = self.invoke("--stack", "gateway", runner=runner)
        self.assertEqual((code, plan["completed"]), (0, ["edge", "gateway"]))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(runner.started[len(started):], ["edge", "gateway"])

    def test_child_failure_has_bounded_completion_report_and_does_not_start_later_owners(self):
        runner = FakeRunner(fail="backplane")
        code, report = self.invoke(*self.bp_arguments(), "--stack", "observability", runner=runner)
        self.assertEqual(code, 3)
        self.assertEqual(report["completed"], ["edge"])
        self.assertEqual(report["stopped_at"], "backplane")
        self.assertLess(len(str(report)), 600)
        self.assertNotIn("private", str(report))
        self.assertFalse((self.host / "observability-stack/.env").exists())
        self.assertNotIn("observability", runner.started)
