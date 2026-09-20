"""Serve ownership and protocol contract without changing the host's Tailscale state."""
import importlib.util
import sys
import unittest
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
