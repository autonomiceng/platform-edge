#!/usr/bin/env python3
"""Bring platform-edge up from a clean checkout, or refuse with a reason.

Lock the env file, create the shared network, refuse conflicting published ports,
start Caddy, probe readiness, and print routed hostnames. No secrets are generated.
Exit codes: 0 ready, 1 refused, 2 bad usage, 3 not ready. Python 3.11+ stdlib only.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

PROJECT = "platform-edge"
NETWORK = "platform"
RESTORE_MARKER = ".pe-restore-incomplete"
ENV_LINE = re.compile(r"^(?:export\s+)?(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")
ROUTE_LINE = re.compile(r"^\{\$PE_SCHEME\}://(?P<host>(?:[a-z0-9-]+\.)*\{\$PE_PUBLIC_DOMAIN\})\s*\{$")
PORT = re.compile(r"(?P<host>\[[^]]+\]|[^, ]+):(?P<first>\d+)(?:-(?P<last>\d+))?->[^, ]+/tcp")
DEFAULTS = {
    "PE_PUBLIC_DOMAIN": "localhost",
    "PE_SCHEME": "http",
    "PE_TLS_ISSUER": "none",
    "PE_BIND_HOST": "127.0.0.1",
    "PE_HTTP_PORT": "80",
    "PE_HTTPS_PORT": "443",
    "PE_PLATFORM_NETWORK": NETWORK,
    "PE_ACME_EMAIL": "",
    "PE_VOLUME_PREFIX": PROJECT,
    "PE_BACKUP_DIR": "./backups",
    "PE_BACKUP_KEEP": "7",
    "PE_METRICS_ALLOW": "127.0.0.0/8 ::1",
}
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class Refused(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def run(argv: list[str], *, start_new_session: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, text=True, capture_output=True, check=False, start_new_session=start_new_session)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ENV_LINE.match(line)
        if not match:
            continue
        key, value = match.group("key"), match.group("value").strip()
        if key not in DEFAULTS and key != "COMPOSE_PROJECT_NAME":
            continue
        if key in values:
            raise Refused("env_repair_required", f"{key} is set twice in {path}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if "$" in value or "#" in value or (key != "PE_METRICS_ALLOW" and any(c.isspace() for c in value)):
            raise Refused("env_repair_required", f"{key} must be a plain value in {path}")
        values[key] = value
    return values


def settings_for(values: dict[str, str]) -> dict[str, str]:
    # Compose shell overrides and empty-value defaults must match the preflight.
    settings = {
        key: os.environ.get(key, values.get(key, default)) or default
        for key, default in DEFAULTS.items()
    }
    scheme, issuer = settings["PE_SCHEME"], settings["PE_TLS_ISSUER"]
    if (scheme, issuer) not in {("http", "none"), ("https", "acme"), ("https", "internal")}:
        raise Refused("invalid_settings", "use http/none, https/acme, or https/internal")
    domain = settings["PE_PUBLIC_DOMAIN"]
    if len(domain) > 253 or not all(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                                   for label in domain.split(".")):
        raise Refused("invalid_settings", "PE_PUBLIC_DOMAIN must be a DNS hostname without a port")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise Refused("invalid_settings", "PE_PUBLIC_DOMAIN must be a DNS hostname, not an IP address")
    for key in ("PE_HTTP_PORT", "PE_HTTPS_PORT"):
        if not settings[key].isdigit() or not 1 <= int(settings[key]) <= 65535:
            raise Refused("invalid_settings", f"{key} must be a port from 1 to 65535")
    if int(settings["PE_HTTP_PORT"]) == int(settings["PE_HTTPS_PORT"]):
        raise Refused("invalid_settings", "HTTP and HTTPS must use different host ports")
    try:
        ipaddress.ip_address(settings["PE_BIND_HOST"].strip("[]"))
    except ValueError as error:
        raise Refused("invalid_settings", "PE_BIND_HOST must be an IP address") from error
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", settings["PE_PLATFORM_NETWORK"]):
        raise Refused("invalid_settings", "PE_PLATFORM_NETWORK must be a Docker network name")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", settings["PE_VOLUME_PREFIX"]):
        raise Refused("invalid_settings", "PE_VOLUME_PREFIX must be a Docker volume prefix")
    if not settings["PE_BACKUP_KEEP"].isdigit() or int(settings["PE_BACKUP_KEEP"]) < 1:
        raise Refused("invalid_settings", "PE_BACKUP_KEEP must be at least 1")
    try:
        for network in settings["PE_METRICS_ALLOW"].split():
            ipaddress.ip_network(network, strict=False)
        if not settings["PE_METRICS_ALLOW"].split():
            raise ValueError("empty allow list")
    except ValueError as error:
        raise Refused("invalid_settings", "PE_METRICS_ALLOW must contain IP addresses or CIDRs") from error
    return settings


def routed_hostnames(routes: Path, domain: str) -> list[str]:
    hosts = set()
    for path in sorted(routes.glob("*.caddy")):
        for line in path.read_text(encoding="utf-8").splitlines():
            match = ROUTE_LINE.fullmatch(line)
            if match:
                hosts.add(match.group("host").replace("{$PE_PUBLIC_DOMAIN}", domain))
    return sorted(hosts)


def check_ports(runner: Runner, settings: dict[str, str], project: str) -> None:
    result = runner(["docker", "ps", "--format", "json"])
    if result.returncode != 0:
        raise Refused("docker_unavailable", result.stderr.strip())
    wanted = {int(settings["PE_HTTP_PORT"]), int(settings["PE_HTTPS_PORT"])}
    bind = settings["PE_BIND_HOST"].strip("[]")
    for line in result.stdout.splitlines():
        container = json.loads(line)
        labels = dict(part.split("=", 1) for part in container.get("Labels", "").split(",") if "=" in part)
        if labels.get("com.docker.compose.project") == project and labels.get("com.docker.compose.service") == "caddy":
            continue
        for match in PORT.finditer(container.get("Ports", "")):
            address = match.group("host").strip("[]")
            first = int(match.group("first"))
            last = int(match.group("last") or first)
            overlaps = address == bind or address in {"0.0.0.0", "::"} or bind in {"0.0.0.0", "::"}
            conflicts = sorted(port for port in wanted if first <= port <= last)
            if overlaps and conflicts:
                raise Refused("port_conflict", f"container {container['Names']} ({container['ID']}) "
                              f"already publishes {address}:{conflicts[0]}/tcp; move its published port before starting the edge")


def ensure_network(runner: Runner, name: str = NETWORK) -> None:
    probe = runner(["docker", "network", "inspect", name])
    if probe.returncode == 0:
        return
    created = runner(["docker", "network", "create", name])
    if created.returncode != 0:
        raise Refused("network_create_failed", created.stderr.strip())


def compose_up(root: Path, env_file: Path, runner: Runner) -> None:
    result = runner([
        "docker", "compose", "--project-directory", str(root), "--env-file", str(env_file),
        "up", "--detach", "--wait", "--wait-timeout", "300",
    ])
    if result.returncode != 0:
        raise Refused("compose_up_failed", (result.stderr or result.stdout).strip()[-2000:])


def volume_names(settings: dict[str, str]) -> list[str]:
    return [f"{settings['PE_VOLUME_PREFIX']}_{suffix}" for suffix in ("edge-data", "edge-config")]


def ensure_volumes(runner: Runner, settings: dict[str, str]) -> None:
    for name in volume_names(settings):
        result = runner(["docker", "volume", "create", name])
        if result.returncode:
            raise Refused("volume_create_failed", result.stderr.strip())


def compose_command(root: Path, env_file: Path) -> list[str]:
    return ["docker", "compose", "--project-directory", str(root), "--env-file", str(env_file)]


def ca_fingerprint(pem: str) -> str:
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def probe(settings: dict[str, str], host: str, ca: str = "", path: str = "/health") -> dict:
    secure = settings["PE_SCHEME"] == "https"
    port = int(settings["PE_HTTPS_PORT" if secure else "PE_HTTP_PORT"])
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    certificate = {}
    try:
        connection.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        if secure:
            context = ssl.create_default_context(cadata=ca) if ca else ssl.create_default_context()
            connection.sock = context.wrap_socket(connection.sock, server_hostname=host)
            leaf = connection.sock.getpeercert()
            certificate = {"not_after": leaf["notAfter"],
                           "not_after_seconds": ssl.cert_time_to_seconds(leaf["notAfter"])}
        connection.request("GET", path, headers={"Host": host})
        response = connection.getresponse()
        if response.status != 200:
            raise OSError(f"{host}{path}: HTTP {response.status}")
        return certificate
    finally:
        connection.close()


def wait_ready(settings: dict[str, str], root: Path, env_file: Path,
               runner: Runner = run, timeout: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout
    last = ""
    dc = compose_command(root, env_file)
    while time.monotonic() < deadline:
        try:
            ca = ""
            if settings["PE_TLS_ISSUER"] == "internal":
                result = runner(dc + ["exec", "-T", "caddy", "cat",
                                      "/data/caddy/pki/authorities/local/root.crt"])
                if result.returncode:
                    raise OSError("internal CA root is not available in edge-data")
                ca = result.stdout
            certificate = probe(settings, settings["PE_PUBLIC_DOMAIN"], ca)
            if ca:
                certificate["ca_sha256"] = ca_fingerprint(ca)
            if certificate:
                metric = (f'pe_certificate_not_after_seconds {certificate["not_after_seconds"]:.0f}\n'
                          f'pe_certificate_checked_seconds {time.time():.0f}\n')
                result = runner(dc + ["exec", "-T", "caddy", "sh", "-ec",
                                     'printf "%s" "$1" > /config/pe-certificate.prom.tmp; '
                                     'mv /config/pe-certificate.prom.tmp /config/pe-certificate.prom',
                                     "sh", metric])
                if result.returncode:
                    raise OSError("cannot publish certificate expiry metric")
            else:
                result = runner(dc + ["exec", "-T", "caddy", "rm", "-f", "/config/pe-certificate.prom"])
                if result.returncode:
                    raise OSError("cannot clear HTTPS metric in HTTP mode")
            return certificate
        except (OSError, ValueError, http.client.HTTPException) as error:
            last = str(error)
        time.sleep(3)
    raise Refused("not_ready", last)


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(json.dumps({"error": "bad_usage", "detail": message}), file=sys.stderr)
        raise SystemExit(2)


def bootstrap(argv: list[str], runner: Runner = run) -> int:
    parser = ArgumentParser(prog="bootstrap.py", description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--template", default=".env.example")
    parser.add_argument("--render-only", action="store_true", help="write only the env file, start nothing")
    parser.add_argument("--probe-only", action="store_true", help="refresh readiness and expiry without starting services")
    args = parser.parse_args(argv)
    if args.render_only and args.probe_only:
        parser.error("--render-only and --probe-only are mutually exclusive")
    root = Path(__file__).resolve().parent.parent
    env_file = (root / args.env_file).resolve()
    template = (root / args.template).resolve()
    if shutil.which("docker") is None and not args.render_only:
        raise Refused("docker_missing", "install Docker with the Compose plugin")

    # Lock the env inode itself: render-only creates no lock file or console state.
    fd = os.open(env_file, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Refused("bootstrap_already_running", str(env_file)) from error
        if not handle.read():
            handle.write(template.read_text(encoding="utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.fchmod(handle.fileno(), 0o600)
        values = read_env(env_file)
        settings = settings_for(values)
        project = os.environ.get("COMPOSE_PROJECT_NAME") or values.get("COMPOSE_PROJECT_NAME") or PROJECT
        if args.render_only:
            print(json.dumps({"env": str(env_file), "project": project, "generated": []}))
            return 0
        if not args.probe_only:
            check_ports(runner, settings, project)
            ensure_network(runner, settings["PE_PLATFORM_NETWORK"])
            ensure_volumes(runner, settings)
            result = runner(compose_command(root, env_file) + [
                "run", "--rm", "--no-deps", "--entrypoint", "sh", "caddy", "-ec",
                f"test ! -e /data/{RESTORE_MARKER} && test ! -e /config/{RESTORE_MARKER}",
            ])
            if result.returncode:
                raise Refused("restore_incomplete", "restore markers present or state unreadable; preserve volumes and restore into a fresh prefix")
            compose_up(root, env_file, runner)
        certificate = wait_ready(settings, root, env_file, runner)
        print(json.dumps({
            "project": project,
            "scheme": settings["PE_SCHEME"],
            "certificate": certificate,
            "hostnames": routed_hostnames(root / "routes.d", settings["PE_PUBLIC_DOMAIN"]),
            "next": "Configure each stack for the shared edge; see docs/operations/ingress.md.",
        }))
        return 0


def main() -> int:
    try:
        return bootstrap(sys.argv[1:])
    except Refused as refused:
        print(json.dumps({"error": refused.code, "detail": refused.detail}), file=sys.stderr)
        return 3 if refused.code in ("not_ready", "compose_up_failed") else 1
    except OSError as error:
        print(json.dumps({"error": "io_error", "detail": str(error)}), file=sys.stderr)
        return 1
    except SystemExit as exit_:
        return 2 if exit_.code not in (0, None) else 0


if __name__ == "__main__":
    raise SystemExit(main())
