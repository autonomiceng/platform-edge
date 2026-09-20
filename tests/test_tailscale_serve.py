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

    def test_console_denial_is_reported_without_hiding_wrong_origins_or_server_errors(self):
        from unittest.mock import patch
        from urllib.error import HTTPError
        endpoint = {"url": "https://host.tail123.ts.net:8450/"}
        for status, origin in ((401, endpoint["url"]), (403, endpoint["url"]), (404, endpoint["url"]),
                               (500, endpoint["url"]), (404, "https://other.example/"),
                               (404, "http://host.tail123.ts.net:8450/")):
            with self.subTest(status=status, origin=origin), patch.object(
                    tailscale_serve.urllib.request, "urlopen", side_effect=HTTPError(origin, status, "denied", {}, None)):
                if status < 500 and origin == endpoint["url"]:
                    self.assertEqual(tailscale_serve.verify_application("backplane_rustfs", endpoint,
                        allowed_denials=(401, 403, 404)), "access_denied")
                else:
                    with self.assertRaises(ValueError):
                        tailscale_serve.verify_application("backplane_rustfs", endpoint, allowed_denials=(401, 403, 404))
        with patch.object(tailscale_serve.urllib.request, "urlopen", side_effect=HTTPError(endpoint["url"], 404, "denied", {}, None)):
            with self.assertRaises(ValueError):
                tailscale_serve.verify_application("backplane", endpoint)

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
        for invalid in ({"PE_TAILSCALE_PORT": "8443"}, {"PE_BIND_HOST": "0.0.0.0"}, {"PE_TAILSCALE_S3_PORT": "65536"}, {"PE_TAILSCALE_RUSTFS_PORT": "8448"}):
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

