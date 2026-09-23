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


# A throwaway self-signed CA certificate (no key) for PEM parsing checks.
TEST_CA = """-----BEGIN CERTIFICATE-----
MIIBlzCCAT2gAwIBAgIULZKdwh3+v+jTCz4OG24FGjstfrMwCgYIKoZIzj0EAwIw
IDEeMBwGA1UEAwwVcGxhdGZvcm0tZWRnZSB0ZXN0IENBMCAXDTI2MDkyMzE5NDA0
MVoYDzIxMjYwODMwMTk0MDQxWjAgMR4wHAYDVQQDDBVwbGF0Zm9ybS1lZGdlIHRl
c3QgQ0EwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAASf6G8DMMGsHM2JJUi6/SM8
WZ5IA7rp245laoEeHTZMIb3C/gOtaN15IWuxZonJioqcFM0UHYj/kADv3254ybfq
o1MwUTAdBgNVHQ4EFgQUVlyz0zArW/aTRB/o2Gysz+MplDkwHwYDVR0jBBgwFoAU
Vlyz0zArW/aTRB/o2Gysz+MplDkwDwYDVR0TAQH/BAUwAwEB/zAKBggqhkjOPQQD
AgNIADBFAiEA9vTa0ChUeNGznGFKaysMzbHwnjIW6A0je9Le20OUWHoCIBenkx/s
xRz4Sf+J5HLs6Iw1kwdvvDGnPop1e7G9gyac
-----END CERTIFICATE-----
"""

CONTRACT_IPAM = [{"Subnet": "172.30.0.0/24", "IPRange": "172.30.0.128/25", "Gateway": "172.30.0.1"}]
PINNED = "caddy:2.11.4@sha256:" + "a" * 64
# Status Documents from fake bootstraps land here, never in the checkout's data directory.
STATE = tempfile.TemporaryDirectory()
os.chmod(STATE.name, 0o755)
tearDownModule = STATE.cleanup


def rendered(argv, source=STATE.name, image=PINNED):
    """Answer `docker compose config --format json` with the status mount; None for other commands."""
    if argv[-3:] != ["config", "--format", "json"]:
        return None
    volumes = [{"type": "bind", "source": "/srv/console", "target": "/srv/console", "read_only": True},
               {"type": "bind", "source": str(source), "target": "/srv/state", "read_only": True}]
    return subprocess.CompletedProcess(argv, 0, json.dumps({"services": {"caddy": {"image": image, "volumes": volumes}}}), "")


class FakeRunner:
    def __init__(self, *, containers=(), network_exists=True, ipam=CONTRACT_IPAM, create_fails=False, state=STATE.name):
        self.state = state
        self.containers = containers
        self.network_exists = network_exists
        self.ipam = ipam
        self.create_fails = create_fails
        self.calls = []

    def __call__(self, argv, **options):
        self.calls.append(argv)
        if rendered(argv, self.state):
            return rendered(argv, self.state)
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


class SanRunner(FakeRunner):
    """Answers the host openssl SAN query and the container state check."""

    def __init__(self, names, **options):
        super().__init__(**options)
        self.names = names
        self.state = "clean\n"

    def __call__(self, argv, **options):
        if argv[:2] == ["openssl", "x509"]:
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "X509v3 Subject Alternative Name: \n    " + self.names + "\n", "")
        if "run" in argv:
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, self.state, "")
        return super().__call__(argv, **options)


