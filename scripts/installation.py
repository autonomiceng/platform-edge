"""Read-only selected-installation preflight. Execution belongs to H-EXEC."""

from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path

import bootstrap

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
        if not value.startswith(("'", '"')) and ("#" in value or any(char.isspace() for char in value)):
            raise ValueError("Quote literal env values containing spaces or #; inline comments are unsupported.")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError("Repair unmatched quotes in the selected env file.")
            value = value[1:-1]
        result[match["key"]] = value
    return result


def preflight(root, env_file, template, args, runner, refused=bootstrap.Refused):
    selected = [name for name in STACKS if name == "edge" or name in args.stack]
    result = {"selected": selected, "actions": [], "conflicts": [], "execution_supported": False,
              "infrastructure": "unverified", "enrollment": "unverified",
              "deferred": ["H-EXEC: recheck all prerequisites under owning locks before any mutation",
                           "H-EXEC: compare installed images, mounts and secrets before recreation",
                           "H-EXEC: validate effective configurations with the owning bootstraps",
                           "H-EXEC: pin and verify exact Edge peer before configuring sibling trust"]}

    def conflict(stack, code, detail):
        result["conflicts"].append({"stack": stack, "code": code, "detail": detail})

    def inspect(argv):
        try:
            response = runner(argv)
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
    network = edge["PE_PLATFORM_NETWORK"]
    if docker:
        try:
            networks = inspect(["docker", "network", "ls", "--format", "{{.Name}}"])
            if network in networks.splitlines():
                network_type = inspect(["docker", "network", "inspect", network, "--format", "{{.Driver}} {{.Internal}}"])
                if network_type != "bridge false":
                    raise ValueError("Platform Network must be a non-internal local bridge.")
        except (ValueError, OSError, bootstrap.Refused):
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
    if docker:
        try:
            published = inspect(["docker", "ps", "--format", "{{.Ports}}"])
            for line in published.splitlines():
                for match in bootstrap.PORT.finditer(line):
                    first, last = int(match["first"]), int(match["last"] or match["first"])
                    listeners.extend((match["host"].strip("[]"), port) for port in range(first, last + 1))
        except (ValueError, OSError, bootstrap.Refused):
            conflict("edge", "ports_unverified", "Cannot inspect Docker TCP publications.")
    wanted = []
    for name in selected:
        prefix, project_default, entrypoint = STACKS[name]
        directory = root if name == "edge" else (getattr(args, name + "_dir") or root.parent / project_default).resolve()
        env = env_file if name == "edge" else directory / ".env"
        action = {"stack": name, "depends_on": [] if name == "edge" else ["edge"],
                  "entrypoint": entrypoint, "checkout": str(directory), "state": "planned",
                  "network": network, "preserve": "existing env, secrets, storage, volumes, native Compose selection and routes"}
        result["actions"].append(action)
        try:
            required = [entrypoint, "compose.yaml", "Caddyfile"] if name == "edge" else [entrypoint, ".env.example", "compose.yaml"]
            if name in {"gateway", "observability"}:
                required.append("compose.proxy.yaml")
            if name == "backplane":
                required += ["compose.gateway.yaml", "compose.edge.yaml", "package.json", "bun.lock"]
            if args.status_timers:
                required.append("scripts/install_status_timer.py")
            if any(not (directory / filename).is_file() for filename in required):
                raise ValueError("Selected checkout is missing an owning entrypoint or required configuration file.")
            recorded = read_settings(env) if env.exists() else {}
            values = read_settings(template if name == "edge" else directory / ".env.example") | recorded
            project = recorded.get("COMPOSE_PROJECT_NAME", project_default)
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
            if docker:
                containers = inspect(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project])
                volumes = inspect(["docker", "volume", "ls", "--filter", "label=com.docker.compose.project=" + project, "--format", "{{.Name}}"])
                named = inspect(["docker", "volume", "ls", "--filter", "name=^" + re.escape(volume_prefix) + "_", "--format", "{{.Name}}"])
                existing = existing or bool(containers or volumes or named)
            action["installation"] = "existing" if existing else ("fresh" if docker else "unknown")
            if existing:
                runbook = "infra/backup/README.md" if name == "backplane" else "docs/operations/backup.md"
                conflict(name, "checkpoint_review_required", "Preserve installation state. Use this stack's " + runbook + " Checkpoint/upgrade route; H-EXEC must verify image and mount identity before reuse.")
            scheme = "https" if name == "backplane" or args.tailscale or edge["PE_TAILSCALE_HOST"] else edge["PE_SCHEME"]
            port = edge["PE_HTTPS_PORT" if scheme == "https" else "PE_HTTP_PORT"]
            suffix = "" if port == ("443" if scheme == "https" else "80") else ":" + port
            if edge["PE_ACCESS_MODE"] in {"public", "proxy"}:
                suffix = ""
            host = {"edge": "", "gateway": "", "backplane": "backplane.", "observability": "grafana."}[name]
            origin = scheme + "://" + host + edge["PE_PUBLIC_DOMAIN"] + suffix
            if args.tailscale or edge["PE_TAILSCALE_HOST"]:
                tail_port = edge["PE_TAILSCALE_" + {"edge": "PORT", "gateway": "GATEWAY_PORT", "backplane": "BACKPLANE_PORT", "observability": "GRAFANA_PORT"}[name]]
                origin = "https://" + edge["PE_TAILSCALE_HOST"] + ":" + tail_port if edge["PE_TAILSCALE_HOST"] else "planned: authenticated Tailscale machine HTTPS origin"
            action["origin"] = origin
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
                    expected.update({prefix + "_PUBLIC_DOMAIN": edge["PE_PUBLIC_DOMAIN"], prefix + "_SCHEME": scheme,
                                     prefix + "_PUBLIC_PORT_SUFFIX": suffix})
                if any(key in recorded and recorded[key] != value for key, value in expected.items()):
                    conflict(name, "origin_conflict", "Recorded access settings differ; use the owning ingress/reconfiguration procedure.")
            for port in ports:
                if not port.isdigit() or not 1 <= int(port) <= 65535:
                    raise ValueError("Configured listener port must be from 1 to 65535.")
                wanted.append((name, bind, int(port)))
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
                if args.gateway_email and recorded.get("LANGFUSE_INIT_USER_EMAIL", email) != email:
                    conflict(name, "email_conflict", "Preserve the recorded Langfuse login email.")
            if name == "backplane":
                action["mode"] = args.backplane_mode or ("preserve recorded selection" if existing else "owning Backplane default; B-PROMOTE pending")
                if args.backplane_mode:
                    capabilities = set(recorded.get("COMPOSE_PROFILES", "").split(",")) & {"blobs", "compute"}
                    if existing and capabilities != ({"blobs", "compute"} if args.backplane_mode == "full" else set()):
                        conflict(name, "mode_conflict", "Requested capabilities differ from the recorded selection; use Backplane migration procedures.")
                    conflict(name, "backplane_mode_pending", "Mode selection requires B-PROMOTE's owning bootstrap interface; no default or overlay is inferred here.")
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
                if env.with_name(env.name + ".lock").exists():
                    conflict(name, "backplane_lock", "Preserve the preparation lock; follow Backplane's interrupted-preparation recovery procedure.")
                result["deferred"].append("Backplane: verify capabilities and enrollment independently; emit bp bootstrap enrollment action")
        except (OSError, ValueError, bootstrap.Refused) as error:
            detail = str(error) if isinstance(error, ValueError) else "Selected installation inspection failed; repair paths/configuration without replacing state."
            conflict(name, "prerequisite_failed", detail)
    for index, (name, bind, port) in enumerate(wanted):
        overlaps = lambda address: address == bind or address in {"*", "0.0.0.0", "::"} or bind in {"0.0.0.0", "::"}
        if any(port == other_port and overlaps(address) for address, other_port in listeners) or any(port == other_port and overlaps(address) for _, address, other_port in wanted[:index]):
            conflict(name, "port_conflict", "Requested TCP listener is occupied or duplicated; verify ownership before execution and preserve recorded ports.")
    if args.tailscale:
        result["actions"].append({"stack": "edge", "action": "connect selected Tailscale applications; preserve existing PE_TAILSCALE_APPS", "depends_on": selected})
        result["deferred"].append("H-EXEC: selected-only Tailscale helper, authenticated machine/HTTPS/Serve conflict checks")
    if args.status_timers:
        result["actions"].append({"action": "install/resume owning status timers for selected stacks", "depends_on": selected})
        result["deferred"].append("H-EXEC: verify user manager, timer custody and matching interrupted units")
    return result