class ConsoleSetupTests(unittest.TestCase):
    def test_selected_consoles_preserve_profiles_credentials_and_allowlists(self):
        import contextlib
        import io
        import json
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            edge, bp, ob, absent = [root / name for name in ('edge', 'bp', 'ob', 'absent')]
            for path in (edge, bp, ob):
                path.mkdir()
            (edge / '.env').write_text('PE_ACCESS_MODE=local\n')
            (bp / '.env').write_text("SECRET='literal$unchanged'\nCOMPOSE_FILE=compose.yaml:custom.yaml:compose.blobs.yaml:compose.gateway.yaml\nCOMPOSE_PROFILES=blobs,compute,gateway\nBP_RUSTFS_CONSOLE=true\nBP_BLOB_BACKEND=s3\nBP_RUSTFS_CONSOLE_ALLOW=100.64.0.9/32\n")
            (ob / '.env').write_text('COMPOSE_FILE=compose.yaml:compose.s3.yaml\nCOMPOSE_PROFILES=s3\nOB_RUSTFS_CONSOLE=true\nOB_RUSTFS_CONSOLE_ALLOW=100.64.0.10/32\n')
            originals = {p: p.read_bytes() for p in root.glob('*/.env')}
            commands = []

            def checked(argv):
                commands.append(argv)
                if argv[:2] == ['tailscale', 'status']:
                    return json.dumps(ServeTests.status)
                if argv[:3] == ['tailscale', 'serve', 'status']:
                    return '{}'
                if argv[:3] == ['docker', 'network', 'inspect']:
                    return json.dumps([{'IPAM': {'Config': [{'Gateway': '172.18.0.1', 'Subnet': '172.18.0.0/16'}]},
                                        'Containers': {'c' * 64: {'IPv4Address': '172.18.0.2/16'}}}])
                if argv[:2] == ['docker', 'ps']:
                    return 'c' * 12
                if argv[:2] == ['docker', 'compose'] and 'ps' in argv:
                    return 'server\nedge\ncaddy\nrustfs\n'
                self.fail('unexpected command ' + ' '.join(argv))

            args = ['--env-file', str(edge / '.env'), '--backplane-dir', str(bp), '--observability-dir', str(ob),
                    '--gateway-dir', str(absent), '--dry-run']
            for extra, explicit in (([], False), (['--console-allow', '100.64.0.0/10 fd7a:115c:a1e0::/48'], True)):
                output = io.StringIO()
                with patch.object(tailscale_serve, 'checked', checked), contextlib.redirect_stdout(output):
                    self.assertEqual(tailscale_serve.main(args + extra), 0)
                result = json.loads(output.getvalue())
                self.assertEqual(result['links']['backplane_rustfs'], 'https://host.tail123.ts.net:8450/rustfs/console/')
                self.assertEqual(result['links']['observability_rustfs'], 'https://host.tail123.ts.net:8451/rustfs/console/')
                settings = {Path(change['env']).parent.name: change['settings'] for change in result['changes']}
                self.assertEqual(settings['edge']['PE_TRUSTED_PROXIES'], '172.18.0.1')
                self.assertEqual(settings['bp']['BP_TRUSTED_PROXIES'], '172.18.0.2')
                self.assertEqual(settings['bp']['BP_RUSTFS_AUTHORITY'], 'host.tail123.ts.net:8450')
                self.assertEqual(settings['bp']['COMPOSE_FILE'], 'compose.yaml:custom.yaml:compose.blobs.yaml:compose.gateway.yaml')
                self.assertNotIn('COMPOSE_PROFILES', settings['bp'])
                self.assertNotIn('BP_RUSTFS_CONSOLE', settings['bp'])
                for name, prefix in [('bp', 'BP'), ('ob', 'OB')]:
                    if explicit:
                        self.assertEqual(settings[name][prefix + '_RUSTFS_CONSOLE_ALLOW'], extra[1])
                    else:
                        self.assertNotIn(prefix + '_RUSTFS_CONSOLE_ALLOW', settings[name])
                self.assertEqual(originals, {p: p.read_bytes() for p in originals})
            (ob / '.env').write_text(originals[ob / '.env'].decode().replace('OB_RUSTFS_CONSOLE=true', 'OB_RUSTFS_CONSOLE=false'))
            output = io.StringIO()
            with patch.object(tailscale_serve, 'checked', checked), contextlib.redirect_stdout(output):
                self.assertEqual(tailscale_serve.main(args), 0)
            self.assertNotIn('observability_rustfs', json.loads(output.getvalue())['links'])
            (bp / '.env').write_text(originals[bp / '.env'].decode().replace('blobs,compute,gateway', 'compute,gateway'))
            with patch.object(tailscale_serve, 'checked', checked), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(tailscale_serve.main(args), 1)
            self.assertFalse(any('up' in command for command in commands))

    def test_exact_proxy_peers_and_new_ports_are_validated(self):
        for value in ('172.18.0.1', '172.18.0.1/32 2001:db8::1/128'):
            tailscale_serve.bootstrap.settings_for({'PE_TRUSTED_PROXIES': value})
        for value in ('172.18.0.0/16', '100.64.0.0/10', 'private_ranges', 'host.example', '127.0.0.1 {'):
            with self.assertRaises(tailscale_serve.bootstrap.Refused):
                tailscale_serve.bootstrap.settings_for({'PE_TRUSTED_PROXIES': value})
        for settings in ({'PE_BIND_HOST': '0.0.0.0'}, {'PE_ACCESS_MODE': 'public', 'PE_PUBLIC_DOMAIN': 'example.com'}):
            with self.assertRaises(tailscale_serve.bootstrap.Refused):
                tailscale_serve.bootstrap.settings_for(dict(settings, PE_TRUSTED_PROXIES='172.18.0.1'))
        for key, value in [('PE_TAILSCALE_BACKPLANE_RUSTFS_PORT', '8448'), ('PE_TAILSCALE_OBSERVABILITY_RUSTFS_PORT', '8450')]:
            with self.assertRaises(tailscale_serve.bootstrap.Refused):
                tailscale_serve.bootstrap.settings_for({'PE_TAILSCALE_HOST': 'host.tail123.ts.net', key: value})
