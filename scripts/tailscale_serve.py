#!/usr/bin/env python3
"""Connect installed platform applications through private Tailscale HTTPS links."""
from __future__ import annotations

import argparse
import fcntl
import ipaddress
import os
import re
import shlex
import tempfile
import json
import subprocess
import sys
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

import bootstrap


class ServeFailure(ValueError):
    """Retain only an authored diagnostic and exit status, never child output."""
    def __init__(self, result):
        diagnostic = ((result.stderr or "")[:4096] + (result.stdout or "")[:4096]).lower()
        self.permission = any(text in diagnostic for text in ("permission denied", "access denied"))
        self.returncode = result.returncode
        detail = "Serve command failed. Inspect Tailscale status and HTTPS configuration locally."
        if self.permission:
            detail = "Serve configuration permission was denied."
        elif "https" in diagnostic and any(text in diagnostic for text in ("not enabled", "disabled", "enable https")):
            detail = "Enable Tailscale HTTPS certificates for this tailnet, then retry."
        elif any(text in diagnostic for text in ("failed to connect to local tailscaled", "tailscaled is not running", "connection refused")):
            detail = "Restore the local Tailscale daemon connection, then retry."
        super().__init__(detail)


def serve_failure(command, error, completed, remaining):
    permission = isinstance(error, ServeFailure) and error.permission
    detail = str(error) if isinstance(error, ServeFailure) else "Serve command could not complete; inspect Tailscale locally before retrying."
    if isinstance(error, subprocess.TimeoutExpired):
        detail = "Serve command timed out; verify actual Serve state before retrying."
    report = {"state": "administrator_action" if permission else "command_failed",
              "command": shlex.join((["sudo"] if permission else []) + command),
              "detail": detail, "serve_changes_completed": completed, "remaining": remaining,
              "next": ("Run only this Serve command as administrator, then rerun the same selection to verify HTTPS."
                       if permission else "Correct the reported command failure locally, then rerun the same selection to verify actual Serve state and HTTPS.")}
    if isinstance(error, ServeFailure):
        report["exit_code"] = error.returncode
    return report