def start_bootstrap(runner, environment, directory):
    output = io.StringIO()
    with patch.dict(os.environ, environment, clear=True), \
            patch.object(bootstrap.shutil, "which", return_value="docker"), \
            patch.object(bootstrap, "wait_ready", return_value={}), contextlib.redirect_stdout(output):
        return bootstrap.bootstrap(["--env-file", str(Path(directory) / ".env")], runner)


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
                    if rendered(argv):
                        return rendered(argv)
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

    def test_missing_network_is_created_with_the_contract_allocation(self):
        for environment, expected in ((
                {}, ["--subnet", "172.30.0.0/24", "--ip-range", "172.30.0.128/25", "--gateway", "172.30.0.1", "isolated"]), (
                {"PE_PLATFORM_SUBNET": "10.9.0.0/16", "PE_PLATFORM_IP_RANGE": "10.9.128.0/17", "PE_EDGE_IP": "10.9.0.2"},
                ["--subnet", "10.9.0.0/16", "--ip-range", "10.9.128.0/17", "--gateway", "10.9.0.1", "isolated"])):
            with self.subTest(environment=environment), tempfile.TemporaryDirectory() as directory:
                ipam = [{"Subnet": expected[1], "IPRange": expected[3], "Gateway": expected[5]}]
                runner = FakeRunner(network_exists=False, ipam=ipam)
                self.assertEqual(start_bootstrap(runner, dict(environment, PE_PLATFORM_NETWORK="isolated"), directory), 0)
                creates = [c for c in runner.calls if c[:3] == ["docker", "network", "create"]]
                self.assertEqual(creates, [["docker", "network", "create", "--driver", "bridge"] + expected])
                probe = ["docker", "network", "inspect", "--format", "{{json .IPAM.Config}}", "isolated"]
                self.assertEqual([c for c in runner.calls if c[:3] == ["docker", "network", "inspect"]], [probe, probe])
                self.assertTrue(any("up" in call and "--wait" in call for call in runner.calls))

    def test_existing_network_with_matching_allocation_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner(ipam=[{"Subnet": "172.30.0.0/24", "IPRange": "172.30.0.128/25", "Gateway": "172.30.0.1"}])
            self.assertEqual(start_bootstrap(runner, {"PE_PLATFORM_NETWORK": "isolated"}, directory), 0)
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
                    start_bootstrap(runner, {"PE_PLATFORM_NETWORK": "isolated"}, directory)
                self.assertEqual(caught.exception.code, "platform_network_mismatch")
                for text in (observed, "172.30.0.0/24", "172.30.0.128/25", "gateway 172.30.0.1", "docker network rm isolated"):
                    self.assertIn(text, caught.exception.detail)
                self.assertFalse(any("up" in call for call in runner.calls))

    def test_lost_creation_race_validates_the_winner_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner(network_exists=False, create_fails=True)
            self.assertEqual(start_bootstrap(runner, {"PE_PLATFORM_NETWORK": "isolated"}, directory), 0)
            self.assertEqual(len([c for c in runner.calls if c[:3] == ["docker", "network", "create"]]), 1)
            self.assertTrue(any("up" in call and "--wait" in call for call in runner.calls))
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner(network_exists=False, create_fails=True, ipam=[{"Subnet": "172.18.0.0/16"}])
            with self.assertRaises(bootstrap.Refused) as caught:
                start_bootstrap(runner, {"PE_PLATFORM_NETWORK": "isolated"}, directory)
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
                        patch.object(bootstrap, "wait_ready", return_value={}) as ready, contextlib.redirect_stdout(output):
                    bootstrap.bootstrap(["--env-file", str(Path(directory) / ".env")], runner)
                creates = [c for c in runner.calls if c[:3] == ["docker", "network", "create"]]
                self.assertEqual(len(creates), 0 if exists else 1)
                self.assertIn(["docker", "network", "inspect", "--format", "{{json .IPAM.Config}}", "isolated"], runner.calls)
                self.assertTrue(any("up" in call and "--wait" in call for call in runner.calls))
                ready.assert_called_once()
                self.assertEqual(ready.call_args.args[0]["PE_HTTP_PORT"], "18280")
                self.assertEqual(ready.call_args.args[0]["PE_PUBLIC_DOMAIN"], "localhost")
                self.assertEqual(len(json.loads(output.getvalue())["hostnames"]), 7)

    def test_bootstrap_publishes_the_edge_status_document_into_the_rendered_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "mounted" / "state"
            backups = Path(directory) / "backups"
            for name, created in (("20260921T030000000000Z", "2026-09-21T03:00:00+00:00"),
                                  ("20260922T030000000000Z", "2026-09-22T05:00:00.123456+02:00"),
                                  ("20260923T030000000000Z", "not a time"), ("partial", "2026-09-23T04:00:00+00:00")):
                (backups / name).mkdir(parents=True)
                (backups / name / "manifest.json").write_text(json.dumps({"created_at": created}))
            runner = FakeRunner(state=state)
            order = []
            base = runner.__call__
            def observed(argv, **options):
                order.append((argv[-1] if "run" in argv else " ".join(argv[-3:]), state.is_dir(), (state / "status.json").exists()))
                return base(argv, **options)
            umask = os.umask(0o077)
            try:
                self.assertEqual(start_bootstrap(observed, {"PE_BACKUP_DIR": str(backups)}, directory), 0)
            finally:
                os.umask(umask)
            self.assertEqual(state.stat().st_mode & 0o777, 0o755)
            # The mount source exists before Docker could create it; the document only follows readiness.
            rendering = [command for command, _, _ in order].index("config --format json")
            self.assertFalse(order[rendering][1])
            self.assertTrue(all(exists for _, exists, _ in order[rendering + 1:]))
            self.assertFalse(any(published for _, _, published in order))
            document = json.loads((state / "status.json").read_text())
            self.assertEqual(oct((state / "status.json").stat().st_mode & 0o777), "0o644")
            self.assertEqual([path.name for path in state.iterdir()], ["status.json"])
            self.assertRegex(document.pop("configuredAt"), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            self.assertEqual(document, {
                "contract": 2, "stack": "edge",
                "components": [{"id": "caddy", "name": "Caddy", "kind": "gateway", "enabled": True,
                                "image": "caddy:2.11.4", "version": "2.11.4", "health": "/health/caddy"}],
                "features": {"backups": {"configured": True, "lastCheckpointAt": "2026-09-22T03:00:00Z"}},
            })
        for image, version in (("local/edge:experiment", None), ("registry.example:5000/team/caddy:v2.11.4-alpine", "v2.11.4"),
                               ("caddy@sha256:" + "b" * 64, None)):
            component = bootstrap.status_document(image, "2026-09-23T00:00:00Z", ROOT, {"PE_BACKUP_DIR": "/nonexistent"})["components"][0]
            self.assertEqual((component["image"], component["version"]), (image.partition("@")[0], version))

    def test_failed_readiness_publishes_nothing_and_a_failed_write_refuses(self):
        for failure in (bootstrap.Refused("not_ready", "original readiness failure"), None):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory, \
                    patch.dict(os.environ, {}, clear=True), patch.object(bootstrap.shutil, "which", return_value="docker"), \
                    patch.object(bootstrap, "wait_ready", side_effect=failure, return_value={}), \
                    patch.object(bootstrap, "publish_status", side_effect=None if failure else PermissionError(13, "Permission denied")) as publish, \
                    contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(bootstrap.Refused) as raised:
                bootstrap.bootstrap(["--env-file", str(Path(directory) / ".env")], FakeRunner(state=Path(directory) / "state"))
            if failure:
                self.assertIs(raised.exception, failure)
                publish.assert_not_called()
            else:
                self.assertEqual(raised.exception.code, "status_write_failed")
                self.assertIn("status.json: Permission denied", raised.exception.detail)
            self.assertEqual(output.getvalue(), "")

    def test_unusable_status_mount_refuses_before_any_change(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "private"
            private.mkdir(mode=0o700)
            private.chmod(0o700)
            for volumes, code in (([], "compose_config_failed"),
                                  ([{"target": "/srv/state", "source": str(private)}], "status_write_failed")):
                runner = FakeRunner()
                def rendering(argv, **options):
                    if argv[-3:] == ["config", "--format", "json"]:
                        return subprocess.CompletedProcess(argv, 0, json.dumps({"services": {"caddy": {"image": PINNED, "volumes": volumes}}}), "")
                    return runner(argv, **options)
                with self.subTest(code=code), self.assertRaises(bootstrap.Refused) as caught:
                    start_bootstrap(rendering, {}, directory)
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(any(call[:3] == ["docker", "network", "create"] or "run" in call or "up" in call for call in runner.calls))

    def test_hostnames_come_from_domain_and_route_files(self):
        expected = ["example.test", "litellm.example.test", "langfuse.example.test", "s3.example.test",
                    "backplane.example.test", "grafana.example.test", "rustfs.example.test"]
        self.assertEqual(bootstrap.routed_hostnames(ROOT / "routes.d", "example.test"), sorted(expected))
        with tempfile.TemporaryDirectory() as directory:
            routes = Path(directory)
            (routes / "custom.caddy").write_text('import site custom.{$PE_PUBLIC_DOMAIN} custom-routes\nhttp://legacy.{$PE_PUBLIC_DOMAIN} {\n}\n')
            self.assertEqual(bootstrap.routed_hostnames(routes, "other.test"), ["custom.other.test", "legacy.other.test"])


class AccessModeTests(unittest.TestCase):
    def test_modes_derive_issuers_and_schemes(self):
        with patch.dict(os.environ, {}, clear=True):
            for mode, scheme, issuer in [("local", "http", "internal"), ("public", "https", "acme"),
                                         ("proxy", "https", "")]:
                settings = bootstrap.settings_for({"PE_ACCESS_MODE": mode, "PE_PUBLIC_DOMAIN": "example.com"})
                self.assertEqual((settings["PE_SCHEME"], settings["PE_TLS_ISSUER"]), (scheme, issuer))
            files = {"PE_TLS_ISSUER": "files", "PE_TLS_DIR": "/srv/certs", "PE_PUBLIC_DOMAIN": "example.com"}
            for mode, issuer in (("local", "files"), ("public", "files"), ("proxy", "")):
                self.assertEqual(bootstrap.settings_for(dict(files, PE_ACCESS_MODE=mode))["PE_TLS_ISSUER"], issuer)
            for invalid in ({"PE_ACCESS_MODE": "typo"}, {"PE_PUBLIC_DOMAIN": "pe-edge"}, {"PE_TLS_ISSUER": "acme"},
                            {"PE_TLS_ISSUER": "files"}, {"PE_TLS_ISSUER": "letsencrypt"}, {"PE_TLS_ISSUER": "none"},
                            {"PE_ACCESS_MODE": "public", "PE_PUBLIC_DOMAIN": "example.com", "PE_TLS_ISSUER": "none"},
                            {"PE_ACCESS_MODE": "public", "PE_PUBLIC_DOMAIN": "example.com", "PE_TLS_ISSUER": "internal"}):
                with self.subTest(invalid=invalid), self.assertRaises(bootstrap.Refused) as caught:
                    bootstrap.settings_for(invalid)
                self.assertEqual(caught.exception.code, "invalid_settings")

    def test_acme_settings_validate_directory_and_account_binding(self):
        public = {"PE_ACCESS_MODE": "public", "PE_PUBLIC_DOMAIN": "example.com"}
        with patch.dict(os.environ, {}, clear=True):
            settings = bootstrap.settings_for(dict(public, PE_ACME_CA="https://ca.example.com/acme/acme/directory"))
            self.assertEqual((settings["PE_TLS_ISSUER"], settings["PE_ACME_EMAIL"]), ("acme", ""))
            bootstrap.settings_for(dict(public, PE_ACME_EAB_KEY_ID="kid", PE_ACME_EAB_HMAC="mac"))
            for invalid in (dict(public, PE_ACME_EAB_KEY_ID="kid"), dict(public, PE_ACME_EAB_HMAC="mac"),
                            dict(public, PE_ACME_CA_ROOT="/srv/ca.crt"),
                            dict(public, PE_ACME_CA="http://ca.example.com/acme/acme/directory"),
                            dict(public, PE_ACME_CA="ca.example.com/acme/acme/directory")):
                with self.subTest(invalid=invalid), self.assertRaises(bootstrap.Refused) as caught:
                    bootstrap.settings_for(invalid)
                self.assertEqual(caught.exception.code, "invalid_settings")

    def test_compose_selection_adds_tls_overlays_only_when_used(self):
        cases = (("PE_ACCESS_MODE=local\nPE_TLS_ISSUER=files\nPE_TLS_DIR=/srv/certs\n", ["compose.yaml", "compose.files.yaml"]),
                 ("PE_ACCESS_MODE=public\nPE_TLS_ISSUER=files\nPE_TLS_DIR=/srv/certs\n",
                  ["compose.yaml", "compose.public.yaml", "compose.files.yaml"]),
                 ("PE_ACCESS_MODE=public\nPE_ACME_CA=https://ca.example.com/acme/acme/directory\nPE_ACME_CA_ROOT=/srv/ca.crt\nPE_ACME_EAB_KEY_ID=kid\nPE_ACME_EAB_HMAC=mac\n",
                  ["compose.yaml", "compose.public.yaml", "compose.acme-ca-root.yaml", "compose.acme-eab.yaml"]),
                 ("PE_ACCESS_MODE=public\nPE_TLS_ISSUER=files\nPE_TLS_DIR=/srv/certs\nPE_ACME_CA_ROOT=/srv/ca.crt\n",
                  ["compose.yaml", "compose.public.yaml", "compose.files.yaml"]),
                 ("PE_ACCESS_MODE=public\n", ["compose.yaml", "compose.public.yaml"]),
                 ("PE_ACCESS_MODE=proxy\nPE_TLS_ISSUER=files\nPE_TLS_DIR=/srv/certs\n", ["compose.yaml", "compose.proxy.yaml"]),
                 ("PE_TLS_ISSUER=files\nPE_TLS_DIR=/srv/certs\nCOMPOSE_FILE=compose.yaml:compose.files.yaml:custom.yaml\n",
                  ["compose.yaml", "custom.yaml", "compose.files.yaml"]),
                 # A recorded overlay from an earlier issuer is dropped, custom overlays stay.
                 ("PE_TLS_ISSUER=internal\nCOMPOSE_FILE=compose.yaml:compose.files.yaml:compose.acme-eab.yaml:custom.yaml\n",
                  ["compose.yaml", "custom.yaml"]))
        for content, expected in cases:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
                env = Path(directory) / ".env"
                env.write_text(content)
                command = bootstrap.compose_command(ROOT, env)
                self.assertEqual([Path(name).name for name in command[command.index("-f") + 1::2]], expected)

    def test_files_issuer_needs_certificate_and_key_readable_by_caddy(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            certs = Path(directory) / "certs"
            certs.mkdir()
            (certs / "tls.crt").write_text("certificate")
            settings = bootstrap.settings_for({"PE_TLS_ISSUER": "files", "PE_TLS_DIR": str(certs)})
            with self.assertRaises(bootstrap.Refused) as caught:
                bootstrap.check_tls_inputs(FakeRunner(), settings, ROOT)
            self.assertEqual(caught.exception.code, "invalid_settings")
            self.assertIn("tls.key", caught.exception.detail)
            (certs / "tls.key").write_text("key")
            with patch.object(bootstrap.shutil, "which", return_value=None), self.assertRaises(bootstrap.Refused) as caught:
                bootstrap.check_tls_inputs(FakeRunner(), settings, ROOT)
            self.assertEqual(caught.exception.code, "openssl_missing")
            public = {"PE_ACCESS_MODE": "public", "PE_PUBLIC_DOMAIN": "example.com",
                      "PE_ACME_CA": "https://ca.example.com/acme/acme/directory"}
            pem = certs / "ca.pem"
            pem.write_text(TEST_CA)
            for key, value in (("PE_TLS_CA", str(certs / "absent.pem")), ("PE_TLS_CA", str(certs)),
                               ("PE_TLS_CA", str(certs / "tls.crt")), ("PE_ACME_CA_ROOT", str(certs / "tls.key"))):
                with self.subTest(key=key, value=value), self.assertRaises(bootstrap.Refused) as caught:
                    bootstrap.check_tls_inputs(FakeRunner(), bootstrap.settings_for(dict(public, **{key: value})), ROOT)
                self.assertIn(key, caught.exception.detail)
            bootstrap.check_tls_inputs(FakeRunner(), bootstrap.settings_for(dict(public, PE_TLS_CA=str(pem), PE_ACME_CA_ROOT=str(pem))), ROOT)
            # Trust files are checked only when the effective issuer uses them.
            bootstrap.check_tls_inputs(FakeRunner(), bootstrap.settings_for({"PE_TLS_CA": str(certs)}), ROOT)
            bootstrap.check_tls_inputs(FakeRunner(), bootstrap.settings_for({"PE_ACCESS_MODE": "proxy", "PE_TLS_CA": str(certs)}), ROOT)
            bootstrap.check_tls_inputs(SanRunner("DNS:example.com, DNS:*.example.com"),
                                       bootstrap.settings_for(dict(public, PE_TLS_ISSUER="files", PE_TLS_DIR=str(certs), PE_ACME_CA_ROOT=str(certs))), ROOT)
            # The state-check container reads the mounted files as Caddy's uid; a refusal names them.
            runner = SanRunner("DNS:localhost, DNS:*.localhost")
            runner.state = "unreadable\n"
            with self.assertRaises(bootstrap.Refused) as caught:
                start_bootstrap(runner, {"PE_TLS_ISSUER": "files", "PE_TLS_DIR": str(certs)}, directory)
            self.assertEqual(caught.exception.code, "tls_files_unreadable")
            self.assertIn("/certs/tls.key", caught.exception.detail)
            command = next(argv for argv in runner.calls if "run" in argv)[-1]
            self.assertIn("cat /certs/tls.crt /certs/tls.key >/dev/null", command)
            self.assertFalse(any("up" in argv for argv in runner.calls))

    def test_files_issuer_refuses_certificates_that_do_not_cover_every_hostname(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            certs = Path(directory) / "certs"
            certs.mkdir()
            (certs / "tls.crt").write_text("certificate")
            (certs / "tls.key").write_text("key")
            settings = bootstrap.settings_for({"PE_TLS_ISSUER": "files", "PE_TLS_DIR": str(certs), "PE_PUBLIC_DOMAIN": "example.test"})
            partial = "DNS:example.test, DNS:litellm.example.test, DNS:langfuse.example.test, DNS:s3.example.test, " \
                      "DNS:rustfs.example.test, DNS:backplane.example.test"
            with self.assertRaises(bootstrap.Refused) as caught:
                bootstrap.check_tls_inputs(SanRunner(partial), settings, ROOT)
            self.assertEqual(caught.exception.code, "invalid_settings")
            self.assertIn("grafana.example.test", caught.exception.detail)
            self.assertNotIn("litellm.example.test;", caught.exception.detail)
            for names in ("DNS:example.test, DNS:*.example.test", partial + ", DNS:GRAFANA.example.test"):
                runner = SanRunner(names)
                bootstrap.check_tls_inputs(runner, settings, ROOT)
                self.assertEqual(runner.calls, [["openssl", "x509", "-in", str(certs / "tls.crt"), "-noout", "-ext", "subjectAltName"]])
            with self.assertRaises(bootstrap.Refused) as caught:
                bootstrap.check_tls_inputs(SanRunner("DNS:*.example.test"), settings, ROOT)
            self.assertIn("tls.crt does not cover example.test;", caught.exception.detail)

    def test_readiness_probe_trusts_the_configured_ca_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            ca = Path(directory) / "corporate.pem"
            ca.write_text("-----BEGIN CERTIFICATE-----\ncorporate\n-----END CERTIFICATE-----\n")
            calls = []
            def runner(argv):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, "", "")
            for issuer, key, expected in (("files", "PE_TLS_CA", ca.read_text()), ("acme", "PE_ACME_CA_ROOT", ca.read_text()),
                                          ("acme", "PE_ACME_EMAIL", ""), ("files", "PE_ACME_CA_ROOT", "")):
                settings = dict(bootstrap.DEFAULTS, PE_ACCESS_MODE="public", PE_SCHEME="https", PE_TLS_ISSUER=issuer, **{key: str(ca)})
                with self.subTest(issuer=issuer, key=key), \
                        patch.object(bootstrap, "probe", return_value={"not_after_seconds": 2000000000}) as probe:
                    result = bootstrap.wait_ready(settings, ROOT, ROOT / "absent-env", runner)
                    self.assertEqual(probe.call_args.args[2], expected)
                    self.assertNotIn("ca_sha256", result)
            self.assertFalse(any("cat" in argv for argv in calls))

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
