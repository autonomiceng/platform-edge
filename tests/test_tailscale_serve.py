"""Serve ownership and protocol contract without changing the host's Tailscale state."""
import importlib.util
import sys
import unittest
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import tailscale_serve


class ServeTests(unittest.TestCase):
    status = {"BackendState": "Running", "Self": {"DNSName": "host.tail123.ts.net."}}

    def test_https_port_maps_to_http_backend_and_preserves_other_endpoints(self):
        serve = {"TCP": {"3773": {"HTTPS": True}}, "Web": {"host.tail123.ts.net:3773": {"Handlers": {"/": {"Proxy": "http://localhost:3773"}}}}}
        result = tailscale_serve.plan(self.status, serve, 8443, 80)
        self.assertEqual(result["url"], "https://host.tail123.ts.net:8443/")
        self.assertEqual(result["command"][-1], "http://127.0.0.1:80")
        self.assertIn("--https=8443", result["command"])
        self.assertIn("3773", serve["TCP"])

    def test_replacing_root_requires_explicit_intent(self):
        serve = {"TCP": {"443": {"HTTPS": True}}, "Web": {"host.tail123.ts.net:443": {"Handlers": {"/": {"Proxy": "http://localhost:443"}}}}}
        with self.assertRaisesRegex(ValueError, "already exists"):
            tailscale_serve.plan(self.status, serve, 443, 80)
        self.assertEqual(tailscale_serve.plan(self.status, serve, 443, 80, True)["url"], "https://host.tail123.ts.net/")

    def test_funnel_and_non_https_ports_are_not_adopted(self):
        for serve in ({"AllowFunnel": {"host.tail123.ts.net:443": True}}, {"TCP": {"443": {"TCPForward": "localhost:443"}}}):
            with self.assertRaises(ValueError):
                tailscale_serve.plan(self.status, serve, 443, 80, True)

    def test_custom_paths_cannot_shadow_application_routes(self):
        serve = {"Web": {"host.tail123.ts.net:8443": {"Handlers": {"/ui": {"Proxy": "http://localhost:1234"}}}}}
        with self.assertRaisesRegex(ValueError, "custom path"):
            tailscale_serve.plan(self.status, serve, 8443, 80, True)

    def test_environment_update_preserves_secrets_and_overlays(self):
        source = "# private\nSECRET='literal$unchanged'\nexport LG_SCHEME=http\nCOMPOSE_FILE=compose.yaml:custom.yaml\n"
        result = tailscale_serve.amended(source, {"LG_SCHEME": "https", "LG_TRUSTED_PROXIES": "172.18.0.7"})
        self.assertIn("SECRET='literal$unchanged'", result)
        self.assertIn("COMPOSE_FILE=compose.yaml:custom.yaml", result)
        self.assertIn("export LG_SCHEME=https", result)
        self.assertNotIn("LG_SCHEME=http\n", result)

    def test_tailscale_ports_must_be_unique_and_loopback_only(self):
        good = {"PE_ACCESS_MODE": "proxy", "PE_TAILSCALE_HOST": "host.tail123.ts.net"}
        tailscale_serve.bootstrap.settings_for(good)
        for invalid in ({"PE_TAILSCALE_PORT": "8443"}, {"PE_BIND_HOST": "0.0.0.0"}, {"PE_TAILSCALE_S3_PORT": "65536"}):
            with self.assertRaises(tailscale_serve.bootstrap.Refused):
                tailscale_serve.bootstrap.settings_for(dict(good, **invalid))

    def test_setup_refuses_different_existing_storage(self):
        rendered = {"services": {"grafana": {"image": "pinned", "volumes": [{"type": "volume", "source": "state", "target": "/data"}]}}, "volumes": {"state": {"name": "installed_state"}}}
        container = {"Config": {"Image": "pinned"}, "Mounts": [{"Destination": "/data", "Name": "installed_state"}]}
        tailscale_serve.check_storage(rendered, "grafana", container)
        rendered["volumes"]["state"]["name"] = "empty_replacement"
        with self.assertRaisesRegex(ValueError, "persistent mount would change"):
            tailscale_serve.check_storage(rendered, "grafana", container)

    def test_tailscale_keeps_local_http_and_https(self):
        settings = tailscale_serve.bootstrap.settings_for({"PE_ACCESS_MODE": "local", "PE_TAILSCALE_HOST": "host.tail123.ts.net"})
        self.assertEqual(settings["PE_TLS_ISSUER"], "internal")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / ".env"
            env.write_text("COMPOSE_FILE=compose.yaml:custom.yaml:compose.proxy.yaml:compose.tailscale.yaml\n")
            result = tailscale_serve.configuration(root, env, {"PE_TAILSCALE_HOST": "host.tail123.ts.net", "PE_ACCESS_MODE": "local"}, ["caddy"])
            self.assertEqual(result["files"], "compose.yaml:custom.yaml:compose.tailscale.yaml")