def checked(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    if result.returncode:
        if argv[:2] == ["tailscale", "serve"] and "status" not in argv:
            raise ServeFailure(result)
        raise ValueError(f"Command failed: {' '.join(argv)}. Inspect that service locally.")
    return result.stdout


def serve_status(output):
    # Preserve the helper's empty/null configuration support without accepting arrays or scalars.
    configuration = json.loads(output)
    if configuration is None:
        return {}
    if not isinstance(configuration, dict):
        raise ValueError("Malformed Serve configuration")
    return configuration


def plan(status: dict, serve: dict, port: int, local_port: int, replace: bool = False) -> dict:
    if not isinstance(status, dict) or not isinstance(serve, dict):
        raise ValueError("Malformed Tailscale status")
    if not isinstance(status.get("Self"), dict):
        raise ValueError("Tailscale machine identity is unavailable")
    for section in ("TCP", "Web", "AllowFunnel", "Foreground"):
        if not isinstance(serve.get(section, {}), dict):
            raise ValueError("Malformed Serve configuration")
    for foreground in serve.get("Foreground", {}).values():
        if (not isinstance(foreground, dict) or not isinstance(foreground.get("TCP", {}), dict)
                or str(port) in foreground.get("TCP", {})):
            raise ValueError("Selected port has a foreground listener")
    if not 1 <= port <= 65535:
        raise ValueError("HTTPS port must be between 1 and 65535")
    name = status["Self"].get("DNSName", "")
    if not isinstance(name, str):
        raise ValueError("Malformed Tailscale machine identity")
    name = name.rstrip(".")
    if status.get("BackendState") != "Running" or not re.fullmatch(r"[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.ts\.net", name) or len(name) > 253:
        raise ValueError("Tailscale must be running with a machine DNS name")
    addresses = status["Self"].get("TailscaleIPs", [])
    if not isinstance(addresses, list) or any(not isinstance(address, str) for address in addresses):
        raise ValueError("Malformed Tailscale addresses")
    try:
        for address in addresses:
            ipaddress.ip_address(address)
    except ValueError:
        raise ValueError("Malformed Tailscale addresses") from None
    target = f"http://127.0.0.1:{local_port}"
    tcp = serve.get("TCP", {}).get(str(port))
    site = serve.get("Web", {}).get(f"{name}:{port}", {})
    if not isinstance(site, dict) or set(site) - {"Handlers"} or (tcp is not None and not isinstance(tcp, dict)):
        raise ValueError("Selected listener has custom or malformed configuration")
    web = site.get("Handlers", {})
    if not isinstance(web, dict) or not isinstance(web.get("/", {}), dict):
        raise ValueError("Malformed selected handlers")
    existing = web.get("/", {}).get("Proxy")
    if existing is not None and not isinstance(existing, str):
        raise ValueError("Malformed proxy target")
    if any(authority != f"{name}:{port}" and authority.endswith(f":{port}") for authority in serve.get("Web", {})):
        raise ValueError("selected port has a foreign hostname handler")
    if tcp and set(tcp) != {"HTTPS"}:
        raise ValueError("selected port has a foreign listener")
    if "/" in web and set(web["/"]) != {"Proxy"} and not replace:
        raise ValueError("selected root has custom handlers")
    if any(path != "/" for path in web):
        raise ValueError("selected port has custom path handlers; choose another port")
    funnel = any(enabled for authority, enabled in serve.get("AllowFunnel", {}).items() if authority.endswith(f":{port}"))
    if funnel:
        raise ValueError("selected port is used by Funnel; choose another HTTPS port")
    if tcp and tcp.get("HTTPS") is not True:
        raise ValueError("selected port has a non-HTTPS listener; choose another port")
    if ((existing and existing not in {target, f"http://localhost:{local_port}"}) or ("/" in web and not existing)) and not replace:
        raise ValueError("selected root endpoint already exists; use another port or --replace")
    matching = tcp == {"HTTPS": True} and web == {"/": {"Proxy": existing}} and existing in {target, f"http://localhost:{local_port}"}
    if (tcp is not None or site) and not matching and not replace:
        raise ValueError("Selected port has an incomplete or foreign Serve configuration")
    return {
        "matching": matching,
        "url": f"https://{name}" + (f":{port}" if port != 443 else "") + "/",
        "command": ["tailscale", "serve", "--bg", f"--https={port}", "--yes", target],
        "undo": ["tailscale", "serve", f"--https={port}", "--set-path=/", "off"],
    }


APPS = {"litellm": 8443, "langfuse": 8444, "s3": 8445, "gateway": 8446,
        "grafana": 8447, "backplane": 8448, "rustfs": 8449,
        "backplane_rustfs": 8450, "observability_rustfs": 8451}


STACK_APPS = {"gateway": ("LG", ["litellm", "langfuse", "s3", "gateway"]),
              "backplane": ("BP", ["backplane"]), "observability": ("OB", ["grafana"])}


def selected_plan(settings, selected, status, serve):
    """Derive public settings from the qualified selection, without opening siblings."""
    endpoints = {"Platform Edge": plan(status, serve, int(settings["PE_TAILSCALE_PORT"]), int(settings["PE_HTTP_PORT"]))}
    host = status["Self"]["DNSName"].rstrip(".")
    if settings["PE_ACCESS_MODE"] == "public" or settings["PE_BIND_HOST"] != "127.0.0.1":
        raise ValueError("Tailscale requires local/proxy access on loopback")
    if settings["PE_TAILSCALE_HOST"] and settings["PE_TAILSCALE_HOST"] != host:
        raise ValueError("The recorded Tailscale machine name differs; preserve omitted routes.")
    changes = {"edge": {"PE_TAILSCALE_HOST": host, "PE_ACCESS_MODE": "local", "PE_SCHEME": "http"}}
    retained = [name for name in settings["PE_TAILSCALE_APPS"].split(",") if name]
    for stack, current in selected.items():
        if stack == "edge":
            continue
        prefix, names = STACK_APPS[stack]
        names = list(names)
        console = "rustfs" if stack == "gateway" else stack + "_rustfs"
        retained = [name for name in retained if name not in {*names, console}]
        enabled = current.get(prefix + "_RUSTFS_CONSOLE", "on" if stack == "gateway" else "false")
        if enabled == ("on" if stack == "gateway" else "true"):
            names.append(console)
        update = {prefix + "_ACCESS_MODE": "proxy"}
        if stack != "backplane":
            update.update({prefix + "_SCHEME": "https", prefix + "_PUBLIC_PORT_SUFFIX": ""})
        for name in names:
            endpoint = plan(status, serve, int(settings["PE_TAILSCALE_" + name.upper() + "_PORT"]), int(settings["PE_HTTP_PORT"]))
            endpoints[name] = endpoint
            label = {"gateway": "CONSOLE", "backplane": "PUBLIC", "backplane_rustfs": "RUSTFS",
                     "observability_rustfs": "RUSTFS"}.get(name, name.upper())
            update[prefix + "_" + label + "_URL"] = endpoint["url"].rstrip("/")
            if stack == "observability" or name == "backplane_rustfs":
                update[prefix + "_" + label + "_URL_HOST"] = host
                update[prefix + "_" + label + "_AUTHORITY"] = urllib.parse.urlsplit(endpoint["url"]).netloc
        changes[stack] = update
        retained.extend(names)
    changes["edge"]["PE_TAILSCALE_APPS"] = ",".join(dict.fromkeys(retained))
    return {"endpoints": endpoints, "changes": changes}


def bridge_gateway(network):
    gateways = [str(ipaddress.IPv4Address(entry["Gateway"])) for entry in network.get("IPAM", {}).get("Config", [])
                if entry.get("Gateway") and ipaddress.ip_address(entry["Gateway"]).version == 4]
    if len(gateways) != 1:
        raise ValueError("Tailscale requires one exact IPv4 host bridge gateway")
    return gateways[0]


def check_listeners(endpoints, status, listeners, edge_ports=()):
    addresses = set((status.get("Self") or {}).get("TailscaleIPs", []))
    for endpoint in endpoints.values():
        port = urllib.parse.urlsplit(endpoint["url"]).port or 443
        for address, occupied in listeners:
            # An exact Tailnet address with the matching Serve entry is owned.
            # Edge's loopback HTTP/HTTPS sockets can share its numeric port.
            if occupied == port and (address, occupied) not in edge_ports:
                if address not in addresses or not endpoint["matching"]:
                    raise ValueError("Selected Tailscale port has a foreign host listener")


def connect(endpoints, inspect):
    """Reinspect all endpoints before the first Serve write; matching entries are read-only."""
    status = json.loads(inspect(["tailscale", "status", "--json"]))
    serve = serve_status(inspect(["tailscale", "serve", "status", "--json"]))
    pending = []
    for endpoint in endpoints.values():
        port = urllib.parse.urlsplit(endpoint["url"]).port or 443
        local_port = urllib.parse.urlsplit(endpoint["command"][-1]).port
        actual = plan(status, serve, port, local_port)
        if actual["url"] != endpoint["url"]:
            raise ValueError("Tailscale machine changed; rerun preflight")
        if not actual["matching"]:
            pending.append(actual["command"])
    for index, command in enumerate(pending):
        try:
            inspect(command)
        except (OSError, ValueError, subprocess.TimeoutExpired, bootstrap.Refused) as error:
            return serve_failure(command, error, index, len(pending) - index)
    access = {}
    for name, endpoint in endpoints.items():
        if name == "Platform Edge":
            verify_application(name, {"url": endpoint["url"] + "health"})
        else:
            access[name] = verify_application(name, endpoint, allowed_denials=(401, 403, 404) if name.endswith("_rustfs") else ())
            protected = {"backplane": "api/v1/workspaces", "grafana": "api/user", "litellm": "v1/models",
                         "langfuse": "api/public/projects", "s3": "", "rustfs": "rustfs/admin/v3/info",
                         "backplane_rustfs": "rustfs/admin/v3/info", "observability_rustfs": "rustfs/admin/v3/info"}
            if name in protected and access[name] != "access_denied":
                denial = verify_application("protected", {"url": endpoint["url"] + protected[name]}, allowed_denials=(401, 403))
                if denial != "access_denied":
                    raise ValueError(name + " anonymous API access was not refused")
    return {"state": "verified", "links": links(endpoints), "access": access,
            "authentication": "Native login remains required; authenticated acceptance is a separate operator check."}


def links(endpoints: dict) -> dict[str, str]:
    return {name: endpoint["url"] + {"litellm": "ui/", "backplane": "dashboard", "rustfs": "rustfs/console/",
                                    "backplane_rustfs": "rustfs/console/", "observability_rustfs": "rustfs/console/"}.get(name, "")
            for name, endpoint in endpoints.items()}


def verify_application(name: str, endpoint: dict, *, allowed_denials: tuple[int, ...] = ()) -> str:
    url = links({name: endpoint})[name]
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            actual = urllib.parse.urlsplit(response.geturl())
            expected = urllib.parse.urlsplit(url)
            if response.status != 200 or actual.scheme != "https" or actual.netloc != expected.netloc:
                raise ValueError(name + " did not serve its configured Tailscale URL")
    except urllib.error.HTTPError as error:
        # An anonymous S3 client must authenticate. This is an API, not a login page.
        actual = urllib.parse.urlsplit(error.geturl())
        expected = urllib.parse.urlsplit(url)
        accepted = error.code in allowed_denials or (name == "s3" and error.code == 403)
        if not accepted or (actual.scheme, actual.netloc) != ("https", expected.netloc):
            raise ValueError(name + " returned HTTP " + str(error.code)) from error
        return "access_denied"
    return "reachable"


def values(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text().splitlines():
        match = re.match(r"^(?:export )?([A-Z][A-Z0-9_]*)=(.*)$", line)
        if match:
            result[match[1]] = match[2].strip().strip("\"'")
    return result


def amended(source: str, changes: dict[str, str]) -> str:
    pending = dict(changes)
    lines = []
    for line in source.splitlines():
        match = re.match(r"^(?:export )?([A-Z][A-Z0-9_]*)=", line)
        key = match[1] if match else None
        if key in changes:
            if key not in pending:
                continue
            value = pending.pop(key)
            line = ("export " if line.startswith("export ") else "") + key + "=" + ("\"" + value + "\"" if " " in value else value)
        lines.append(line)
    lines.extend(key + "=" + ("\"" + value + "\"" if " " in value else value)
                 for key, value in pending.items())
    return "\n".join(lines) + "\n"


def compose(root: Path, env: Path, files: str) -> list[str]:
    command = ["docker", "compose", "--project-directory", str(root), "--env-file", str(env)]
    for filename in files.split(":"):
        command += ["-f", str(root / filename)]
    return command


def configuration(root: Path, env: Path, changes: dict[str, str], services: list[str]) -> dict:
    current = values(env)
    files = current.get("COMPOSE_FILE", "compose.yaml").split(":")
    # Preserve operator overlays; replace only our own listener selection.
    managed = {(root / name).resolve() for name in ("compose.proxy.yaml", "compose.tailscale.yaml")}
    files = [f for f in files if (root / f).resolve() not in managed]
    if "BP_ACCESS_MODE" not in changes and "PE_TAILSCALE_HOST" not in changes:
        files.append(str(root / "compose.proxy.yaml") if files and Path(files[0]).is_absolute() else "compose.proxy.yaml")
    if "PE_TAILSCALE_HOST" in changes:
        files.append(str(root / "compose.tailscale.yaml") if files and Path(files[0]).is_absolute() else "compose.tailscale.yaml")
    changes = dict(changes, COMPOSE_FILE=":".join(files))
    return {"root": root, "env": env, "changes": changes, "services": services,
            "source": env.read_text(), "files": changes["COMPOSE_FILE"]}


def check_storage(rendered: dict, service: str, container: dict) -> None:
    desired = rendered["services"][service]
    if container["Config"]["Image"] != desired["image"]:
        raise ValueError(service + ": update the stack independently before connecting Tailscale; image differs")
    mounts = {mount["Destination"]: mount for mount in container["Mounts"]}
    for mount in desired.get("volumes", []):
        existing = mounts.get(mount["target"], {})
        if mount["type"] == "volume":
            matches = existing.get("Name") == rendered["volumes"][mount["source"]]["name"]
        elif mount["type"] == "bind" and (not mount.get("read_only") or "/data/" in mount["source"]):
            matches = existing.get("Source") == mount["source"]
        else:
            continue
        if not matches:
            raise ValueError(service + ": persistent mount would change at " + mount["target"] + "; preserve the original installation settings")


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=root / ".env")
    parser.add_argument("--https-port", type=int, default=443)
    parser.add_argument("--port-base", type=int, default=8443, help="first of nine consecutive application HTTPS ports")
    parser.add_argument("--gateway-dir", type=Path, default=root.parent / "llm-gateway-stack")
    parser.add_argument("--observability-dir", type=Path, default=root.parent / "observability-stack")
    parser.add_argument("--backplane-dir", type=Path, default=root.parent / "agent-backplane")
    parser.add_argument("--console-allow", help="explicit client IP/CIDR list for enabled Backplane and Observability consoles; omission preserves their allowlists")
    parser.add_argument("--replace", action="store_true", help="replace conflicting HTTPS root handlers on the selected ports")
    parser.add_argument("--dry-run", action="store_true", help="print proposed URLs and public settings without changing anything")
    args = parser.parse_args(argv)
    temporary = []
    locks = []
    try:
        if any(key.startswith(("PE_", "LG_", "OB_", "BP_", "COMPOSE_")) for key in os.environ):
            raise ValueError("Unset exported stack and Compose settings before setup; configure installations through their .env files.")
        if args.console_allow is not None:
            if not args.console_allow.strip():
                raise ValueError("--console-allow requires client IPs or CIDRs")
            for address in args.console_allow.split():
                ipaddress.ip_network(address, strict=False)
        settings = bootstrap.settings_for(bootstrap.read_env(args.env_file))
        if settings["PE_ACCESS_MODE"] == "public":
            raise ValueError("This helper adds Tailscale to local access. Keep public installations separate.")
        status = json.loads(checked(["tailscale", "status", "--json"]))
        serve = serve_status(checked(["tailscale", "serve", "status", "--json"]))
        landing = plan(status, serve, args.https_port, int(settings["PE_HTTP_PORT"]), args.replace)
        host = status["Self"]["DNSName"].rstrip(".")
        ports = {name: args.port_base + offset for offset, name in enumerate(APPS)}
        edge = dict(PE_ACCESS_MODE="local", PE_SCHEME="http", PE_BIND_HOST="127.0.0.1",
                    PE_TAILSCALE_HOST=host, PE_TAILSCALE_PORT=str(args.https_port))
        edge.update({"PE_TAILSCALE_" + name.upper() + "_PORT": str(port) for name, port in ports.items()})
        bootstrap.settings_for(dict(settings, **edge))
        # Pin the existing Edge peer so recreation cannot silently invalidate sibling trust.
        network = json.loads(checked(["docker", "network", "inspect", settings["PE_PLATFORM_NETWORK"]]))[0]
        containers = network.get("Containers", {})
        gateways = [str(ipaddress.ip_address(entry["Gateway"])) for entry in network.get("IPAM", {}).get("Config", [])
                    if entry.get("Gateway") and ipaddress.ip_address(entry["Gateway"]).version == 4]
        if len(gateways) != 1:
            raise ValueError("Tailscale ingress requires one exact IPv4 host bridge gateway")
        edge["PE_TRUSTED_PROXIES"] = gateways[0]
        project = values(args.env_file).get("COMPOSE_PROJECT_NAME", "platform-edge")
        ids = checked(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=" + project,
                       "--filter", "label=com.docker.compose.service=caddy"]).split()
        peers = [containers[c]["IPv4Address"].split("/")[0] for c in containers if any(c.startswith(i) for i in ids)]
        if len(peers) != 1:
            raise ValueError("Start Platform Edge first; exactly one running Edge must be on its platform network.")
        peer = str(ipaddress.IPv4Address(peers[0]))
        if args.console_allow is not None:
            platform_ranges = [ipaddress.ip_network(entry["Subnet"]) for entry in network.get("IPAM", {}).get("Config", []) if entry.get("Subnet")]
            for value in args.console_allow.split():
                allowed = ipaddress.ip_network(value, strict=False)
                if allowed.prefixlen == 0 or any(allowed.version == subnet.version and allowed.overlaps(subnet) for subnet in platform_ranges):
                    raise ValueError("console client allowlist must exclude all-address and Platform Network ranges")
                if allowed.version == 4 and (ipaddress.ip_address(peer) in allowed or ipaddress.ip_address(gateways[0]) in allowed):
                    raise ValueError("console client allowlist must exclude ingress proxy addresses")
        edge["PE_TAILSCALE_EDGE_IP"] = peer
        configs = [configuration(root, args.env_file.resolve(), edge, ["caddy"])]
        endpoints = {"Platform Edge": landing}
        skipped = []
        for directory, prefix, names, services in (
            (args.gateway_dir, "LG", ["litellm", "langfuse", "s3", "gateway"], ["caddy", "litellm", "langfuse-web", "langfuse-worker", "rustfs"]),
            (args.observability_dir, "OB", ["grafana"], ["caddy", "grafana"]),
            (args.backplane_dir, "BP", ["backplane"], ["server"]),
        ):
            directory = directory.resolve()
            env = directory / ".env"
            if not env.exists():
                skipped.append(directory.name + " (no .env; not configured)")
                continue
            current = values(env)
            if prefix == "BP":
                selected_files = {(directory / name).resolve() for name in current.get("COMPOSE_FILE", "compose.yaml").split(":")}
                if (directory / "compose.gateway.yaml").resolve() not in selected_files or (directory / "compose.edge.yaml").resolve() in selected_files or "gateway" not in current.get("COMPOSE_PROFILES", "").split(","):
                    raise ValueError("Backplane must select its existing compose.gateway.yaml before connecting Tailscale")
                services = ["server", "edge"]
            console_enabled = prefix in {"BP", "OB"} and current.get(prefix + "_RUSTFS_CONSOLE", "false") == "true"
            if console_enabled:
                if prefix == "BP" and ((directory / "compose.blobs.yaml").resolve() not in selected_files or "blobs" not in current.get("COMPOSE_PROFILES", "").split(",") or current.get("BP_BLOB_BACKEND") != "s3"):
                    raise ValueError("Backplane console requires its existing S3 blobs selection")
                if prefix == "OB" and ("s3" not in current.get("COMPOSE_PROFILES", "").split(",") or (directory / "compose.s3.yaml").resolve() not in {(directory / name).resolve() for name in current.get("COMPOSE_FILE", "compose.yaml").split(":")}):
                    raise ValueError("Observability console requires its existing S3 storage selection")
                names = [*names, "backplane_rustfs" if prefix == "BP" else "observability_rustfs"]
                services = [*services, "rustfs"]
            if prefix == "LG" and current.get("LG_RUSTFS_CONSOLE", "on") == "on":
                names = [*names, "rustfs"]
            changes = {prefix + "_ACCESS_MODE": "proxy", prefix + "_PLATFORM_NETWORK": settings["PE_PLATFORM_NETWORK"]}
            if prefix != "BP":
                changes.update({prefix + "_SCHEME": "https", prefix + "_TRUSTED_PROXIES": peer,
                                prefix + "_BIND_HOST": "127.0.0.1", prefix + "_PUBLIC_PORT_SUFFIX": "", prefix + "_PUBLIC_DOMAIN": settings["PE_PUBLIC_DOMAIN"]})
            if console_enabled:
                console_name = "backplane_rustfs" if prefix == "BP" else "observability_rustfs"
                changes.update({prefix + "_RUSTFS_URL_HOST": host,
                                prefix + "_RUSTFS_AUTHORITY": host + ":" + str(ports[console_name]),
                                prefix + "_TRUSTED_PROXIES": peer})
                if args.console_allow is not None:
                    changes[prefix + "_RUSTFS_CONSOLE_ALLOW"] = " ".join(args.console_allow.split())
            if prefix == "OB":
                changes["OB_GRAFANA_URL_HOST"] = host
                changes["OB_GRAFANA_AUTHORITY"] = host + ":" + str(ports["grafana"])
            if prefix == "LG":
                changes["LG_GRAFANA_URL"] = f"https://{host}:{ports['grafana']}"
                changes["LG_BACKPLANE_URL"] = f"https://{host}:{ports['backplane']}"
                # These operator pages still require their application login. Only Edge is added.
                allow = current.get("LG_OPERATOR_ALLOW", "127.0.0.0/8 ::1").split()
                changes["LG_OPERATOR_ALLOW"] = " ".join(dict.fromkeys(allow + [peer]))
            for name in names:
                endpoint = plan(status, serve, ports[name], int(settings["PE_HTTP_PORT"]), args.replace)
                endpoints[name] = endpoint
                key = {"gateway": "LG_CONSOLE_URL", "grafana": "OB_GRAFANA_URL", "backplane": "BP_PUBLIC_URL",
                       "backplane_rustfs": "BP_RUSTFS_URL", "observability_rustfs": "OB_RUSTFS_URL"}.get(name, "LG_" + name.upper() + "_URL")
                changes[key] = endpoint["url"].rstrip("/")
            config = configuration(directory, env, changes, services)
            running = checked(compose(directory, env, current.get("COMPOSE_FILE", "compose.yaml")) + ["ps", "--status", "running", "--services"]).split()
            if not running:
                raise ValueError(f"Start {directory.name} independently before connecting it to Tailscale.")
            configs.append(config)
        configs[0]["changes"]["PE_TAILSCALE_APPS"] = ",".join(name for name in endpoints if name != "Platform Edge")
        print(json.dumps({"links": links(endpoints), "skipped": skipped,
                          "changes": [{"env": str(c["env"]), "settings": c["changes"]} for c in configs],
                          "applied": False}), flush=True)
        if args.dry_run:
            return 0
        # Validate all rendered Compose projects before changing any environment or service.
        for config in configs:
            handle = config["env"].open("r+")
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locks.append(handle)
            if handle.read() != config["source"]:
                raise ValueError("Configuration changed during setup; retry.")
            fd, filename = tempfile.mkstemp(prefix="platform-tailscale-", suffix=".env")
            temporary.append(Path(filename))
            with os.fdopen(fd, "w") as temp:
                temp.write(amended(config["source"], config["changes"]))
            rendered = json.loads(checked(compose(config["root"], Path(filename), config["files"]) + ["config", "--format", "json"]))
            for service in config["services"]:
                ids = checked(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=" + rendered["name"],
                               "--filter", "label=com.docker.compose.service=" + service]).split()
                if len(ids) != 1:
                    raise ValueError(service + ": start the stack independently before connecting Tailscale")
                container = json.loads(checked(["docker", "inspect", ids[0]]))[0]
                check_storage(rendered, service, container)
                if service == "rustfs" and any(key in config["changes"] for key in ("BP_RUSTFS_URL", "OB_RUSTFS_URL")):
                    environment = dict(value.split("=", 1) for value in container["Config"].get("Env", []) if "=" in value)
                    if environment.get("RUSTFS_CONSOLE_ENABLE") != "true" or environment.get("RUSTFS_CONSOLE_ADDRESS", ":9001") != ":9001":
                        raise ValueError("deploy the selected native RustFS console independently before connecting Tailscale")
        for config, handle in zip(configs, locks):
            handle.seek(0)
            handle.write(amended(config["source"], config["changes"]))
            handle.truncate()
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        for config in configs:
            checked(compose(config["root"], config["env"], config["files"]) +
                    ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "180"] + config["services"])
        bootstrap.wait_ready(bootstrap.settings_for(bootstrap.read_env(args.env_file)),
                             root, args.env_file, bootstrap.run)
        pending = [endpoint["command"] for endpoint in endpoints.values() if not endpoint["matching"]]
        for index, command in enumerate(pending):
            try:
                checked(command)
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                print(json.dumps({"applied": False, **serve_failure(command, error, index, len(pending) - index)}))
                return 1
        with urllib.request.urlopen(landing["url"] + "health", timeout=15) as response:
            if response.status != 200:
                raise ValueError("Tailscale Edge health check failed")
        console_access = {}
        for name in endpoints:
            if name == "Platform Edge":
                continue
            if name in {"backplane_rustfs", "observability_rustfs"}:
                console_access[name] = verify_application(name, endpoints[name], allowed_denials=(401, 403, 404))
                continue
            probe_name = "observability" if name == "grafana" else name
            with urllib.request.urlopen(landing["url"] + "health/" + probe_name, timeout=15) as response:
                if response.status != 200:
                    raise ValueError(name + " is not reachable through Edge")
            verify_application(name, endpoints[name])
        print(json.dumps({"applied": True, "links": links(endpoints), "consoleAccessFromSetupHost": console_access,
                          "next": "Open Platform Edge to check and launch applications. Each application keeps its own login."}))
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired, bootstrap.Refused) as error:
        print(json.dumps({"error": "tailscale_setup_failed", "detail": str(error),
                          "next": "Setup may be partly applied. Correct the error and rerun; existing data is preserved."}), file=sys.stderr)
        return 1
    finally:
        for handle in locks:
            handle.close()
        for path in temporary:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
