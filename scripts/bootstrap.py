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
import signal
import socket
import ssl
import subprocess
import sys
import time
import tempfile
from pathlib import Path
from typing import Callable

from status_io import Unavailable, directory, now, task_record


def record_bootstrap(root, env_file, started, state):
    try:
        # Create the read-only mount source before Compose can create it as root.
        with directory(root / "data/console"):
            pass
        task_record(root, env_file, started, state)
    except (OSError, Unavailable):
        print("Status execution record unavailable; check data directory ownership and permissions.", file=sys.stderr)

PROJECT = "platform-edge"
NETWORK = "platform"
RETIRED_OVERLAY = "compose.tailscale.yaml"
# Retired settings are read only to refuse values that contradict the fixed Edge address.
LEGACY = {"PE_TAILSCALE_EDGE_IP"}
RESTORE_MARKER = ".pe-restore-incomplete"
TLS_OVERLAYS = ("compose.files.yaml", "compose.acme-ca-root.yaml", "compose.acme-eab.yaml")
ENV_LINE = re.compile(r"^(?:export\s+)?(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")
# Route Files declare each host once with `import site <host> <routes>`; plain site addresses still count.
ROUTE_LINE = re.compile(r"^\s*(?:import site (?P<site>HOST) [a-z0-9-]+|https?://(?P<address>HOST)\s*\{)$"
                        .replace("HOST", r"(?:[a-z0-9-]+\.)*\{\$PE_PUBLIC_DOMAIN\}"))
