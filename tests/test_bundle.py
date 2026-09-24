"""Bundle installer contract: sibling env rewrites and bootstrap hand-off; every process call is a fake."""

import argparse
import contextlib
import fcntl
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
import bundle
from test_bootstrap import FakeRunner

EDGE = dict(bootstrap.settings_for({"PE_PUBLIC_DOMAIN": "example.com"}), PE_SCHEME="https")
GATEWAY_ENV = ("# Default public domain.\r\nLG_PUBLIC_DOMAIN=localhost\r\n\nLG_ACCESS_MODE=local\nexport LG_SCHEME=''\n"
               "LITELLM_MASTER_KEY='sk-secret$1'\nLG_HTTP_PORT=80\nCUSTOM=kept")
# Without Tailnet Origins the gateway derives its browser URLs from its public domain.
EMPTY_ORIGINS = {key: "" for key in bundle.ORIGIN_KEYS["gateway"]}


def checkout(root, stack, env=None):
    directory = root / bundle.STACKS[stack][1]
    entrypoint = directory / bundle.STACKS[stack][2]
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("")
    (directory / ".env.example").write_text(f"# template\n{bundle.STACKS[stack][0]}_ACCESS_MODE=local\n")
    if env is not None:
        (directory / ".env").write_bytes(env.encode())
    return directory


def arguments(root, *stacks, capability=None):
    directories = {stack + "_dir": root / bundle.STACKS[stack][1] if stack in stacks else None for stack in bundle.ORDER}
    return argparse.Namespace(stacks=list(stacks), capability_file=capability, **directories)


class Runs:
    """Records sibling bootstrap invocations; exit codes by checkout name."""

    def __init__(self, codes=()):
        self.codes = dict(codes)
        self.calls = []

    def __call__(self, argv, directory):
        self.calls.append((argv, directory))
        return self.codes.get(directory.name, 0)


def run_main(argv, runner):
    """bootstrap.main with the Edge Docker runner faked; returns exit code, stdout, stderr."""
    out, err = io.StringIO(), io.StringIO()
    with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", ["bootstrap.py", *argv]), \
            patch.object(shutil, "which", return_value="/usr/bin/tool"), patch.object(bootstrap, "wait_ready", return_value={}), \
            patch.object(bootstrap, "bootstrap", functools.partial(bootstrap.bootstrap, runner=runner)), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = bootstrap.main()
    return code, out.getvalue(), err.getvalue()


