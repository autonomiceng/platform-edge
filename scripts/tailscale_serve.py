#!/usr/bin/env python3
"""Connect installed platform applications through private Tailscale HTTPS links."""
from __future__ import annotations

import argparse
import fcntl
import ipaddress
import os
import re
import tempfile
import json
import subprocess
import sys
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

import bootstrap


def checked(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise ValueError(f"Command failed: {' '.join(argv)}. Inspect that service locally. For Tailscale permission errors, rerun with sudo.")
    return result.stdout


def plan(status: dict, serve: dict, port: int, local_port: int, replace: bool = False) -> dict:
    if not 1 <= port <= 65535:
        raise ValueError("HTTPS port must be between 1 and 65535")
    name = (status.get("Self") or {}).get("DNSName", "").rstrip(".")
    if status.get("BackendState") != "Running" or not name.endswith(".ts.net"):
        raise ValueError("Tailscale must be running with a machine DNS name")
    target = f"http://127.0.0.1:{local_port}"
    tcp = serve.get("TCP", {}).get(str(port))
    web = serve.get("Web", {}).get(f"{name}:{port}", {}).get("Handlers", {})
    existing = web.get("/", {}).get("Proxy")
    if any(path != "/" for path in web):
        raise ValueError("selected port has custom path handlers; choose another port")
    funnel = serve.get("AllowFunnel", {}).get(f"{name}:{port}", False)
    if funnel:
        raise ValueError("selected port is used by Funnel; choose another HTTPS port")
    if tcp and not tcp.get("HTTPS"):
        raise ValueError("selected port has a non-HTTPS listener; choose another port")
    if ((existing and existing not in {target, f"http://localhost:{local_port}"}) or ("/" in web and not existing)) and not replace:
        raise ValueError("selected root endpoint already exists; use another port or --replace")
    return {
        "url": f"https://{name}" + (f":{port}" if port != 443 else "") + "/",
        "command": ["tailscale", "serve", "--bg", f"--https={port}", "--yes", target],
        "undo": ["tailscale", "serve", f"--https={port}", "--set-path=/", "off"],
    }


APPS = {"litellm": 8443, "langfuse": 8444, "s3": 8445, "gateway": 8446,
        "grafana": 8447, "backplane": 8448}


def links(endpoints: dict) -> dict[str, str]:
    return {name: endpoint["url"] + {"litellm": "ui/", "backplane": "dashboard"}.get(name, "")
            for name, endpoint in endpoints.items()}


def verify_application(name: str, endpoint: dict) -> None:
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
        if name != "s3" or error.code != 403 or (actual.scheme, actual.netloc) != ("https", expected.netloc):
            raise ValueError(name + " returned HTTP " + str(error.code)) from error


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
    files = [f for f in files if f not in {"compose.proxy.yaml", "compose.tailscale.yaml"}]
    if services != ["server"] and "PE_TAILSCALE_HOST" not in changes:
        files.append("compose.proxy.yaml")
    if "PE_TAILSCALE_HOST" in changes:
        files.append("compose.tailscale.yaml")
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
    parser.add_argument("--port-base", type=int, default=8443, help="first of six consecutive application HTTPS ports")
    parser.add_argument("--gateway-dir", type=Path, default=root.parent / "llm-gateway-stack")
    parser.add_argument("--observability-dir", type=Path, default=root.parent / "observability-stack")
    parser.add_argument("--backplane-dir", type=Path, default=root.parent / "agent-backplane")
    parser.add_argument("--replace", action="store_true", help="replace conflicting HTTPS root handlers on the selected ports")
    parser.add_argument("--dry-run", action="store_true", help="print proposed URLs and public settings without changing anything")
    args = parser.parse_args(argv)
    temporary = []
    locks = []
    try:
        if any(key.startswith(("PE_", "LG_", "OB_", "BP_", "COMPOSE_")) for key in os.environ):
            raise ValueError("Unset exported stack and Compose settings before setup; configure installations through their .env files.")
        settings = bootstrap.settings_for(bootstrap.read_env(args.env_file))
        if settings["PE_ACCESS_MODE"] == "public":
            raise ValueError("This helper adds Tailscale to local access. Keep public installations separate.")
        status = json.loads(checked(["tailscale", "status", "--json"]))
        serve = json.loads(checked(["tailscale", "serve", "status", "--json"])) or {}
        landing = plan(status, serve, args.https_port, int(settings["PE_HTTP_PORT"]), args.replace)
        host = status["Self"]["DNSName"].rstrip(".")
        ports = {name: args.port_base + offset for offset, name in enumerate(APPS)}
        edge = dict(PE_ACCESS_MODE="local", PE_SCHEME="http", PE_BIND_HOST="127.0.0.1",
                    PE_TAILSCALE_HOST=host, PE_TAILSCALE_PORT=str(args.https_port))
        edge.update({"PE_TAILSCALE_" + name.upper() + "_PORT": str(port) for name, port in ports.items()})
        bootstrap.settings_for(dict(settings, **edge))
        # Pin the existing Edge peer so recreation cannot silently invalidate sibling trust.
        containers = json.loads(checked(["docker", "network", "inspect", settings["PE_PLATFORM_NETWORK"]]))[0].get("Containers", {})
        project = values(args.env_file).get("COMPOSE_PROJECT_NAME", "platform-edge")
        ids = checked(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=" + project,
                       "--filter", "label=com.docker.compose.service=caddy"]).split()
        peers = [containers[c]["IPv4Address"].split("/")[0] for c in containers if any(c.startswith(i) for i in ids)]
        if len(peers) != 1:
            raise ValueError("Start Platform Edge first; exactly one running Edge must be on its platform network.")
        peer = str(ipaddress.IPv4Address(peers[0]))
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
            changes = {prefix + "_ACCESS_MODE": "proxy", prefix + "_PLATFORM_NETWORK": settings["PE_PLATFORM_NETWORK"]}
            if prefix != "BP":
                changes.update({prefix + "_SCHEME": "https", prefix + "_TRUSTED_PROXIES": peer,
                                prefix + "_BIND_HOST": "127.0.0.1", prefix + "_PUBLIC_PORT_SUFFIX": "", prefix + "_PUBLIC_DOMAIN": settings["PE_PUBLIC_DOMAIN"]})
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
                key = {"gateway": "LG_CONSOLE_URL", "grafana": "OB_GRAFANA_URL", "backplane": "BP_PUBLIC_URL"}.get(name, "LG_" + name.upper() + "_URL")
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
                check_storage(rendered, service, json.loads(checked(["docker", "inspect", ids[0]]))[0])
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
        for endpoint in endpoints.values():
            checked(endpoint["command"])
        with urllib.request.urlopen(landing["url"] + "health", timeout=15) as response:
            if response.status != 200:
                raise ValueError("Tailscale Edge health check failed")
        for name in endpoints:
            if name == "Platform Edge":
                continue
            probe_name = "observability" if name == "grafana" else name
            with urllib.request.urlopen(landing["url"] + "health/" + probe_name, timeout=15) as response:
                if response.status != 200:
                    raise ValueError(name + " is not reachable through Edge")
            verify_application(name, endpoints[name])
        print(json.dumps({"applied": True, "links": links(endpoints),
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