SAN_NAME = re.compile(r"DNS:([^,\s]+)")
PORT = re.compile(r"(?P<host>\[[^]]+\]|[^, ]+):(?P<first>\d+)(?:-(?P<last>\d+))?->[^, ]+/tcp")
DEFAULTS = {
    "PE_CADDY_IMAGE": "",
    "PE_ACCESS_MODE": "local",
    "PE_PUBLIC_DOMAIN": "localhost",
    "PE_SCHEME": "http",
    "PE_BIND_HOST": "127.0.0.1",
    "PE_HTTP_PORT": "80",
    "PE_HTTPS_PORT": "443",
    "PE_PLATFORM_NETWORK": NETWORK,
    "PE_PLATFORM_SUBNET": "172.30.0.0/24",
    "PE_PLATFORM_IP_RANGE": "172.30.0.128/25",
    "PE_EDGE_IP": "172.30.0.2",
    "PE_TLS_ISSUER": "",
    "PE_ACME_EMAIL": "",
    "PE_ACME_CA": "",
    "PE_ACME_CA_ROOT": "",
    "PE_ACME_EAB_KEY_ID": "",
    "PE_ACME_EAB_HMAC": "",
    "PE_TLS_DIR": "",
    "PE_TLS_CA": "",
    "PE_TAILSCALE_HOST": "",
    "PE_TAILSCALE_APPS": "",
    "PE_TAILSCALE_PORT": "443",
    "PE_TAILSCALE_LITELLM_PORT": "8443",
    "PE_TAILSCALE_LANGFUSE_PORT": "8444",
    "PE_TAILSCALE_S3_PORT": "8445",
    "PE_TAILSCALE_GATEWAY_PORT": "8446",
    "PE_TAILSCALE_GRAFANA_PORT": "8447",
    "PE_TAILSCALE_BACKPLANE_PORT": "8448",
    "PE_TAILSCALE_RUSTFS_PORT": "8449",
    "PE_TAILSCALE_BACKPLANE_RUSTFS_PORT": "8450",
    "PE_TAILSCALE_OBSERVABILITY_RUSTFS_PORT": "8451",
    "PE_TRUSTED_PROXIES": "",
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


def run_detached(argv: list[str], *, timeout: float, stdin=subprocess.DEVNULL,
                 stdout=subprocess.PIPE, text: bool = False, env=None, stderr=subprocess.PIPE, cwd=None) -> subprocess.CompletedProcess:
    with subprocess.Popen(argv, stdin=stdin, stdout=stdout, stderr=stderr, env=env, cwd=cwd,
                          text=text, start_new_session=True) as child:
        try:
            output, error = child.communicate(timeout=timeout)
        except BaseException:
            # This session and its process group belong to the child we spawned.
            # Killing only the CLI can leave its plugin holding our pipes open.
            try:
                if child.returncode is None:
                    os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            raise
        return subprocess.CompletedProcess(argv, child.returncode, output, error)


def run(argv: list[str], *, start_new_session: bool = False,
        timeout: float | None = None, env=None, quiet: bool = False, cwd=None) -> subprocess.CompletedProcess[str]:
    # Compose startup has its own 300s health budget; allow image/startup overhead.
    budget = timeout if timeout is not None else (360 if {"up", "run"} & set(argv) else 120)
    try:
        options = {"env": env} if env is not None else {}
        if cwd is not None:
            options["cwd"] = cwd
        if quiet:
            options.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return run_detached(argv, timeout=budget, text=True, **options)
    except subprocess.TimeoutExpired:
        if start_new_session:
            raise
        raise Refused("docker_timeout", "Docker command exceeded its deadline") from None


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ENV_LINE.match(line)
        if not match:
            continue
        key, value = match.group("key"), match.group("value").strip()
        if key not in DEFAULTS and key not in LEGACY and key not in {"COMPOSE_PROJECT_NAME", "COMPOSE_FILE"}:
            continue
        if key in values:
            raise Refused("env_repair_required", f"{key} is set twice in {path}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if "$" in value or "#" in value or (key not in {"PE_METRICS_ALLOW", "PE_TRUSTED_PROXIES"} and any(c.isspace() for c in value)):
            raise Refused("env_repair_required", f"{key} must be a plain value in {path}")
        values[key] = value
    return values


def settings_for(values: dict[str, str]) -> dict[str, str]:
    # Compose shell overrides and empty-value defaults must match the preflight.
    settings = {
        key: os.environ.get(key, values.get(key, default)) or default
        for key, default in DEFAULTS.items()
    }
    mode = settings["PE_ACCESS_MODE"]
    if mode not in {"local", "public", "proxy"}:
        raise Refused("invalid_settings", "PE_ACCESS_MODE must be local, public, or proxy")
    settings["PE_TLS_ISSUER"] = tls_issuer(mode, settings["PE_TLS_ISSUER"])
    # Compose interpolates the raw .env value, so only names with a Caddy snippet may pass.
    if settings["PE_TLS_ISSUER"] not in {"local": ("internal", "files"), "public": ("acme", "files"), "proxy": ("",)}[mode]:
        raise Refused("invalid_settings", "PE_TLS_ISSUER must be internal or files in local mode and acme or files in public mode")
    if settings["PE_TLS_ISSUER"] == "files" and not settings["PE_TLS_DIR"]:
        raise Refused("invalid_settings", "PE_TLS_ISSUER=files needs PE_TLS_DIR, a directory holding tls.crt and tls.key")
    if settings["PE_TLS_ISSUER"] == "acme":
        if settings["PE_ACME_CA"] and not re.fullmatch(r"https://[^/\s]+(?:/\S*)?", settings["PE_ACME_CA"]):
            raise Refused("invalid_settings", "PE_ACME_CA must be an https:// ACME directory URL")
        if bool(settings["PE_ACME_EAB_KEY_ID"]) != bool(settings["PE_ACME_EAB_HMAC"]):
            raise Refused("invalid_settings", "PE_ACME_EAB_KEY_ID and PE_ACME_EAB_HMAC must be set together")
        # trusted_roots replaces Caddy's trust pool for the ACME server; the public default would fail.
        if settings["PE_ACME_CA_ROOT"] and not settings["PE_ACME_CA"]:
            raise Refused("invalid_settings", "PE_ACME_CA_ROOT needs PE_ACME_CA, the private ACME directory it trusts")
    configured_scheme = os.environ.get("PE_SCHEME", values.get("PE_SCHEME", ""))
    settings["PE_SCHEME"] = configured_scheme or ("http" if mode == "local" else "https")
    if settings["PE_SCHEME"] not in {"http", "https"} or (mode == "public" and settings["PE_SCHEME"] != "https"):
        raise Refused("invalid_settings", "PE_SCHEME must be http or https; public mode requires https")
    domain = settings["PE_PUBLIC_DOMAIN"]
    if domain.lower() == "pe-edge":
        raise Refused("invalid_settings", "pe-edge is reserved for internal metrics; choose an application domain")
    if mode == "public" and ("." not in domain or domain.lower().endswith(".localhost")):
        raise Refused("invalid_settings", "public mode needs your own domain, such as example.com")
    if len(domain) > 253 or not all(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                                   for label in domain.split(".")):
        raise Refused("invalid_settings", "PE_PUBLIC_DOMAIN must be a DNS hostname without a port")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise Refused("invalid_settings", "PE_PUBLIC_DOMAIN must be a DNS hostname, not an IP address")
    # Compose parses the base port mappings before applying the proxy override.
    for key in ("PE_HTTP_PORT", "PE_HTTPS_PORT"):
        if not settings[key].isdigit() or not 1 <= int(settings[key]) <= 65535:
            raise Refused("invalid_settings", f"{key} must be a port from 1 to 65535")
    if mode != "proxy" and int(settings["PE_HTTP_PORT"]) == int(settings["PE_HTTPS_PORT"]):
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
    try:
        subnet = ipaddress.IPv4Network(settings["PE_PLATFORM_SUBNET"])
        ip_range = ipaddress.IPv4Network(settings["PE_PLATFORM_IP_RANGE"])
        edge = ipaddress.IPv4Address(settings["PE_EDGE_IP"])
    except ValueError as error:
        raise Refused("invalid_settings", "PE_PLATFORM_SUBNET and PE_PLATFORM_IP_RANGE must be IPv4 networks and PE_EDGE_IP an IPv4 address") from error
    if not ip_range.subnet_of(subnet):
        raise Refused("invalid_settings", "PE_PLATFORM_IP_RANGE must lie inside PE_PLATFORM_SUBNET")
    if edge not in subnet or edge in (subnet.network_address, subnet.broadcast_address):
        raise Refused("invalid_settings", "PE_EDGE_IP must be a host address inside PE_PLATFORM_SUBNET")
    if edge in ip_range or edge == platform_gateway(subnet):
        raise Refused("invalid_settings", "PE_EDGE_IP must lie outside PE_PLATFORM_IP_RANGE and differ from the network gateway")
    legacy = os.environ.get("PE_TAILSCALE_EDGE_IP", values.get("PE_TAILSCALE_EDGE_IP", ""))
    if legacy and legacy != settings["PE_EDGE_IP"]:
        raise Refused("legacy_setting", f"PE_TAILSCALE_EDGE_IP={legacy} is retired; Edge always uses PE_EDGE_IP "
                      f"({settings['PE_EDGE_IP']}). Remove the setting, then rerun bootstrap after the network cutover in docs/operations/ingress.md")
    if settings["PE_TRUSTED_PROXIES"] and (settings["PE_BIND_HOST"] != "127.0.0.1" or settings["PE_ACCESS_MODE"] not in ("local", "proxy")):
        raise Refused("invalid_settings", "trusted ingress peers require a loopback-only local or proxy listener")
    for peer in settings["PE_TRUSTED_PROXIES"].split():
        try:
            address = ipaddress.ip_interface(peer)
            if address.network.prefixlen != address.max_prefixlen:
                raise ValueError("proxy range is not exact")
        except ValueError as error:
            raise Refused("invalid_settings", "PE_TRUSTED_PROXIES requires exact IP addresses") from error
    tail = settings["PE_TAILSCALE_HOST"]
    if tail:
        if not re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+\.ts\.net", tail):
            raise Refused("invalid_settings", "PE_TAILSCALE_HOST must be this machine's Tailscale hostname")
        if mode not in {"local", "proxy"} or settings["PE_BIND_HOST"] != "127.0.0.1":
            raise Refused("invalid_settings", "Tailscale requires local or proxy mode and a loopback HTTP listener")
        ports = [settings["PE_TAILSCALE_PORT"], *[settings["PE_TAILSCALE_" + app + "_PORT"] for app in ("LITELLM", "LANGFUSE", "S3", "GATEWAY", "GRAFANA", "BACKPLANE", "RUSTFS", "BACKPLANE_RUSTFS", "OBSERVABILITY_RUSTFS")]]
        if any(not port.isdigit() or not 1 <= int(port) <= 65535 for port in ports) or len(set(map(int, ports))) != len(ports):
            raise Refused("invalid_settings", "Tailscale HTTPS ports must be valid and distinct")
        if any(int(port) <= 1023 for port in ports[1:]):
            raise Refused("invalid_settings", "Tailscale application ports must be above 1023")
    return settings


def tls_issuer(mode: str, configured: str) -> str:
    """The effective issuer; behind another gateway there is no HTTPS listener, so it is empty."""
    if mode == "proxy":
        return ""
    return configured or {"public": "acme"}.get(mode, "internal")


def routed_hostnames(routes: Path, domain: str) -> list[str]:
    hosts = set()
    for path in sorted(routes.glob("*.caddy")):
        for line in path.read_text(encoding="utf-8").splitlines():
            match = ROUTE_LINE.fullmatch(line)
            if match:
                hosts.add((match.group("site") or match.group("address")).replace("{$PE_PUBLIC_DOMAIN}", domain))
    return sorted(hosts)


def certificate_covers(names: set[str], host: str) -> bool:
    return host in names or ("." in host and "*." + host.split(".", 1)[1] in names)


def mounted_tls_files(settings: dict[str, str]) -> list[str]:
    """Container paths of operator certificate inputs mounted by the selected overlays."""
    if settings["PE_TLS_ISSUER"] == "files":
        return ["/certs/tls.crt", "/certs/tls.key"]
    if settings["PE_TLS_ISSUER"] == "acme" and settings["PE_ACME_CA_ROOT"]:
        return ["/certs/acme-ca-root.crt"]
    return []


def trust_files(settings: dict[str, str]) -> list[str]:
    """Settings naming CA files the effective issuer uses."""
    issuer = settings["PE_TLS_ISSUER"]
    return [key for key, used in (("PE_TLS_CA", issuer in {"acme", "files"}), ("PE_ACME_CA_ROOT", issuer == "acme"))
            if used and settings[key]]


def check_tls_inputs(runner: Runner, settings: dict[str, str], root: Path) -> None:
    for key in trust_files(settings):
        path = root / settings[key]
        try:
            if not path.is_file():
                raise OSError("not a regular file")
            ssl.create_default_context(cadata=path.read_text(encoding="utf-8"))
        except (OSError, ValueError, ssl.SSLError) as error:
            raise Refused("invalid_settings", f"{key} ({path}) must be a readable PEM file holding CA certificates") from error
    if settings["PE_TLS_ISSUER"] != "files":
        return
    directory = root / settings["PE_TLS_DIR"]
    certificate = directory / "tls.crt"
    if not directory.is_dir() or not certificate.is_file() or not (directory / "tls.key").is_file():
        raise Refused("invalid_settings", f"PE_TLS_DIR ({directory}) must be a directory holding tls.crt and tls.key")
    if shutil.which("openssl") is None:
        raise Refused("openssl_missing", "install openssl; bootstrap reads the certificate's subject alternative names with it")
    result = runner(["openssl", "x509", "-in", str(certificate), "-noout", "-ext", "subjectAltName"])
    if result.returncode:
        raise Refused("invalid_settings", f"openssl cannot read {certificate} as a PEM certificate")
    names = {name.lower() for name in SAN_NAME.findall(result.stdout)}
    missing = [host for host in routed_hostnames(root / "routes.d", settings["PE_PUBLIC_DOMAIN"])
               if not certificate_covers(names, host.lower())]
    if missing:
        raise Refused("invalid_settings", f"{certificate} does not cover {', '.join(missing)}; its subject alternative names are "
                      + (", ".join(sorted(names)) or "empty"))


def check_ports(runner: Runner, settings: dict[str, str], project: str) -> None:
    result = runner(["docker", "ps", "--format", "json"])
    if result.returncode != 0:
        raise Refused("docker_unavailable", result.stderr.strip())
    wanted = {int(settings["PE_HTTP_PORT"])}
    if settings.get("PE_ACCESS_MODE") != "proxy":
        wanted.add(int(settings["PE_HTTPS_PORT"]))
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


def check_proxy_peer(runner: Runner, settings: dict[str, str]) -> None:
    if not settings["PE_TAILSCALE_HOST"] or not settings["PE_TRUSTED_PROXIES"]:
        return
    result = runner(["docker", "network", "inspect", settings["PE_PLATFORM_NETWORK"]])
    if result.returncode:
        raise Refused("docker_unavailable", "cannot verify the trusted ingress peer")
    configs = json.loads(result.stdout)[0].get("IPAM", {}).get("Config", [])
    gateways = {str(ipaddress.ip_address(item["Gateway"])) for item in configs if item.get("Gateway")
                and ipaddress.ip_address(item["Gateway"]).version == 4}
    peers = {str(ipaddress.ip_interface(peer).ip) for peer in settings["PE_TRUSTED_PROXIES"].split()}
    if len(gateways) != 1 or peers != gateways:
        raise Refused("invalid_settings", "Tailscale trusted peer must match the current host bridge gateway")


def platform_gateway(subnet: ipaddress.IPv4Network) -> ipaddress.IPv4Address:
    return subnet.network_address + 1


def check_network_allocation(name: str, observed: str, settings: dict[str, str]) -> None:
    subnet = ipaddress.IPv4Network(settings["PE_PLATFORM_SUBNET"])
    expected = (str(subnet), str(ipaddress.IPv4Network(settings["PE_PLATFORM_IP_RANGE"])), str(platform_gateway(subnet)))
    try:
        configs = [item for item in (json.loads(observed) or []) if item.get("Subnet")
                   and ipaddress.ip_network(item["Subnet"]).version == 4]
        actual = ("none", "none", "none")
        if configs:
            # A gateway elsewhere in the subnet could sit on the fixed Edge address.
            actual = (str(ipaddress.IPv4Network(configs[0]["Subnet"])),
                      str(ipaddress.IPv4Network(configs[0]["IPRange"])) if configs[0].get("IPRange") else "none",
                      str(ipaddress.IPv4Address(configs[0]["Gateway"])) if configs[0].get("Gateway") else "none")
    except (ValueError, TypeError, KeyError):
        actual = ("unreadable", "unreadable", "unreadable")
    if actual != expected:
        raise Refused("platform_network_mismatch",
                      f"network {name} has subnet {actual[0]}, ip-range {actual[1]} and gateway {actual[2]}; expected subnet "
                      f"{expected[0]}, ip-range {expected[1]} and gateway {expected[2]}. Stop every stack on the network, "
                      f"run `docker network rm {name}`, then rerun each bootstrap (Edge first).")


def ensure_network(runner: Runner, settings: dict[str, str]) -> None:
    name = settings["PE_PLATFORM_NETWORK"]
    probe_command = ["docker", "network", "inspect", "--format", "{{json .IPAM.Config}}", name]
    probe = runner(probe_command)
    if probe.returncode != 0:
        subnet = ipaddress.IPv4Network(settings["PE_PLATFORM_SUBNET"])
        created = runner(["docker", "network", "create", "--driver", "bridge", "--subnet", str(subnet),
                          "--ip-range", settings["PE_PLATFORM_IP_RANGE"], "--gateway", str(platform_gateway(subnet)), name])
        # A sibling bootstrap may have created the network first; its allocation is validated below.
        probe = runner(probe_command)
        if probe.returncode != 0:
            raise Refused("network_create_failed", created.stderr.strip())
    check_network_allocation(name, probe.stdout, settings)


def drop_retired_overlay(handle) -> None:
    """Rewrite a COMPOSE_FILE line that still lists the retired peer-pinning overlay."""
    handle.seek(0)
    lines = handle.read().splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = ENV_LINE.match(line.rstrip("\r\n"))
        if not match or match.group("key") != "COMPOSE_FILE":
            continue
        value = match.group("value").strip()
        quote = value[0] if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"') else ""
        names = value.strip(quote).split(os.pathsep)
        kept = [name for name in names if Path(name).name != RETIRED_OVERLAY]
        if len(kept) == len(names):
            return
        lines[index] = line[:match.start("value")] + quote + os.pathsep.join(kept) + quote + line[match.end("value"):]
        print(f"COMPOSE_FILE: dropped {RETIRED_OVERLAY}; the Edge address is fixed by PE_EDGE_IP", file=sys.stderr)
        handle.seek(0)
        handle.write("".join(lines))
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())
        return


def compose_up(root: Path, env_file: Path, runner: Runner) -> None:
    result = runner(compose_command(root, env_file) + [
        "up", "--detach", "--wait", "--wait-timeout", "300",
    ])
    if result.returncode != 0:
        raise Refused("compose_up_failed", (result.stderr or result.stdout).strip()[-2000:])


def volume_names(settings: dict[str, str]) -> list[str]:
    return [f"{settings['PE_VOLUME_PREFIX']}_{suffix}" for suffix in ("edge-data", "edge-config")]


def ensure_volumes(runner: Runner, settings: dict[str, str], project: str) -> None:
    for name in volume_names(settings):
        result = runner(["docker", "volume", "create", "--label", f"com.docker.compose.project={project}", name])
        if result.returncode:
            raise Refused("volume_create_failed", result.stderr.strip())


def compose_command(root: Path, env_file: Path) -> list[str]:
    command = ["docker", "compose", "--project-directory", str(root), "--env-file", str(env_file)]
    values = read_env(env_file) if env_file.exists() else {}
    def setting(key: str) -> str:
        return os.environ.get(key, values.get(key, ""))
    mode = setting("PE_ACCESS_MODE") or "local"
    files = os.environ.get("COMPOSE_FILE", values.get("COMPOSE_FILE", "compose.yaml")).split(os.pathsep)
    files = [str((root / name).resolve()) for name in files if name]
    overlays = [f"compose.{mode}.yaml"] if mode in {"proxy", "public"} else []
    issuer = tls_issuer(mode, setting("PE_TLS_ISSUER"))
    if issuer == "files":
        overlays.append("compose.files.yaml")
    if issuer == "acme":
        overlays += [name for name, key in (("compose.acme-ca-root.yaml", "PE_ACME_CA_ROOT"),
                                            ("compose.acme-eab.yaml", "PE_ACME_EAB_KEY_ID")) if setting(key)]
    # A recorded TLS overlay from an earlier issuer would demand its unused input.
    files = [name for name in files if name not in {str(root / overlay) for overlay in TLS_OVERLAYS}]
    for overlay in overlays:
        override = str(root / overlay)
        files = [name for name in files if name != override] + [override]
    for name in files:
        command += ["-f", name]
    return command


def access_urls(settings: dict[str, str]) -> list[str]:
    mode = settings["PE_ACCESS_MODE"]
    schemes = ("http", "https") if mode == "local" else (("http",) if mode == "proxy" else (settings["PE_SCHEME"],))
    urls = []
    for scheme in schemes:
        port = settings["PE_HTTPS_PORT" if scheme == "https" else "PE_HTTP_PORT"]
        suffix = "" if port == ("443" if scheme == "https" else "80") else ":" + port
        urls.append(f"{scheme}://{settings['PE_PUBLIC_DOMAIN']}{suffix}/")
    return urls


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


def probe_trust(settings: dict[str, str], root: Path, runner: Runner, dc: list[str]) -> str:
    """PEM data the readiness probe trusts; empty means the system store."""
    if settings["PE_TLS_ISSUER"] == "internal":
        result = runner(dc + ["exec", "-T", "caddy", "cat", "/data/caddy/pki/authorities/local/root.crt"])
        if result.returncode:
            raise OSError("internal CA root is not available in edge-data")
        return result.stdout
    if settings.get("PE_TLS_CA"):
        return (root / settings["PE_TLS_CA"]).read_text(encoding="utf-8")
    if settings["PE_TLS_ISSUER"] == "acme" and settings.get("PE_ACME_CA_ROOT"):
        return (root / settings["PE_ACME_CA_ROOT"]).read_text(encoding="utf-8")
    return ""


def wait_ready(settings: dict[str, str], root: Path, env_file: Path,
               runner: Runner = run, timeout: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout
    last = ""
    dc = compose_command(root, env_file)
    while time.monotonic() < deadline:
        try:
            ca = probe_trust(settings, root, runner, dc)
            mode = settings.get("PE_ACCESS_MODE", "local")
            if mode == "local":
                probe(dict(settings, PE_SCHEME="http"), settings["PE_PUBLIC_DOMAIN"])
                certificate = probe(dict(settings, PE_SCHEME="https"), settings["PE_PUBLIC_DOMAIN"], ca)
            else:
                listener = dict(settings, PE_SCHEME="http") if mode == "proxy" else settings
                certificate = probe(listener, settings["PE_PUBLIC_DOMAIN"], ca)
            if settings["PE_TLS_ISSUER"] == "internal":
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
    if "CERTIFICATE_VERIFY_FAILED" in last and settings["PE_TLS_ISSUER"] != "internal" and not settings.get("PE_TLS_CA"):
        last += "; set PE_TLS_CA to the issuing CA's PEM file when it is not in the system trust store"
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
    from installation import add_arguments, install
    add_arguments(parser)
    args = parser.parse_args(argv)
    if args.render_only and args.probe_only:
        parser.error("--render-only and --probe-only are mutually exclusive")
    root = Path(__file__).resolve().parent.parent
    env_file = (root / args.env_file).resolve()
    template = (root / args.template).resolve()
    selected_options = any((args.gateway_dir, args.backplane_dir, args.observability_dir,
                            args.gateway_backup_dir, args.gateway_email, args.backplane_backup_dir,
                            args.capability_file, args.backplane_mode, args.tailscale, args.status_timers))
    if selected_options and not args.stack:
        parser.error("selected installation options require --stack")
    if (args.stack or args.dry_run) and (args.render_only or args.probe_only):
        parser.error("--stack/--dry-run cannot be combined with --render-only/--probe-only")
    for stack in ("gateway", "backplane", "observability"):
        names = {"gateway": ("gateway_dir", "gateway_backup_dir", "gateway_email"),
                 "backplane": ("backplane_dir", "backplane_backup_dir", "capability_file", "backplane_mode"),
                 "observability": ("observability_dir",)}[stack]
        if any(getattr(args, name) for name in names) and stack not in args.stack:
            parser.error("options for " + stack + " require --stack " + stack)
    if args.stack or args.dry_run:
        if (root / args.env_file).is_symlink():
            raise Refused("installation_env_custody", "Selected installation env files must not be symlinks.")
        code, report = install(root, env_file, template, args, runner, Refused)
        print(json.dumps(report))
        return code
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
        drop_retired_overlay(handle)
        exported = os.environ.get("COMPOSE_FILE", "")
        if any(Path(name).name == RETIRED_OVERLAY for name in exported.split(os.pathsep)):
            raise Refused("legacy_setting", f"the exported COMPOSE_FILE lists the retired {RETIRED_OVERLAY}; unset it or drop that entry")
        values = read_env(env_file)
        settings = settings_for(values)
        project = os.environ.get("COMPOSE_PROJECT_NAME") or values.get("COMPOSE_PROJECT_NAME") or PROJECT
        if args.render_only:
            print(json.dumps({"env": str(env_file), "project": project, "generated": []}))
            return 0
        check_tls_inputs(runner, settings, root)
        if not args.probe_only:
            check_ports(runner, settings, project)
            started = now()
            record_bootstrap(root, env_file, started, "unknown")
            try:
                ensure_network(runner, settings)
                check_proxy_peer(runner, settings)
                ensure_volumes(runner, settings, project)
                # The read-only state check must not contend for the running Edge's fixed address.
                # Caddy runs as uid 0 without CAP_DAC_OVERRIDE, so mounted certificate files are read
                # here under the same identity; busybox `test -r` would wrongly pass for root.
                mounted = mounted_tls_files(settings)
                unreadable = f"elif ! cat {' '.join(mounted)} >/dev/null 2>&1; then echo unreadable; " if mounted else ""
                with tempfile.NamedTemporaryFile("w", suffix=".yaml") as isolated:
                    isolated.write("services:\n  caddy:\n    networks: !reset []\n    network_mode: none\n")
                    isolated.flush()
                    result = runner(compose_command(root, env_file) + ["-f", isolated.name,
                        "run", "--rm", "--no-deps", "--entrypoint", "sh", "caddy", "-ec",
                        f"if test -e /data/{RESTORE_MARKER} || test -e /config/{RESTORE_MARKER}; then echo marker; "
                        f"{unreadable}else ls /data /config >/dev/null && echo clean; fi",
                    ])
                state = result.stdout.strip().splitlines()[-1:]
                if result.returncode or state not in (["clean"], ["marker"], ["unreadable"]):
                    raise Refused("state_check_failed", (result.stderr or result.stdout).strip()[-2000:])
                if state == ["marker"]:
                    raise Refused("restore_incomplete", "restore markers present; preserve volumes and restore into a fresh prefix")
                if state == ["unreadable"]:
                    raise Refused("tls_files_unreadable", "Caddy (uid 0 without CAP_DAC_OVERRIDE) cannot read "
                                  + ", ".join(mounted) + "; own tls.key by root with mode 0600, and keep certificates readable")
                compose_up(root, env_file, runner)
                certificate = wait_ready(settings, root, env_file, runner)
            except BaseException:
                record_bootstrap(root, env_file, started, "unavailable")
                raise
            record_bootstrap(root, env_file, started, "healthy")
        else:
            certificate = wait_ready(settings, root, env_file, runner)
        try:
            observed = runner([sys.executable, str(root / "scripts/status_observer.py"),
                               "--checkout", str(root), "--env-file", str(env_file)], timeout=120)
            if observed.returncode:
                raise subprocess.SubprocessError()
        except (OSError, subprocess.SubprocessError, Refused):
            print("Status observation failed; inspect publication permissions and retry the observer.", file=sys.stderr)
        print(json.dumps({
            "project": project,
            "access_mode": settings["PE_ACCESS_MODE"],
            "scheme": settings["PE_SCHEME"],
            "certificate": certificate,
            "listener_urls": access_urls(settings),
            "trust": "Install the public CA root for direct HTTPS" if settings["PE_TLS_ISSUER"] == "internal" else None,
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
