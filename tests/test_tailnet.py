"""Tailnet Origins contract: refusal without a key, node selection, enrollment and the recorded domain; no Docker calls."""

import contextlib
import functools
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bootstrap
import tailnet
from test_bootstrap import FakeRunner

ROOT = Path(__file__).resolve().parent.parent
RUNNING = {"BackendState": "Running", "Self": {"DNSName": "litellm.tail1234.ts.net.", "Online": True}}


def status(name, domain="tail1234.ts.net", state="Running"):
    return json.dumps({"BackendState": state, "Self": {"DNSName": f"{name}.{domain}." if state == "Running" else ""}})


class TailnetRunner(FakeRunner):
    """Answers `tailscale status --json` per node; the first poll of every node is still enrolling."""

    def __init__(self, names=None, **options):
        super().__init__(**options)
        self.names = names or {}
        self.polls = {}

    def __call__(self, argv, **options):
        if argv[-3:] == ["tailscale", "status", "--json"]:
            self.calls.append(argv)
            service = argv[argv.index("exec") + 2]
            self.polls[service] = self.polls.get(service, 0) + 1
            if self.polls[service] == 1:
                return subprocess.CompletedProcess(argv, 0, status("", state="NeedsLogin"), "")
            return subprocess.CompletedProcess(argv, 0, status(self.names.get(service, service.removeprefix("ts-"))), "")
        return super().__call__(argv, **options)


def run_main(argv, runner, environment=None):
    out, err = io.StringIO(), io.StringIO()
    with patch.dict(os.environ, environment or {}, clear=True), patch.object(sys, "argv", ["bootstrap.py", *argv]), \
            patch.object(shutil, "which", return_value="/usr/bin/tool"), patch.object(bootstrap, "wait_ready", return_value={}), \
            patch.object(tailnet, "probe_origins", return_value="skipped: test"), patch.object(tailnet.time, "sleep"), \
            patch.object(bootstrap, "bootstrap", functools.partial(bootstrap.bootstrap, runner=runner)), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = bootstrap.main()
    return code, out.getvalue(), err.getvalue()


