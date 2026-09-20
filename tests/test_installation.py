"""Selected preflight contract. Files are real fixtures; Docker is never invoked."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import bootstrap
import installation


OWNER_SOURCE = {
    "edge": "# Edge has no managed secrets\n",
    "gateway": 'SECRETS: dict[str, int] = {"POSTGRES_PASSWORD": 24}\nPREFIXED: dict[str, tuple] = {"LANGFUSE_INIT_PROJECT_SECRET_KEY": ("sk-lf-", 24)}\n',
    "observability": 'SECRETS: dict[str, int] = {"OB_GRAFANA_ADMIN_PASSWORD": 24, "OB_S3_ACCESS_KEY": 10, "OB_S3_SECRET_KEY": 24}\n',
    "backplane": '// --mode full|minimal\nconst core = ["BP_AUTH_SECRET", "BP_POSTGRES_ADMIN_PASSWORD", "BP_POSTGRES_PASSWORD", "BP_OPERATIONS_TOKEN"];\nconst blobs = ["BP_RUSTFS_ROOT_USER", "BP_RUSTFS_ROOT_PASSWORD", "BP_BLOB_S3_ACCESS_KEY", "BP_BLOB_S3_SECRET_KEY"];\n',
}


class FakeRunner:
    def __init__(self, listeners="", occupied=False, publications="", fail=None):
        self.listeners, self.occupied, self.publications = listeners, occupied, publications
        self.containers, self.images, self.configs, self.volumes = {}, {}, {}, {}
        self.image_volumes = {}
        self.started = []
        self.fail = fail
        self.interrupted_backplane_services = None
        self.inventory_failed = False

    def render(self, root, values):
        name = next(name for name, (_, directory, _) in installation.STACKS.items() if root.name == directory)
        prefix, project, _ = installation.STACKS[name]
        project = values.get("COMPOSE_PROJECT_NAME", project)
        volume = values.get(prefix + "_VOLUME_PREFIX") or project
        ports = {"edge": (80, 443), "gateway": (18080,), "backplane": (3000,), "observability": (18180,)}[name]
        service = "server" if name == "backplane" else "caddy"
        services = {service: {"image": "fixture/" + name + ":1", "volumes": [
            {"type": "volume", "source": "data", "target": "/data"},
            {"type": "bind", "source": str(root / "Caddyfile"), "target": "/config", "read_only": True}],
            "ports": [{"host_ip": "127.0.0.1", "published": str(port), "target": port} for port in ports]}}
        if name == "edge":
            services["caddy"]["networks"] = {"platform": {"ipv4_address": values.get("PE_TAILSCALE_EDGE_IP", "")}}
        if name == "backplane":
            profiles = values.get("COMPOSE_PROFILES", "blobs,compute,gateway").split(",")
            services["server"]["image"] = values.get("BP_SERVER_IMAGE") or "fixture/backplane:1"
            if "compute" in profiles:
                services["workerd"] = {"image": values.get("BP_WORKERD_IMAGE") or "fixture/workerd:1", "volumes": [], "ports": []}
            services["server"]["environment"] = {"BP_BLOB_BACKEND": "s3" if "blobs" in profiles else "filesystem", "BP_COMPUTE_URL": "http://workerd:8080" if "compute" in profiles else ""}
        for desired in services.values():
            self.images.setdefault(desired["image"], desired["image"] if desired["image"].startswith("sha256:") else "sha256:" + hashlib.sha256(desired["image"].encode()).hexdigest())
        config = {"name": project, "services": services, "volumes": {"data": {"name": volume + ("-" if name == "gateway" else "_") + "data"}}}
        self.configs[project] = config
        return config

    def container(self, root, values, services=None):
        config = self.render(root, values)
        for volume in config["volumes"].values():
            labels = {} if root.name == "llm-gateway-stack" else {"com.docker.compose.project": config["name"]}
            self.volumes.setdefault(volume["name"], labels)
        for service, desired in config["services"].items():
            if services is not None and service not in services:
                continue
            identity = config["name"] + "-" + service
            self.containers[identity] = {"Id": identity, "Image": self.images[desired["image"]],
                "Labels": {"com.docker.compose.project": config["name"], "com.docker.compose.service": service},
                "Mounts": [{"Type": mount["type"], "Name": config["volumes"][mount["source"]]["name"] if mount["type"] == "volume" else None,
                            "Source": mount["source"], "Destination": mount["target"]} for mount in desired["volumes"]],
                "Ports": {str(port["target"]) + "/tcp": [{"HostIp": port["host_ip"], "HostPort": port["published"]}] for port in desired["ports"]},
                "Networks": {"platform": {"IPAddress": "172.30.0.2"}}, "Running": True}

    def __call__(self, argv, **options):
        if len(argv) > 1 and argv[1].endswith(("scripts/bootstrap.py", "infra/bootstrap/prepare.ts")):
            root = Path(argv[1]).parents[2 if argv[0] == "bun" else 1]
            name = next(name for name, (_, directory, _) in installation.STACKS.items() if root.name == directory)
            if Path(options.get("cwd", ".")) != root:
                return subprocess.CompletedProcess(argv, 1, "", "owner checkout unavailable")
            self.started.append(name)
            if self.fail == name:
                return subprocess.CompletedProcess(argv, 1, "private capability" * 10000, "private secret" * 10000)
            env = Path(argv[argv.index("--env-file") + 1])
            values = installation.read_settings(env)
            profiles = values.get("COMPOSE_PROFILES", "").split(",")
            if name == "backplane":
                if "COMPOSE_PROFILES" not in values:
                    profiles = (["blobs", "compute"] if "minimal" not in argv else []) + ["gateway"]
                    values.update(COMPOSE_FILE=":".join([str(root / "compose.yaml")] + [str(root / ("compose." + p + ".yaml")) for p in profiles]),
                                  COMPOSE_PROFILES=",".join(profiles), COMPOSE_PROJECT_NAME=root.name,
                                  BP_BLOB_BACKEND="s3" if "blobs" in profiles else "filesystem")
            from installation_execution import secret_keys
            keys = secret_keys(name, OWNER_SOURCE[name], profiles)
            additions = {key: "owner-secret-" + key for key in keys if key not in values}
            if name == "backplane":
                additions.update({key: value for key, value in values.items() if key not in installation.read_settings(env)})
            with env.open("a") as handle:
                handle.write("".join(key + "=" + value + "\n" for key, value in additions.items()))
            env.chmod(0o600)
            if name == "observability":
                marker = root / "data/installation/storage-mode"
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("filesystem\n")
            if name == "backplane" and self.interrupted_backplane_services is not None:
                self.container(root, installation.read_settings(env), self.interrupted_backplane_services)
                return subprocess.CompletedProcess(argv, 1, "private capability", "private error")
            self.container(root, installation.read_settings(env))
            return subprocess.CompletedProcess(argv, 0, "private capability", "private warning")
        if argv == ["ss", "-H", "-ltn"]:
            output = self.listeners
        elif argv[:3] == ["docker", "context", "inspect"]:
            output = "unix:///var/run/docker.sock"
        elif argv[:2] == ["docker", "info"]:
            output = "28.0.0"
        elif argv[:3] == ["docker", "compose", "version"]:
            output = "2.24.4"
        elif argv[:2] == ["docker", "compose"]:
            values = dict(options["env"], COMPOSE_PROFILES=",".join(argv[i + 1] for i, value in enumerate(argv[:-1]) if value == "--profile"))
            output = json.dumps(self.render(Path(argv[argv.index("--project-directory") + 1]), values))
        elif argv == ["docker", "ps", "--no-trunc", "--format", "json"]:
            rows = [{"ID": c["Id"], "Ports": ", ".join(binding["HostIp"] + ":" + binding["HostPort"] + "->" + target
                    for target, bindings in c["Ports"].items() for binding in bindings)} for c in self.containers.values() if c["Running"]]
            rows += [{"ID": "foreign", "Ports": line} for line in self.publications.splitlines()]
            output = "\n".join(map(json.dumps, rows))
        elif argv[:3] == ["docker", "network", "ls"]:
            output = ""
        elif argv[:3] == ["docker", "image", "inspect"]:
            output = json.dumps(self.image_volumes.get(argv[-1], {})) if argv[4] == "{{json .Config.Volumes}}" else self.images[argv[-1]]
        elif argv[:2] == ["docker", "inspect"]:
            value = self.containers[argv[-1]]
            output = value["Image"] if argv[3] == "{{.Image}}" else json.dumps(value["Networks"] if argv[3] == "{{json .NetworkSettings.Networks}}" else value)
        elif argv[:2] == ["docker", "ps"] and any(v.startswith("volume=") for v in argv):
            volume = next(v.removeprefix("volume=") for v in argv if v.startswith("volume="))
            output = "\n".join(c["Id"] if "--no-trunc" in argv else c["Id"][:12] for c in self.containers.values() if any(m.get("Name") == volume for m in c["Mounts"]))
        elif argv[:2] == ["docker", "ps"]:
            project = next(v.split("=", 2)[2] for v in argv if v.startswith("label=com.docker.compose.project="))
            service = next((v.split("=", 2)[2] for v in argv if v.startswith("label=com.docker.compose.service=")), None)
            output = "\n".join(c["Id"] for c in self.containers.values() if c["Labels"]["com.docker.compose.project"] == project
                               and (service is None or c["Labels"]["com.docker.compose.service"] == service))
        elif argv[:3] == ["docker", "volume", "ls"]:
            if self.inventory_failed:
                return subprocess.CompletedProcess(argv, 1, "", "private inventory error")
            selector = argv[argv.index("--filter") + 1]
            output = "existing" if self.occupied else "\n".join(name for name, labels in self.volumes.items()
                if (re.search(selector[5:], name) if selector.startswith("name=")
                    else labels.get("com.docker.compose.project") == selector.split("=", 2)[2]))
        elif argv[:3] == ["docker", "volume", "inspect"]:
            output = json.dumps(self.volumes[argv[-1]] if argv[4] == "{{json .Labels}}" else self.volumes[argv[-1]].get("com.docker.compose.project", ""))
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
                             "compose.edge.yaml", "compose.gateway.yaml", "compose.blobs.yaml", "compose.compute.yaml", "compose.public.yaml", "compose.tailscale.yaml", "package.json", "bun.lock", "Caddyfile"}:
                path = checkout / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("")
            checkout.chmod(0o755)
            (checkout / entrypoint).write_text(OWNER_SOURCE[name])
            if name == "edge":
                (checkout / ".env.example").write_bytes((ROOT / ".env.example").read_bytes())
            if name == "observability":
                (checkout / ".env.example").write_text("OB_ALERTS=placeholder\n")
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

    def test_empty_selection_retains_render_and_connection_flags_refuse_before_writing(self):
        code, rendered = self.invoke("--render-only")
        self.assertEqual(code, 0)
        self.assertEqual(rendered["generated"], [])
        self.assertEqual((self.root / ".env").read_bytes(), (self.root / ".env.example").read_bytes())
        (self.root / ".env").unlink()
        before = self.snapshot()
        with self.assertRaises(bootstrap.Refused) as caught:
            self.invoke("--stack", "edge", "--tailscale")
        self.assertEqual(caught.exception.code, "installation_connection_pending")
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

    def test_observability_requires_explicit_alert_delivery_before_any_installation(self):
        template = self.host / "observability-stack/.env.example"
        for settings, allowed in [("", False), ("OB_ALERTS=placeholder\n", True),
                                  ("OB_ALERT_WEBHOOK_URL=https://alerts.example.test/hook\n", True),
                                  ("OB_ALERT_EMAIL=operator@company.test\nOB_SMTP_URL=smtps://mail.example.test\n", True)]:
            with self.subTest(settings=settings):
                template.write_text(settings)
                before = self.snapshot()
                code, plan = self.invoke("--stack", "observability", "--dry-run")
                self.assertEqual(code, 0 if allowed else 1)
                self.assertEqual(self.snapshot(), before)
                if not allowed:
                    self.assertIn("Configure Observability alert delivery", plan["conflicts"][0]["detail"])

    def test_missing_prerequisites_do_not_write_or_disclose_diagnostics(self):
        before = self.snapshot()
        with patch.object(installation.shutil, "which", return_value=None):
            code, plan = self.invoke("--stack", "backplane", "--stack", "gateway", "--dry-run")
        self.assertEqual(code, 1)
        self.assertEqual({issue["stack"] for issue in plan["conflicts"]}, {"edge", "gateway", "backplane"})
        self.assertEqual(plan["infrastructure"], "unverified")
        self.assertEqual(plan["actions"][1]["installation"], "unknown")
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
        (checkout / ".env").chmod(0o600)
        before = self.snapshot()
        code, plan = self.invoke("--stack", "backplane", "--backplane-mode", "full", "--capability-file", str(self.capability), "--dry-run", runner=FakeRunner(occupied=True))
        self.assertEqual(code, 1)
        action = plan["actions"][1]
        self.assertEqual(action["compose_selection"], {"COMPOSE_FILE": selection, "COMPOSE_PROFILES": "gateway"})
        self.assertEqual((action["project"], action["volume_prefix"]), ("retained", "retained-data"))
        self.assertTrue({"mode_conflict", "network_conflict", "prerequisite_failed"} <= {issue["code"] for issue in plan["conflicts"]})
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
        (self.root / ".env").write_text("PE_TAILSCALE_HOST=machine.tailnet.ts.net\nPE_TAILSCALE_APPS=litellm,backplane,grafana\n")
        route = self.root / "routes.d" / "operator.caddy"
        route.parent.mkdir()
        route.write_text("# existing operator routing\n")
        (self.root / ".env").chmod(0o600)
        before = self.snapshot()
        code, plan = self.invoke("--stack", "observability", "--tailscale", "--status-timers", "--dry-run")
        self.assertEqual(code, 0)  # Env-only Edge can resume; connection actions remain deferred.
        self.assertEqual(plan["selected"], ["edge", "observability"])
        self.assertEqual(plan["actions"][1]["origin"], "https://machine.tailnet.ts.net:8447")
        self.assertIn("preserve existing PE_TAILSCALE_APPS", plan["actions"][2]["action"])
        self.assertEqual(plan["actions"][3]["depends_on"], plan["selected"])
        self.assertFalse(plan["execution_supported"])
        self.assertEqual(self.snapshot(), before)

    def test_operator_identity_and_capability_parent_cannot_come_from_unsafe_defaults(self):
        gateway = self.host / "llm-gateway-stack"
        (gateway / ".env.example").write_text("LG_BACKUP_DIR=" + str(self.backup) + "\nLG_ALLOW_SAME_FILESYSTEM_BACKUP=true\nLANGFUSE_INIT_USER_EMAIL=template@company.test\n")
        _, plan = self.invoke("--stack", "gateway", "--dry-run")
        self.assertTrue(any("Supply an existing" in item["detail"] for item in plan["conflicts"]))
        _, plan = self.invoke("--stack", "gateway", "--gateway-backup-dir", str(self.backup), "--dry-run")
        self.assertTrue(any("separate filesystem" in item["detail"] for item in plan["conflicts"]))
        (gateway / ".env").write_text("LG_ALLOW_SAME_FILESYSTEM_BACKUP=true\n")
        (gateway / ".env").chmod(0o600)
        _, plan = self.invoke("--stack", "gateway", "--gateway-backup-dir", str(self.backup), "--dry-run")
        self.assertTrue(any("operator's Langfuse" in item["detail"] for item in plan["conflicts"]))
        shared = self.host / "shared"
        shared.mkdir(mode=0o1777)
        shared.chmod(0o1777)
        original_stat = Path.stat
        def root_owned(path, *args, **kwargs):
            info = original_stat(path, *args, **kwargs)
            if path == shared:
                fields = list(info)
                fields[4] = 0  # Model the root-owned sticky /tmp parent, without changing host ownership.
                return os.stat_result(fields)
            return info
        with patch.object(Path, "stat", root_owned):
            _, plan = self.invoke("--stack", "backplane", "--backplane-backup-dir", str(self.backup), "--capability-file", str(shared / "enrollment"), "--dry-run")
        self.assertTrue(any("private-directory" in item["detail"] for item in plan["conflicts"]))
        self.assertFalse((shared / "enrollment").exists())

    def test_selection_usage_and_literal_env_rules(self):
        with patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": "foreign"}):
            _, plan = self.invoke("--stack", "gateway", "--dry-run")
        self.assertEqual([item["code"] for item in plan["conflicts"]], ["shell_settings"])
        self.assertEqual(plan["actions"], [])
        for args in [("--gateway-email", "operator@company.test"), ("--stack", "gateway", "--render-only"), ("--stack", "edge", "--gateway-dir", str(self.host))]:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                self.invoke(*args)
            self.assertEqual(error.exception.code, 2)
        env = self.host / "literal.env"
        for value in ["/mnt/backup # daily"]:
            env.write_text("BP_BACKUP_DIR=" + value + "\n")
            with self.assertRaisesRegex(ValueError, "Quote literal"):
                installation.read_settings(env)
        env.write_text("OB_OPERATOR_ALLOW=127.0.0.1/8 ::1\nBP_BACKUP_DIR=/mnt/with spaces\n")
        self.assertEqual(installation.read_settings(env), {"OB_OPERATOR_ALLOW": "127.0.0.1/8 ::1", "BP_BACKUP_DIR": "/mnt/with spaces"})
        observability = self.host / "observability-stack"
        (observability / ".env.example").write_text("OB_ALERTS=placeholder\nOB_OPERATOR_ALLOW=127.0.0.1/8 ::1\nOB_RUSTFS_CONSOLE_ALLOW=127.0.0.1/8 ::1\n")
        code, plan = self.invoke("--stack", "observability", "--dry-run")
        self.assertEqual((code, plan["conflicts"]), (0, []))
        env.write_text("BP_BACKUP_DIR='/mnt/with spaces'\n")
        self.assertEqual(installation.read_settings(env)["BP_BACKUP_DIR"], "/mnt/with spaces")
        gateway = self.host / "llm-gateway-stack"
        (gateway / ".env.example").write_text("COMPOSE_FILE=compose.yaml:compose.${LG_ACCESS_MODE:-local}.yaml\n")
        _, plan = self.invoke("--stack", "gateway", "--dry-run")
        self.assertTrue(any("unavailable or unresolved files" in item["detail"] for item in plan["conflicts"]))
        (gateway / "compose.local.yaml").write_text("")
        (gateway / ".env").write_text("COMPOSE_FILE=compose.yaml:compose.${LG_ACCESS_MODE:-local}.yaml\nLG_BACKUP_DIR=" + str(self.backup) + "\nLG_ALLOW_SAME_FILESYSTEM_BACKUP=true\nLANGFUSE_INIT_USER_EMAIL=operator@company.test\n")
        (gateway / ".env").chmod(0o600)
        _, plan = self.invoke("--stack", "gateway", "--dry-run")
        self.assertFalse(any(item["code"] == "prerequisite_failed" for item in plan["conflicts"]))
