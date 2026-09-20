"""Plan and preflight selected installations before invoking their owning bootstraps."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

import bootstrap
import tailscale_serve
from installation_execution import custody, execute, qualify, read_source

STACKS = {
    "edge": ("PE", "platform-edge", "scripts/bootstrap.py"),
    "gateway": ("LG", "llm-gateway-stack", "scripts/bootstrap.py"),
    "backplane": ("BP", "agent-backplane", "infra/bootstrap/prepare.ts"),
    "observability": ("OB", "observability-stack", "scripts/bootstrap.py"),
}


def add_arguments(parser: bootstrap.ArgumentParser) -> None:
    parser.add_argument("--stack", action="append", choices=STACKS, default=[])
    parser.add_argument("--dry-run", action="store_true", help="inspect prerequisites and print a plan without writes")
    for stack in ("gateway", "backplane", "observability"):
        parser.add_argument(f"--{stack}-dir", type=Path)
    for name in ("backplane-backup-dir", "capability-file", "gateway-backup-dir"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--gateway-email")
    parser.add_argument("--backplane-mode", choices=("full", "minimal"))
    parser.add_argument("--tailscale", action="store_true")
    parser.add_argument("--status-timers", action="store_true")


def read_settings(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = bootstrap.ENV_LINE.fullmatch(line)
        if not match or match["key"] in result:
            raise ValueError("Repair ambiguous env assignments using the owning bootstrap contract.")
        value = match["value"].strip()
        if not value.startswith(("'", '"')) and "#" in value:
            raise ValueError("Quote literal env values containing #; inline comments are unsupported.")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError("Repair unmatched quotes in the selected env file.")
            value = value[1:-1]
        result[match["key"]] = value
    return result


def timer_command(item):
    command = [sys.executable, str(item["root"] / "scripts/install_status_timer.py"),
               "--checkout", str(item["root"]), "--env-file", str(item["env"])]
    if item["name"] == "backplane":
        command += ["--compose-project", item["action"]["project"]]
        for filename in item["files"]:
            command += ["--compose-file", str((item["root"] / filename).resolve())]
        for profile in item["profiles"]:
            command += ["--profile", profile]
    return command


def preflight(root, env_file, template, args, runner, refused=bootstrap.Refused, prepared=None):
    prepared = [] if prepared is None else prepared
    selected = [name for name in STACKS if name == "edge" or name in args.stack]
    result = {"selected": selected, "actions": [], "conflicts": [], "executable": False, "execution_supported": True,
              "infrastructure": "unverified", "enrollment": "unverified",
              "deferred": ["Backplane enrollment remains a separate bp bootstrap action; readiness is not enrollment."]}

    def conflict(stack, code, detail):
        result["conflicts"].append({"stack": stack, "code": code, "detail": detail})

    def inspect(argv, **options):
        try:
            response = runner(argv, **options)
        except refused:
            raise ValueError("Read-only inspection failed; inventory is unknown.") from None
        if response.returncode:
            raise ValueError("Read-only inspection failed; inventory is unknown.")
        return response.stdout.strip()

    prefixes = tuple(STACKS[name][0] + "_" for name in selected) + ("COMPOSE_",)
    if any(key.startswith(prefixes) for key in os.environ):
        conflict("edge", "shell_settings", "Unset selected stack and Compose exports; use the installation env files.")
        return result
    commands = ["docker", "ss"] + (["bun"] if "backplane" in selected else [])
    commands += (["tailscale"] if args.tailscale else []) + (["systemctl"] if args.status_timers else [])
    missing = [name for name in commands if shutil.which(name) is None]
    for name in missing:
        conflict("edge", "command_missing", "Required command: " + name)
    docker = "docker" not in missing
    try:
        if docker:
            endpoint = os.environ.get("DOCKER_HOST", "")
            context = inspect(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"])
            if (endpoint and not endpoint.startswith("unix:///")) or not context.startswith("unix:///"):
                raise ValueError("Use a trusted local Unix-socket Docker context.")
            inspect(["docker", "info", "--format", "{{.ServerVersion}}"])
            version = inspect(["docker", "compose", "version", "--short"])
            parts = re.match(r"v?(\d+)\.(\d+)\.(\d+)", version)
            if not parts or tuple(map(int, parts.groups())) < (2, 24, 4):
                raise ValueError("Docker Compose 2.24.4 or newer is required.")
    except (ValueError, OSError, bootstrap.Refused):
        docker = False
        conflict("edge", "docker_unavailable", "Cannot verify trusted local Docker and Compose 2.24.4+; inventory is unknown.")
    try:
        edge = bootstrap.settings_for(bootstrap.read_env(env_file if env_file.exists() else template))
    except (OSError, ValueError, bootstrap.Refused):
        conflict("edge", "edge_settings", "Repair Edge settings before planning installation.")
        return result
    if args.tailscale and (edge["PE_ACCESS_MODE"] == "public" or edge["PE_BIND_HOST"] != "127.0.0.1"):
        conflict("edge", "tailscale_access", "Tailscale requires local/proxy access with the loopback listener.")
        return result
    origin_settings = dict(edge)
    connection = None
    if args.tailscale:
        try:
            tail_status = json.loads(inspect(["tailscale", "status", "--json"]))
            tail_serve = tailscale_serve.serve_status(inspect(["tailscale", "serve", "status", "--json"]))
            connection = tailscale_serve.selected_plan(edge, {}, tail_status, tail_serve)
            edge = bootstrap.settings_for(edge | connection["changes"]["edge"])
            result["connection"] = {"links": tailscale_serve.links(connection["endpoints"]), "state": "planned"}
        except (OSError, ValueError, KeyError, TypeError, bootstrap.Refused):
            conflict("edge", "tailscale_preflight", "Cannot verify the selected private Tailscale endpoints.")
            return result
    network = edge["PE_PLATFORM_NETWORK"]
    peer = edge["PE_TAILSCALE_EDGE_IP"] or None
    if docker:
        try:
            networks = inspect(["docker", "network", "ls", "--format", "{{.Name}}"])
            if network in networks.splitlines():
                network_type = inspect(["docker", "network", "inspect", network, "--format", "{{.Driver}} {{.Internal}}"])
                if args.tailscale:
                    bridge = tailscale_serve.bridge_gateway(json.loads(inspect(["docker", "network", "inspect", network]))[0])
                    connection["changes"]["edge"]["PE_TRUSTED_PROXIES"] = bridge
                if network_type != "bridge false":
                    raise ValueError("Platform Network must be a non-internal local bridge.")
        except (ValueError, OSError, KeyError, TypeError, IndexError, bootstrap.Refused):
            conflict("edge", "network_unverified", "Cannot verify the existing Platform Network as a non-internal bridge.")
    if (args.tailscale or edge["PE_TAILSCALE_HOST"]) and (edge["PE_ACCESS_MODE"] == "public" or edge["PE_BIND_HOST"] != "127.0.0.1"):
        conflict("edge", "tailscale_access", "Tailscale requires local/proxy access with the loopback listener.")
    listeners = []
    try:
        if "ss" not in missing:
            for line in inspect(["ss", "-H", "-ltn"]).splitlines():
                address, port = line.split()[3].rsplit(":", 1)
                listeners.append((address.strip("[]"), int(port)))
    except (ValueError, IndexError, OSError, bootstrap.Refused):
        conflict("edge", "ports_unverified", "Cannot inspect host TCP listeners.")
    publications = []
    if docker:
        try:
            publications = [json.loads(line) for line in inspect(["docker", "ps", "--no-trunc", "--format", "json"]).splitlines()]
        except (ValueError, OSError, bootstrap.Refused):
            conflict("edge", "ports_unverified", "Cannot inspect Docker TCP publications.")
    wanted = []
    for name in selected:
        prefix, project_default, entrypoint = STACKS[name]
        directory = root if name == "edge" else (getattr(args, name + "_dir") or root.parent / project_default).resolve()
        env = env_file if name == "edge" else directory / ".env"
        action = {"stack": name, "depends_on": [] if name == "edge" else ["edge"],
                  "entrypoint": entrypoint, "checkout": str(directory), "state": "planned",
                  "recovery": "infra/backup/README.md" if name == "backplane" else "docs/operations/backup.md",
                  "network": network, "preserve": "existing env, secrets, storage, volumes, native Compose selection and routes"}
        result["actions"].append(action)
        try:
            required = [entrypoint, "compose.yaml", "Caddyfile", "compose.tailscale.yaml"] if name == "edge" else [entrypoint, ".env.example", "compose.yaml"]
            if name in {"gateway", "observability"}:
                required.append("compose.proxy.yaml")
            if name == "backplane":
                required += ["compose.gateway.yaml", "compose.edge.yaml", "package.json", "bun.lock"]
            if args.status_timers:
                required.append("scripts/install_status_timer.py")
            if any(not (directory / filename).is_file() for filename in required):
                raise ValueError("Selected checkout is missing an owning entrypoint or required configuration file.")
            custody(env)
            recorded = read_settings(env) if env.exists() else {}
            if recorded.get("COMPOSE_ENV_FILES") or recorded.get("COMPOSE_PATH_SEPARATOR", ":") != ":":
                raise ValueError("Alternate Compose env files or separators require owning configuration repair.")
            values = read_settings(template if name == "edge" else directory / ".env.example") | recorded
            if name == "observability" and not (values.get("OB_ALERT_WEBHOOK_URL")
                    or values.get("OB_ALERT_EMAIL") and values.get("OB_SMTP_URL")
                    or values.get("OB_ALERTS") == "placeholder"):
                raise ValueError("Configure Observability alert delivery, or explicitly record OB_ALERTS=placeholder for degraded delivery, before selected setup.")
            project = (recorded if env.exists() else values).get("COMPOSE_PROJECT_NAME") or project_default
            volume_prefix = values.get(prefix + "_VOLUME_PREFIX") or project_default
            if not all(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", item) for item in (project, volume_prefix)):
                raise ValueError("Repair the recorded project identity or volume prefix.")
            action.update(project=project, volume_prefix=volume_prefix,
                          compose_selection={key: recorded[key] for key in ("COMPOSE_FILE", "COMPOSE_PROFILES") if key in recorded})
            for key, value in recorded.items():
                if key.startswith((prefix + "_", "COMPOSE_")) and "$" in value and key != "COMPOSE_FILE":
                    raise ValueError("Resolve interpolated installation settings with the owning bootstrap before planning.")
            # Resolve the owning Gateway template selection, not arbitrary shell expressions.
            default_files = values.get("COMPOSE_FILE", "compose.yaml") if name == "gateway" else "compose.yaml"
            files = recorded.get("COMPOSE_FILE", default_files).replace("${LG_ACCESS_MODE:-local}", values.get("LG_ACCESS_MODE", "local"))
            if "$" in files or any(not (directory / filename).is_file() for filename in files.split(os.pathsep)):
                raise ValueError("Recorded Compose selection has unavailable or unresolved files; preserve and repair it.")
            if recorded.get(prefix + "_PLATFORM_NETWORK", network) != network:
                conflict(name, "network_conflict", "Preserve the recorded network; reconcile through the owning ingress procedure.")
            data_key, data_default = {"edge": (None, "data"), "gateway": ("LG_POSTGRES_DATA_DIR", "data/postgres"),
                                      "backplane": ("BP_DATA_DIR", "data"), "observability": ("OB_STATE_DIR", "data")}[name]
            data = (directory / values.get(data_key, data_default)).resolve()
            existing = env.exists() or data.exists()
            containers, volumes, named = "", "", ""
            if docker:
                containers = inspect(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project])
                volumes = inspect(["docker", "volume", "ls", "--filter", "label=com.docker.compose.project=" + project, "--format", "{{.Name}}"])
                named = inspect(["docker", "volume", "ls", "--filter", "name=^" + re.escape(volume_prefix) + ("-" if name == "gateway" else "_"), "--format", "{{.Name}}"])
                existing = existing or bool(containers or volumes or named)
            action["installation"] = "existing" if existing else ("fresh" if docker else "unknown")
            scheme = "https" if name == "backplane" or origin_settings["PE_TAILSCALE_HOST"] else origin_settings["PE_SCHEME"]
            port = origin_settings["PE_HTTPS_PORT" if scheme == "https" else "PE_HTTP_PORT"]
            suffix = "" if port == ("443" if scheme == "https" else "80") else ":" + port
            if origin_settings["PE_ACCESS_MODE"] in {"public", "proxy"}:
                suffix = ""
            host = {"edge": "", "gateway": "", "backplane": "backplane.", "observability": "grafana."}[name]
            origin = scheme + "://" + host + origin_settings["PE_PUBLIC_DOMAIN"] + suffix
            tail_app = {"edge": "", "gateway": "gateway", "backplane": "backplane", "observability": "grafana"}[name]
            connected = origin_settings["PE_TAILSCALE_APPS"].split(",")
            if origin_settings["PE_TAILSCALE_HOST"] and (not tail_app or tail_app in connected):
                tail_port = origin_settings["PE_TAILSCALE_" + {"edge": "PORT", "gateway": "GATEWAY_PORT", "backplane": "BACKPLANE_PORT", "observability": "GRAFANA_PORT"}[name]]
                origin = "https://" + origin_settings["PE_TAILSCALE_HOST"] + ":" + tail_port
            action["origin"] = origin
            connection_changes = {}
            if args.tailscale:
                selected_connection = tailscale_serve.selected_plan(edge, {name: values}, tail_status, tail_serve)
                connection["endpoints"].update(selected_connection["endpoints"])
                connection_changes = selected_connection["changes"][name]
                action["origin"] = selected_connection["endpoints"][tail_app or "Platform Edge"]["url"].rstrip("/")
            changes = dict(connection_changes) if name == "edge" else {}
            if name == "edge":
                ports = [edge["PE_HTTP_PORT"]] + ([edge["PE_HTTPS_PORT"]] if edge["PE_ACCESS_MODE"] != "proxy" else [])
                bind = edge["PE_BIND_HOST"]
            else:
                key = "BP_PORT" if name == "backplane" else prefix + "_HTTP_PORT"
                ports = [recorded.get(key, {"gateway": "18080", "backplane": "3000", "observability": "18180"}[name])]
                bind = "127.0.0.1"
                action["access_mode"] = "proxy"
                action["trusted_edge_peer"] = "planned: exact address pinned and verified at execution"
                expected = {prefix + "_ACCESS_MODE": "proxy", prefix + "_BIND_HOST": bind}
                if name == "backplane":
                    expected["BP_PUBLIC_URL"] = origin
                else:
                    expected.update({prefix + "_PUBLIC_DOMAIN": origin_settings["PE_PUBLIC_DOMAIN"], prefix + "_SCHEME": scheme,
                                     prefix + "_PUBLIC_PORT_SUFFIX": suffix})
                if name == "observability":
                    expected["OB_GRAFANA_URL"] = origin
                if name == "gateway":
                    for app, label in (("gateway", "CONSOLE"), ("litellm", "LITELLM"), ("langfuse", "LANGFUSE"), ("s3", "S3"), ("rustfs", "RUSTFS")):
                        app_host = "" if app == "gateway" else app + "."
                        url = scheme + "://" + app_host + origin_settings["PE_PUBLIC_DOMAIN"] + suffix
                        if origin_settings["PE_TAILSCALE_HOST"] and app in connected:
                            url = "https://" + origin_settings["PE_TAILSCALE_HOST"] + ":" + origin_settings["PE_TAILSCALE_" + app.upper() + "_PORT"]
                        expected["LG_" + label + "_URL"] = url
                expected[prefix + "_PLATFORM_NETWORK"] = network
                expected[key] = ports[0]
                if name != "backplane" or values.get("BP_RUSTFS_CONSOLE") == "true":
                    proxies = [ipaddress.ip_interface(value) for value in recorded.get(prefix + "_TRUSTED_PROXIES", "").split()]
                    if any(proxy.network.prefixlen != proxy.max_prefixlen for proxy in proxies):
                        raise ValueError("Proxy trust must contain only the exact Edge address, never a network range.")
                    if recorded.get(prefix + "_TRUSTED_PROXIES") and not peer:
                        conflict(name, "peer_unknown", "Start or pin Edge first; its exact peer must be known before saved proxy trust can be verified.")
                    expected[prefix + "_TRUSTED_PROXIES"] = peer + "/32" if peer else recorded.get(prefix + "_TRUSTED_PROXIES", "192.0.2.1/32")
                    if recorded.get(prefix + "_TRUSTED_PROXIES") and peer:
                        if {str(ipaddress.ip_interface(v).ip) for v in recorded[prefix + "_TRUSTED_PROXIES"].split()} == {peer}:
                            expected[prefix + "_TRUSTED_PROXIES"] = recorded[prefix + "_TRUSTED_PROXIES"]
                # Accept the original Edge origins or the requested Tailnet origins, never arbitrary public settings.
                prior = dict(expected)
                expected.update(connection_changes)
                changes.update(expected)
                if any(key in recorded and recorded[key] != value and (not args.tailscale or recorded[key] != prior.get(key))
                       for key, value in expected.items()):
                    conflict(name, "origin_conflict", "Recorded access settings differ; use the owning ingress/reconfiguration procedure.")
            for port in ports:
                if not port.isdigit() or not 1 <= int(port) <= 65535:
                    raise ValueError("Configured listener port must be from 1 to 65535.")
            action["listeners"] = [bind + ":" + port for port in ports]
            if name in {"gateway", "backplane"}:
                backup = getattr(args, name + "_backup_dir")
                key = prefix + "_BACKUP_DIR"
                if backup and recorded.get(key) and backup.resolve() != (directory / recorded[key]).resolve():
                    conflict(name, "backup_conflict", "Preserve the recorded backup directory.")
                # Operator identities and custody exceptions must be explicitly recorded.
                backup = backup.resolve() if backup else (directory / recorded[key]).resolve() if recorded.get(key) else None
                if backup is None or not backup.is_dir() or not os.access(backup, os.W_OK | os.X_OK):
                    raise ValueError("Supply an existing writable mounted backup directory for the selected stack.")
                changes[key] = str(backup) if key not in recorded else recorded[key]
                if backup.is_relative_to(data) or data.is_relative_to(backup):
                    raise ValueError("Backup and data directories must not overlap.")
                parent = data
                while not parent.exists():
                    parent = parent.parent
                if name == "gateway" and recorded.get("LG_ALLOW_SAME_FILESYSTEM_BACKUP", "false") != "true" and backup.stat().st_dev == parent.stat().st_dev:
                    raise ValueError("Gateway backups require a separate filesystem from Postgres data.")
            if name == "gateway":
                email = args.gateway_email or recorded.get("LANGFUSE_INIT_USER_EMAIL", "")
                if not re.fullmatch(r"[^\s@]+@[^\s@]+", email) or email.lower().endswith("@example.com"):
                    raise ValueError("Supply --gateway-email with the operator's Langfuse login email.")
                changes["LANGFUSE_INIT_USER_EMAIL"] = email
                if args.gateway_email and recorded.get("LANGFUSE_INIT_USER_EMAIL", email) != email:
                    conflict(name, "email_conflict", "Preserve the recorded Langfuse login email.")
            if name == "backplane":
                action["mode"] = args.backplane_mode or ("preserve recorded selection" if "COMPOSE_PROFILES" in recorded else "full")
                if args.backplane_mode:
                    capabilities = set(recorded.get("COMPOSE_PROFILES", "").split(",")) & {"blobs", "compute"}
                    if "COMPOSE_PROFILES" in recorded and capabilities != ({"blobs", "compute"} if args.backplane_mode == "full" else set()):
                        conflict(name, "mode_conflict", "Requested capabilities differ from the recorded selection; use Backplane migration procedures.")
                capability = args.capability_file
                if capability is None or not capability.is_absolute() or capability.parent.resolve() != capability.parent or not capability.parent.is_dir():
                    raise ValueError("Supply --capability-file as an absolute path with an existing private parent.")
                parent_stat = capability.parent.stat()
                if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != os.getuid() or parent_stat.st_mode & 0o077:
                    raise ValueError("Capability parent must satisfy Backplane's private-directory custody contract.")
                if not os.access(capability.parent, os.W_OK | os.X_OK):
                    raise ValueError("Capability parent must be writable and searchable by the operator.")
                if capability.exists() or capability.is_symlink():
                    info = capability.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                        raise ValueError("Existing capability file must be an owned single-link regular file with mode 0600.")
                lock = env.with_name(env.name + ".lock")
                if lock.exists() or lock.is_symlink():
                    conflict(name, "backplane_lock", "Preserve Backplane .env.lock. Confirm no installer, preparation or bootstrap process is running before removing only that confirmed stale lock, then rerun the same selection.")
            profiles = [p for p in recorded.get("COMPOSE_PROFILES", "").split(",") if p]
            if name == "backplane":
                if "COMPOSE_PROFILES" not in recorded:
                    profiles = (["blobs", "compute"] if args.backplane_mode != "minimal" else []) + ["gateway"]
                if any(p not in {"gateway", "blobs", "compute"} for p in profiles) or "gateway" not in profiles:
                    raise ValueError("Backplane must retain gateway ingress; migrate standalone ingress through its owning procedure.")
                expected_backend = "s3" if "blobs" in profiles else "filesystem"
                if recorded.get("BP_BLOB_BACKEND", expected_backend) != expected_backend:
                    raise ValueError("Backplane backend differs from recorded profiles; use its migration procedure.")
                default_files = ["compose.yaml"] + ["compose." + p + ".yaml" for p in profiles]
            elif name == "observability":
                default_files = ["compose.yaml"] + (["compose.s3.yaml"] if "s3" in profiles else []) + ["compose.proxy.yaml"]
            else:
                default_files = ["compose.yaml"] + (["compose.proxy.yaml"] if name == "gateway" else [])
            configured_files = recorded.get("COMPOSE_FILE", values.get("COMPOSE_FILE", ":".join(default_files)) if name in {"edge", "gateway"} else ":".join(default_files))
            files = configured_files.replace("${LG_ACCESS_MODE:-local}", "proxy" if name == "gateway" else "local").split(":")
            if (directory / files[0]).resolve() != directory / "compose.yaml" or any(not (directory / f).is_file() for f in files):
                raise ValueError("Native Compose selection must retain its base file first and all ordered overlays.")
            if name == "observability":
                resolved = [(directory / filename).resolve() for filename in files]
                if (len(set(resolved)) != len(resolved) or directory / "compose.proxy.yaml" not in resolved
                        or (directory / "compose.s3.yaml" in resolved) != ("s3" in profiles)):
                    raise ValueError("Observability overlays must retain the selected storage and proxy access modes without duplicate files.")
            if name == "edge" and edge["PE_ACCESS_MODE"] in {"public", "proxy"}:
                mode_file = "compose." + edge["PE_ACCESS_MODE"] + ".yaml"
                if str(directory / mode_file) not in [str((directory / f).resolve()) for f in files]:
                    files.append(mode_file)
            if args.tailscale and name == "edge":
                files = [file for file in files if (directory / file).resolve() != directory / "compose.proxy.yaml"]
                if ":".join(files) != configured_files:
                    changes["COMPOSE_FILE"] = ":".join(files)
            if name != "backplane" and "COMPOSE_FILE" not in recorded:
                changes["COMPOSE_FILE"] = ":".join(files)
            if args.tailscale and name in {"backplane", "observability"} and values.get(prefix + "_RUSTFS_CONSOLE") == "true":
                required_profile = "blobs" if name == "backplane" else "s3"
                if required_profile not in profiles or (directory / ("compose." + required_profile + ".yaml")) not in [(directory / f).resolve() for f in files]:
                    raise ValueError("Enabled RustFS console requires its recorded native storage selection.")
            command = ["bun" if name == "backplane" else sys.executable, str(directory / entrypoint), "--env-file", str(env)]
            if name == "backplane":
                changes.update(COMPOSE_PROJECT_NAME=project, BP_VOLUME_PREFIX=volume_prefix)
                command += ["--capability-file", str(args.capability_file)]
                if "COMPOSE_PROFILES" not in recorded:
                    command += ["--profile", "gateway"]
                if args.backplane_mode:
                    command += ["--mode", args.backplane_mode]
            elif name == "edge":
                command += ["--template", str(template)]
            if any(any(character in value for character in "'\"\\\n\r$`") for value in changes.values()):
                raise ValueError("Installation inputs must be literal env values without quotes, escapes or interpolation.")
            item = {"name": name, "prefix": prefix, "root": directory, "env": env, "template": template if name == "edge" else directory / ".env.example",
                    "source": read_source(env), "template_source": read_source(template if name == "edge" else directory / ".env.example"), "recorded": recorded, "values": values, "changes": changes,
                    "files": files, "profiles": profiles, "command": command, "action": action, "ids": containers.split(), "volumes": set(volumes.split()) | set(named.split()),
                    "resources": bool(containers or volumes or named or name == "gateway" and data.exists() and (not os.access(data, os.R_OK) or any(data.iterdir())))}
            if docker:
                qualify(item, inspect)
                if name == "edge" and item["containers"]:
                    actual = item["containers"][0]["Networks"][network]["IPAddress"] or item["pinned_peer"]
                    if not actual:
                        raise ValueError("Stopped Edge has no qualified reserved peer; recover its original network identity first.")
                    if peer and peer != actual:
                        raise ValueError("Recorded Edge peer differs from the installed peer.")
                    peer = str(ipaddress.IPv4Address(actual))
                action["listeners"] = [bind + ":" + str(port) for bind, port in item["ports"]]
                wanted.extend((name, bind, port) for bind, port in item["ports"])
                prepared.append(item)
            action["compose_selection"] = {"COMPOSE_FILE": changes.get("COMPOSE_FILE", recorded.get("COMPOSE_FILE", ":".join(files))), "COMPOSE_PROFILES": recorded.get("COMPOSE_PROFILES", ",".join(profiles))}
        except (OSError, ValueError, KeyError, TypeError, SyntaxError, bootstrap.Refused) as error:
            detail = str(error) if isinstance(error, ValueError) else "Selected installation inspection failed; repair paths/configuration without replacing state."
            conflict(name, "prerequisite_failed", detail)
    owned = {container["Id"] for item in prepared for container in item["containers"]}
    owned_ports = {(binding["HostIp"], int(binding["HostPort"])) for item in prepared for container in item["containers"] if container["Running"]
                   for target, bindings in (container["Ports"] or {}).items() if target.endswith("/tcp") for binding in (bindings or [])}
    foreign_ports = []
    for container in publications:
        if container["ID"] not in owned:
            for match in bootstrap.PORT.finditer(container.get("Ports", "")):
                foreign_ports.extend((match["host"].strip("[]"), port) for port in range(int(match["first"]), int(match["last"] or match["first"]) + 1))
    for index, (name, bind, port) in enumerate(wanted):
        overlaps = lambda address: address == bind or address in {"*", "0.0.0.0", "::"} or bind in {"0.0.0.0", "::"}
        if any(port == other_port and overlaps(address) for address, other_port in foreign_ports + [p for p in listeners if p not in owned_ports]) or any(port == other_port and overlaps(address) for _, address, other_port in wanted[:index]):
            conflict(name, "port_conflict", "Requested TCP listener has a foreign owner or is duplicated; preserve installed listeners.")
    if args.tailscale:
        try:
            final_connection = tailscale_serve.selected_plan(edge, {item["name"]: item["values"] for item in prepared}, tail_status, tail_serve)
            edge_ports = {(bind, port) for name, bind, port in wanted if name == "edge"}
            tailscale_serve.check_listeners(final_connection["endpoints"], tail_status, listeners, edge_ports)
            tailscale_serve.check_listeners(final_connection["endpoints"], {}, foreign_ports)
            if prepared:
                prepared[0]["changes"].update(connection["changes"]["edge"])
                prepared[0]["changes"].update(final_connection["changes"]["edge"])
                prepared[0]["connection"] = final_connection
            result["connection"] = {"links": tailscale_serve.links(final_connection["endpoints"]), "state": "planned"}
        except (ValueError, KeyError, TypeError):
            conflict("edge", "tailscale_conflict", "Selected Tailscale endpoint or host listener is foreign; nothing changed.")
        result["actions"].append({"stack": "edge", "action": "connect selected Tailscale applications; preserve existing PE_TAILSCALE_APPS", "depends_on": selected})

    if args.status_timers:
        result["actions"].append({"action": "install/resume owning status timers for selected stacks", "depends_on": selected})
        for item in prepared:
            try:
                inspect(timer_command(item) + ["--check"], cwd=str(item["root"]))
            except (OSError, ValueError, bootstrap.Refused):
                conflict(item["name"], "timer_preflight", "Owning timer must support --check and exact-pair retry for this native selection; preserve existing units. See docs/operations/status-observer.md.")
    result["executable"] = result["execution_supported"] and not result["conflicts"]
    return result


def install(root, env_file, template, args, runner, refused=bootstrap.Refused):
    prepared = []
    plan = preflight(root, env_file, template, args, runner, refused, prepared)
    if args.dry_run or plan["conflicts"]:
        return (1 if plan["conflicts"] else 0), plan
    def inspect(argv, **options):
        result = runner(argv, **options)
        if result.returncode:
            if argv[:2] == ["tailscale", "serve"] and "status" not in argv:
                raise tailscale_serve.ServeFailure(result)
            raise ValueError("Inspection failed.")
        return result.stdout.strip()
    report = execute(prepared, runner, inspect, refused)
    if report["stopped_at"] is None and args.tailscale:
        try:
            report["connection"] = tailscale_serve.connect(prepared[0]["connection"]["endpoints"], inspect)
            if report["connection"]["state"] != "verified":
                report["stopped_at"] = "tailscale"
        except (OSError, ValueError, KeyError, TypeError, KeyboardInterrupt, refused):
            report.update(stopped_at="tailscale", connection={"state": "unverified"},
                          next="Correct private HTTPS access and rerun the same selection; completed installations are retained.")
    if report["stopped_at"] is None and args.status_timers:
        report["timers"] = []
        for item in prepared:
            try:
                response = runner(timer_command(item) + ["--install"], cwd=str(item["root"]), quiet=True, timeout=120)
                if response.returncode:
                    raise ValueError("Owning timer failed")
            except (OSError, ValueError, KeyboardInterrupt, refused):
                report.update(stopped_at=item["name"] + "-timer", next="Preserve installed units and rerun the same selection after correcting the owning timer failure.")
                break
            report["timers"].append(item["name"])
    return (3 if report["stopped_at"] else 0), report