class TailnetTests(unittest.TestCase):
    def test_tailscale_without_an_auth_key_is_refused_before_any_change(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            runner = FakeRunner()
            code, _, err = run_main(["--env-file", str(env), "--tailscale"], runner)
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(err)["error"], "tailscale_auth_key")
            self.assertFalse(env.exists())
            self.assertEqual(runner.calls, [])
            env.write_text("PE_TS_AUTHKEY=tskey-auth-test\nPE_BIND_HOST=0.0.0.0\n")
            code, _, err = run_main(["--env-file", str(env), "--tailscale"], FakeRunner())
            self.assertEqual((code, json.loads(err)["error"]), (1, "invalid_settings"))
            for invalid in ({"PE_TS_APPS": "console,minio"}, {"PE_TAILNET_DOMAIN": "example.com"}, {"PE_TS_TAG": "platform"},
                            {"PE_ROOT_HOST": "litellm"}, {"PE_ROOT_HOST": "a.b"}):
                with self.subTest(invalid=invalid), patch.dict(os.environ, {}, clear=True), self.assertRaises(bootstrap.Refused) as caught:
                    bootstrap.settings_for(invalid)
                self.assertEqual(caught.exception.code, "invalid_settings")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(bootstrap.settings_for({"PE_TS_TAG": ""})["PE_TS_TAG"], "")
                self.assertEqual(bootstrap.settings_for({})["PE_TS_APPS"], "console,litellm,langfuse,s3,rustfs,backplane,grafana")

    def test_selected_apps_map_to_profiles_services_volumes_and_origins(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env = Path(directory) / ".env"
            env.write_text("PE_TS_AUTHKEY=tskey-auth-test\nPE_TS_APPS=console,litellm,backplane\nPE_ROOT_HOST=edge\nPE_TAILNET_DOMAIN=tail1234.ts.net\n")
            settings = bootstrap.settings_for(bootstrap.read_env(env))
            apps = tailnet.selected(settings)
            self.assertEqual(apps, ("console", "litellm", "backplane"))
            command = bootstrap.compose_command(ROOT, env, apps)
            self.assertEqual([command[i + 1] for i, part in enumerate(command) if part == "--profile"], ["ts-console", "ts-litellm", "ts-backplane"])
            self.assertEqual(command[-2:], ["-f", str(ROOT / "compose.tailscale.yaml")])
            self.assertEqual(bootstrap.compose_command(ROOT, env).count("--profile"), 0)
            self.assertNotIn("compose.tailscale.yaml", " ".join(bootstrap.compose_command(ROOT, env)))
            self.assertEqual(bootstrap.volume_names(settings, apps)[2:], ["platform-edge_ts-console", "platform-edge_ts-litellm", "platform-edge_ts-backplane"])
            self.assertEqual(tailnet.origins(settings, apps), {"console": "https://edge.tail1234.ts.net", "litellm": "https://litellm.tail1234.ts.net",
                                                              "backplane": "https://backplane.tail1234.ts.net"})
            runner = TailnetRunner({"ts-console": "edge"})
            code, out, err = run_main(["--env-file", str(env), "--tailscale"], runner)
            self.assertEqual(code, 0, err)
            ups = [argv for argv in runner.calls if "up" in argv]
            self.assertEqual(len(ups), 2)
            self.assertEqual(ups[0][-3:], ["ts-console", "ts-litellm", "ts-backplane"])
            self.assertTrue(ups[1][-1] == "300", ups[1])
            for argv in ups:
                self.assertEqual([argv[i + 1] for i, part in enumerate(argv) if part == "--profile"], ["ts-console", "ts-litellm", "ts-backplane"])
            created = [argv[-1] for argv in runner.calls if argv[:3] == ["docker", "volume", "create"]]
            self.assertEqual(created, ["platform-edge_edge-data", "platform-edge_edge-config", "platform-edge_ts-console",
                                       "platform-edge_ts-litellm", "platform-edge_ts-backplane"])
            # The selection is recorded, so plain Compose and ordinary reruns keep the nodes and the routes.
            self.assertTrue(env.read_text().endswith("COMPOSE_FILE=compose.yaml:compose.tailscale.yaml\nCOMPOSE_PROFILES=ts-console,ts-litellm,ts-backplane\n"))
            rerun = TailnetRunner()
            code, out2, err = run_main(["--env-file", str(env)], rerun)
            self.assertEqual(code, 0, err)
            self.assertEqual(sum(1 for argv in rerun.calls if argv[-3:] == ["tailscale", "status", "--json"]), 0)
            [up] = [argv for argv in rerun.calls if "up" in argv]
            self.assertEqual(up[up.index("--env-file") + 2:up.index("up")][-2:], ["-f", str(ROOT / "compose.tailscale.yaml")])
            self.assertEqual([argv[-1] for argv in rerun.calls if argv[:3] == ["docker", "volume", "create"]][2:],
                             ["platform-edge_ts-console", "platform-edge_ts-litellm", "platform-edge_ts-backplane"])
            self.assertEqual(json.loads(out2)["tailnet"]["origins"]["backplane"], "https://backplane.tail1234.ts.net")
            # A changed selection replaces the recorded ts- profiles and keeps the operator's own.
            env.write_text(env.read_text().replace("PE_TS_APPS=console,litellm,backplane", "PE_TS_APPS=console")
                           .replace("COMPOSE_PROFILES=ts-console,ts-litellm,ts-backplane", "COMPOSE_PROFILES=custom,ts-console,ts-litellm,ts-backplane"))
            code, _, err = run_main(["--env-file", str(env), "--tailscale"], TailnetRunner({"ts-console": "edge"}))
            self.assertEqual(code, 0, err)
            self.assertTrue(env.read_text().endswith("COMPOSE_FILE=compose.yaml:compose.tailscale.yaml\nCOMPOSE_PROFILES=custom,ts-console\n"))
            report = json.loads(out)["tailnet"]
            self.assertEqual(report["nodes"], ["ts-console", "ts-litellm", "ts-backplane"])
            self.assertEqual(report["probes"], "skipped: test")
            self.assertEqual(report["sibling_settings"]["gateway"], {"LG_CONSOLE_URL": "https://edge.tail1234.ts.net",
                                                                     "LG_LITELLM_URL": "https://litellm.tail1234.ts.net",
                                                                     "LG_BACKPLANE_URL": "https://backplane.tail1234.ts.net"})
            self.assertEqual(report["sibling_settings"]["backplane"], {"BP_PUBLIC_URL": "https://backplane.tail1234.ts.net"})
            self.assertNotIn("tskey", out + err)
            code, out, _ = run_main(["--env-file", str(env), "--tailscale", "--dry-run"], TailnetRunner())
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["tailnet"]["origins"]["console"], "https://edge.tail1234.ts.net")
            self.assertNotIn("tskey", out)

    def test_enrollment_parses_status_and_records_the_domain_once(self):
        self.assertIsNone(tailnet.enrolled(status("", state="NeedsLogin"), "litellm"))
        self.assertIsNone(tailnet.enrolled("not json", "litellm"))
        self.assertEqual(tailnet.enrolled(status("litellm"), "litellm"), "tail1234.ts.net")
        with self.assertRaises(bootstrap.Refused) as caught:
            tailnet.enrolled(status("litellm-1"), "litellm")
        self.assertEqual(caught.exception.code, "tailscale_name_taken")
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env = Path(directory) / ".env"
            env.write_text("# keep\nPE_TS_AUTHKEY='tskey-auth-test'\nPE_TS_APPS=console,grafana\nPE_TAILNET_DOMAIN=\nCUSTOM=1\n")
            runner = TailnetRunner({"ts-console": "platform"})
            code, out, err = run_main(["--env-file", str(env), "--tailscale"], runner)
            self.assertEqual(code, 0, err)
            self.assertEqual(env.read_text(), "# keep\nPE_TS_AUTHKEY='tskey-auth-test'\nPE_TS_APPS=console,grafana\nPE_TAILNET_DOMAIN=tail1234.ts.net\nCUSTOM=1\n"
                                              "COMPOSE_FILE=compose.yaml:compose.tailscale.yaml\nCOMPOSE_PROFILES=ts-console,ts-grafana\n")
            self.assertEqual(json.loads(out)["tailnet"]["domain"], "tail1234.ts.net")
            self.assertEqual(sum(1 for argv in runner.calls if argv[-3:] == ["tailscale", "status", "--json"]), 4)
            modified = env.stat().st_mtime_ns
            code, _, err = run_main(["--env-file", str(env), "--tailscale"], TailnetRunner({"ts-console": "platform"}))
            self.assertEqual(code, 0, err)
            self.assertEqual(env.stat().st_mtime_ns, modified)
            env.write_text("PE_TS_AUTHKEY=tskey-auth-test\nPE_TS_APPS=console\nPE_TAILNET_DOMAIN=other.ts.net\n")
            code, _, err = run_main(["--env-file", str(env), "--tailscale"], TailnetRunner({"ts-console": "platform"}))
            self.assertEqual((code, json.loads(err)["error"]), (1, "tailscale_domain_mismatch"))
            # A failed enrollment records nothing; a recorded selection without a domain is refused.
            self.assertNotIn("COMPOSE_", env.read_text())
            env.write_text("PE_TS_APPS=console\nCOMPOSE_FILE=compose.yaml:compose.tailscale.yaml\nCOMPOSE_PROFILES=ts-console\n")
            code, _, err = run_main(["--env-file", str(env)], TailnetRunner())
            self.assertEqual((code, json.loads(err)["error"]), (1, "tailnet_not_enrolled"))
            env.write_text("PE_TS_APPS=console\nPE_TAILNET_DOMAIN=tail1234.ts.net\nPE_BIND_HOST=0.0.0.0\nCOMPOSE_FILE=compose.yaml:compose.tailscale.yaml\nCOMPOSE_PROFILES=ts-console\n")
            code, _, err = run_main(["--env-file", str(env)], TailnetRunner())
            self.assertEqual((code, json.loads(err)["error"]), (1, "invalid_settings"))
            settings = bootstrap.settings_for({"PE_TS_APPS": "console,grafana"})
            timeouts = []
            def stalled(argv, timeout):
                timeouts.append(timeout)
                return subprocess.CompletedProcess(argv, 1, "", "")
            # The deadline bounds every poll: the last exec gets the remaining budget and nothing runs after it.
            with patch.object(tailnet.time, "monotonic", side_effect=[0, 0, 100, 115, 116, 130, 130]), patch.object(tailnet.time, "sleep"), \
                    self.assertRaises(bootstrap.Refused) as caught:
                tailnet.wait_enrolled(stalled, ["dc"], settings, ("console", "grafana"))
            self.assertEqual(caught.exception.code, "not_ready")
            self.assertEqual(timeouts, [30.0, 20.0, 4.0])


if __name__ == "__main__":
    unittest.main()