class BundleTests(unittest.TestCase):
    def test_env_rewrite_preserves_unrelated_lines_and_sets_the_bundle_keys(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            root = Path(temporary)
            gateway = checkout(root, "gateway", GATEWAY_ENV)
            observability = checkout(root, "observability")
            runs = Runs()
            with patch.object(bundle, "run_bootstrap", runs), contextlib.redirect_stdout(io.StringIO()):
                bundle.install(bundle.plan(arguments(root, "gateway", "observability"), root, EDGE), ["--with", "gateway"])
            self.assertEqual((gateway / ".env").read_bytes().decode(),
                             "# Default public domain.\r\nLG_PUBLIC_DOMAIN=example.com\r\n\nLG_ACCESS_MODE=proxy\nexport LG_SCHEME=https\n"
                             "LITELLM_MASTER_KEY='sk-secret$1'\nLG_HTTP_PORT=18080\nCUSTOM=kept\n"
                             "LG_BIND_HOST=127.0.0.1\nLG_PUBLIC_PORT_SUFFIX=\nLG_PLATFORM_NETWORK=platform\nLG_PLATFORM_SUBNET=172.30.0.0/24\n"
                             "LG_PLATFORM_IP_RANGE=172.30.0.128/25\nLG_TRUSTED_PROXIES=172.30.0.2/32\nLG_CONSOLE_URL=\nLG_LITELLM_URL=\n"
                             "LG_LANGFUSE_URL=\nLG_S3_URL=\nLG_RUSTFS_URL=\nLG_METRICS=true\n")
            created = observability / ".env"
            self.assertEqual(created.stat().st_mode & 0o777, 0o600)
            self.assertTrue(created.read_text().startswith("# template\nOB_ACCESS_MODE=proxy\n"))
            for line in ("OB_HTTP_PORT=18180", "OB_GATEWAY_HEALTH_HOST=example.com", "OB_GATEWAY_URL=https://example.com",
                         "OB_BACKPLANE_URL=https://backplane.example.com", "OB_TRUSTED_PROXIES=172.30.0.2/32", "OB_GRAFANA_URL="):
                self.assertIn(line + "\n", created.read_text())
            self.assertEqual([(argv, directory.name) for argv, directory in runs.calls],
                             [([sys.executable, "scripts/bootstrap.py"], "llm-gateway-stack"),
                              ([sys.executable, "scripts/bootstrap.py"], "observability-stack")])

    def test_managed_values_change_only_when_different_and_reruns_write_nothing(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            root = Path(temporary)
            source = ("LG_ACCESS_MODE=proxy\nLG_SCHEME='https'\nLG_PUBLIC_DOMAIN=old.example\nLITELLM_MASTER_KEY=\n"
                      "POSTGRES_PASSWORD='p#ss'\nLG_PLATFORM_NETWORK=platform\nLG_PLATFORM_SUBNET=172.30.0.0/24\nLG_PLATFORM_IP_RANGE=172.30.0.128/25\n")
            gateway = checkout(root, "gateway", source)
            output = io.StringIO()
            with patch.object(bundle, "run_bootstrap", Runs()), contextlib.redirect_stdout(output):
                [item] = bundle.plan(arguments(root, "gateway"), root, EDGE)
                self.assertEqual(item["writes"], {"LG_PUBLIC_DOMAIN": "example.com", "LG_BIND_HOST": "127.0.0.1", "LG_HTTP_PORT": "18080",
                                                  "LG_PUBLIC_PORT_SUFFIX": "", "LG_TRUSTED_PROXIES": "172.30.0.2/32", **EMPTY_ORIGINS})
                bundle.install([item], [])
                text = (gateway / ".env").read_text()
                self.assertTrue(text.startswith("LG_ACCESS_MODE=proxy\nLG_SCHEME='https'\nLG_PUBLIC_DOMAIN=example.com\n"
                                                "LITELLM_MASTER_KEY=\nPOSTGRES_PASSWORD='p#ss'\n"))
                modified = (gateway / ".env").stat().st_mtime_ns
                [again] = bundle.plan(arguments(root, "gateway"), root, EDGE)
                self.assertEqual(again["writes"], {})
                bundle.install([again], [])
            self.assertEqual((gateway / ".env").stat().st_mtime_ns, modified)
            self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["written"], {})
            for broken in ("LG_SCHEME=a\nLG_SCHEME=b\n", "SECRET='first line\nLG_SCHEME=fragment'\n"):
                with self.assertRaises(bootstrap.Refused) as refused:
                    bundle.amended(broken, {"LG_SCHEME": "https"})
                self.assertEqual(refused.exception.code, "bundle_env_repair_required")
            overridden = dict(EDGE, PE_PLATFORM_SUBNET="10.9.0.0/24", PE_PLATFORM_IP_RANGE="10.9.0.128/25", PE_EDGE_IP="10.9.0.2")
            self.assertEqual({key: value for key, value in bundle.settings("gateway", overridden).items() if "PLATFORM" in key or "TRUSTED" in key},
                             {"LG_PLATFORM_NETWORK": "platform", "LG_PLATFORM_SUBNET": "10.9.0.0/24", "LG_PLATFORM_IP_RANGE": "10.9.0.128/25",
                              "LG_TRUSTED_PROXIES": "10.9.0.2/32"})

    def test_missing_checkout_is_refused_with_exit_1_before_any_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gateway = checkout(root, "gateway", GATEWAY_ENV)
            edge_env = root / "edge.env"
            runs, runner = Runs(), FakeRunner()
            with patch.object(bundle, "run_bootstrap", runs):
                code, _, err = run_main(["--env-file", str(edge_env), "--with", "gateway", "--gateway-dir", str(gateway),
                                         "--with", "observability", "--observability-dir", str(root / "missing")], runner)
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(err)["error"], "bundle_checkout_missing")
            self.assertFalse(edge_env.exists())
            self.assertEqual((gateway / ".env").read_bytes().decode(), GATEWAY_ENV)
            self.assertEqual(runs.calls, [])
            self.assertEqual(runner.calls, [])

    def test_sibling_failure_exits_3_with_the_rerun_and_leaves_later_stacks_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gateway = checkout(root, "gateway", GATEWAY_ENV)
            observability = checkout(root, "observability")
            edge_env = root / "edge.env"
            runs = Runs({"llm-gateway-stack": 1})
            argv = ["--env-file", str(edge_env), "--with", "gateway", "--gateway-dir", str(gateway),
                    "--with", "observability", "--observability-dir", str(observability)]
            with patch.object(bundle, "run_bootstrap", runs):
                code, out, err = run_main(argv, FakeRunner())
            self.assertEqual(code, 3, err)
            error = json.loads(err)
            self.assertEqual(error["error"], "sibling_bootstrap_failed")
            self.assertIn("gateway bootstrap exited 1 in " + str(gateway), error["detail"])
            self.assertIn("python3 scripts/bootstrap.py " + " ".join(argv), error["detail"])
            self.assertIn("LG_ACCESS_MODE=proxy\n", (gateway / ".env").read_text())
            self.assertFalse((observability / ".env").exists())
            self.assertEqual([directory.name for _, directory in runs.calls], ["llm-gateway-stack"])
            self.assertIn("Bundle stacks follow: gateway, observability", out)

    def test_dry_run_with_gateway_writes_nothing_and_prints_the_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gateway = checkout(root, "gateway", GATEWAY_ENV)
            edge_env = root / "edge.env"
            runs, runner = Runs(), FakeRunner()
            with patch.object(bundle, "run_bootstrap", runs):
                code, out, err = run_main(["--env-file", str(edge_env), "--dry-run", "--with", "gateway", "--gateway-dir", str(gateway)], runner)
                self.assertEqual(code, 0, err)
                edge, plan = [json.loads(line) for line in out.splitlines()]
                self.assertEqual((edge["access_mode"], edge["scheme"], edge["bundle"]), ("local", "http", ["gateway"]))
                self.assertEqual(plan["stack"], "gateway")
                self.assertEqual(plan["writes"], {"LG_ACCESS_MODE": "proxy", "LG_SCHEME": "https",
                                                  "LG_BIND_HOST": "127.0.0.1", "LG_HTTP_PORT": "18080", "LG_PUBLIC_PORT_SUFFIX": "",
                                                  "LG_PLATFORM_NETWORK": "platform", "LG_PLATFORM_SUBNET": "172.30.0.0/24",
                                                  "LG_PLATFORM_IP_RANGE": "172.30.0.128/25", "LG_TRUSTED_PROXIES": "172.30.0.2/32", **EMPTY_ORIGINS})
                self.assertEqual(plan["command"], [sys.executable, "scripts/bootstrap.py"])
                self.assertFalse(edge_env.exists())
                self.assertEqual((gateway / ".env").read_bytes().decode(), GATEWAY_ENV)
                self.assertEqual(runs.calls, [])
                self.assertFalse(any("up" in argv or "create" in argv for argv in runner.calls))
                code, out, err = run_main(["--env-file", str(edge_env), "--dry-run"], runner)
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["bundle"], [])
                # An empty Edge env is filled from the template at execution, so the plan reads the template.
                edge_env.write_text("")
                (root / "custom.env.example").write_text("PE_ACCESS_MODE=public\nPE_PUBLIC_DOMAIN=custom.example\nPE_BIND_HOST=0.0.0.0\n")
                code, out, err = run_main(["--env-file", str(edge_env), "--template", str(root / "custom.env.example"),
                                           "--dry-run", "--with", "gateway", "--gateway-dir", str(gateway)], runner)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out.splitlines()[1])["writes"]["LG_PUBLIC_DOMAIN"], "custom.example")
            self.assertEqual(edge_env.read_bytes(), b"")

    def test_backplane_command_with_and_without_a_recorded_profile_set(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            root = Path(temporary)
            backplane = checkout(root, "backplane", "COMPOSE_PROFILES=blobs,compute,gateway\nBP_AUTH_SECRET=s\n")
            (backplane / "compose.gateway.yaml").write_text("")
            capability = root / "cap"
            [item] = bundle.plan(arguments(root, "backplane", capability=capability), root, EDGE)
            self.assertEqual(item["command"], [sys.executable, "scripts/bootstrap.py", "--capability-file", str(capability.resolve()),
                                              "--access-mode", "proxy", "--public-url", "https://backplane.example.com"])
            self.assertEqual(item["writes"], {"BP_ACCESS_MODE": "proxy", "BP_PUBLIC_URL": "https://backplane.example.com", "BP_BIND_HOST": "127.0.0.1",
                                              "BP_PORT": "3000", "BP_PLATFORM_NETWORK": "platform", "BP_PLATFORM_SUBNET": "172.30.0.0/24",
                                              "BP_PLATFORM_IP_RANGE": "172.30.0.128/25", "BP_TRUSTED_PROXIES": "172.30.0.2/32"})
            # Edge reaches bp-server directly, so a recorded selection without the gateway profile is complete.
            (backplane / ".env").write_text("COMPOSE_PROFILES=blobs,compute\nBP_AUTH_SECRET=s\n")
            [item] = bundle.plan(arguments(root, "backplane", capability=capability), root, EDGE)
            self.assertNotIn("--profile", item["command"])
            # An installation that predates a recorded selection must name its profiles itself.
            (backplane / ".env").write_text("BP_AUTH_SECRET=s\n")
            with self.assertRaises(bootstrap.Refused) as refused:
                bundle.plan(arguments(root, "backplane", capability=capability), root, EDGE)
            self.assertEqual(refused.exception.code, "bundle_backplane_selection_required")
            (backplane / ".env").write_text("BP_AUTH_SECRET=\n")
            [fresh] = bundle.plan(arguments(root, "backplane", capability=capability), root, EDGE)
            self.assertEqual(fresh["command"][-2:], ["--profile", "gateway"])
            # Once the checkout drops its internal gateway overlay, the profile is neither passed nor accepted.
            (backplane / "compose.gateway.yaml").unlink()
            [direct] = bundle.plan(arguments(root, "backplane", capability=capability), root, EDGE)
            self.assertEqual(direct["command"], fresh["command"][:-2])
            (backplane / ".env").write_text("COMPOSE_PROFILES=blobs,compute,gateway\nBP_AUTH_SECRET=s\n")
            with self.assertRaises(bootstrap.Refused) as refused:
                bundle.plan(arguments(root, "backplane", capability=capability), root, EDGE)
            self.assertEqual(refused.exception.code, "bundle_gateway_profile_retired")
            self.assertIn("drop gateway from COMPOSE_PROFILES", refused.exception.detail)
            (backplane / ".env").write_text("BP_AUTH_SECRET=\n")
            with self.assertRaises(bootstrap.Refused) as refused:
                bundle.plan(arguments(root, "backplane"), root, EDGE)
            self.assertEqual(refused.exception.code, "bundle_capability_file")
            # A running Backplane bootstrap holds the flock on its .env.lock sidecar.
            with open(backplane / ".env.lock", "w") as held, patch.object(bundle, "run_bootstrap", Runs()), \
                    self.assertRaises(bootstrap.Refused) as refused:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                bundle.install([direct], ["--with", "backplane"])
            self.assertEqual(refused.exception.code, "sibling_bootstrap_failed")
            self.assertIn("was not started: " + str(backplane / ".env.lock"), refused.exception.detail)
            self.assertIn("rerun `python3 scripts/bootstrap.py --with backplane`", refused.exception.detail)
            self.assertEqual((backplane / ".env").read_text(), "BP_AUTH_SECRET=\n")
            for failure, text in ((subprocess.TimeoutExpired("python3", bundle.TIMEOUT), "ran longer than 1800 s"),
                                  (FileNotFoundError(2, "No such file", "python3"), "was not started: ")):
                with patch.object(bundle, "run_bootstrap", side_effect=failure), self.assertRaises(bootstrap.Refused) as refused:
                    bundle.install([direct], ["--with", "backplane"])
                self.assertEqual(refused.exception.code, "sibling_bootstrap_failed")
                self.assertIn(text, refused.exception.detail)
                self.assertIn("rerun `python3 scripts/bootstrap.py --with backplane`", refused.exception.detail)
            with patch.object(bundle, "run_bootstrap", Runs()), contextlib.redirect_stdout(io.StringIO()):
                bundle.install([direct], [])
            self.assertTrue((backplane / ".env").read_text().startswith("BP_AUTH_SECRET=\nBP_ACCESS_MODE=proxy\n"))

    def test_bundle_keys_carry_the_tailnet_origins_only_with_both_flags(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            root = Path(temporary)
            gateway = checkout(root, "gateway", GATEWAY_ENV)
            edge_env = root / "edge.env"
            edge_env.write_text("PE_TS_AUTHKEY=tskey-auth-test\nPE_TAILNET_DOMAIN=tail1234.ts.net\nPE_TS_APPS=console,litellm,langfuse,s3,rustfs,grafana\n")
            runs, runner = Runs(), FakeRunner()
            with patch.object(bundle, "run_bootstrap", runs):
                code, out, err = run_main(["--env-file", str(edge_env), "--dry-run", "--tailscale", "--with", "gateway", "--gateway-dir", str(gateway)], runner)
                self.assertEqual(code, 0, err)
                plan = json.loads(out.splitlines()[1])
                self.assertEqual({key: value for key, value in plan["writes"].items() if key.endswith("_URL")},
                                 {"LG_CONSOLE_URL": "https://platform.tail1234.ts.net", "LG_LITELLM_URL": "https://litellm.tail1234.ts.net",
                                  "LG_LANGFUSE_URL": "https://langfuse.tail1234.ts.net", "LG_S3_URL": "https://s3.tail1234.ts.net",
                                  "LG_RUSTFS_URL": "https://rustfs.tail1234.ts.net"})
                # The public domain stays the routing domain; only the browser origins move to the tailnet.
                self.assertEqual((plan["writes"]["LG_SCHEME"], "LG_PUBLIC_DOMAIN" in plan["writes"]), ("https", False))
                code, out, err = run_main(["--env-file", str(edge_env), "--dry-run", "--with", "gateway", "--gateway-dir", str(gateway)], runner)
                self.assertEqual(code, 0, err)
                self.assertEqual({key: value for key, value in json.loads(out.splitlines()[1])["writes"].items() if key.endswith("_URL")}, EMPTY_ORIGINS)
                # A recorded selection keeps the Tailnet Origins on ordinary bundle reruns.
                edge_env.write_text(edge_env.read_text() + "COMPOSE_FILE=compose.yaml:compose.tailscale.yaml\nCOMPOSE_PROFILES=ts-litellm\n")
                code, out, err = run_main(["--env-file", str(edge_env), "--dry-run", "--with", "gateway", "--gateway-dir", str(gateway)], runner)
                self.assertEqual(code, 0, err)
                writes = json.loads(out.splitlines()[1])["writes"]
                self.assertEqual((writes["LG_LITELLM_URL"], writes["LG_LANGFUSE_URL"]), ("https://litellm.tail1234.ts.net", ""))
            origins = {"console": "https://platform.tail1234.ts.net", "backplane": "https://backplane.tail1234.ts.net", "grafana": "https://grafana.tail1234.ts.net"}
            self.assertEqual(bundle.tailnet_settings("observability", origins),
                             {"OB_GRAFANA_URL": "https://grafana.tail1234.ts.net", "OB_GATEWAY_URL": "https://platform.tail1234.ts.net",
                              "OB_BACKPLANE_URL": "https://backplane.tail1234.ts.net"})
            self.assertEqual(bundle.settings("backplane", EDGE, origins)["BP_PUBLIC_URL"], "https://backplane.tail1234.ts.net")
            self.assertEqual(bundle.settings("backplane", EDGE, {"console": origins["console"]})["BP_PUBLIC_URL"], "https://backplane.example.com")
            self.assertEqual(bundle.settings("observability", EDGE, {"console": "https://<PE_TAILNET_DOMAIN>"})["OB_GATEWAY_URL"], "https://<PE_TAILNET_DOMAIN>")


if __name__ == "__main__":
    unittest.main()
