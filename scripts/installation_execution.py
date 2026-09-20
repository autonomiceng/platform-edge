"""Owning bootstrap qualification and selected execution; no stack lifecycle commands."""

from __future__ import annotations

import ast
import contextlib
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile

import bootstrap


def secret_keys(name: str, source: str, profiles: list[str]) -> set[str]:
    if name == "edge":
        return set()
    if name == "backplane":
        if "--mode full|minimal" not in source:
            raise ValueError("Backplane requires the full/minimal owning bootstrap interface.")
        keys = set()
        for group in ["core"] + (["blobs"] if "blobs" in profiles else []):
            match = re.search(r"const " + group + r" = (\[[^;]+\]);", source)
            if not match:
                raise ValueError("Cannot identify the owning bootstrap's required secrets.")
            keys.update(json.loads(match[1]))
        return keys | ({"BP_COMPUTE_TOKEN"} if "compute" in profiles else set())
    keys = {"UI_USERNAME"} if name == "gateway" else set()
    declarations = {node.target.id: node.value for node in ast.parse(source).body
                    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)}
    for group in (["SECRETS", "PREFIXED"] if name == "gateway" else ["SECRETS"]):
        if group not in declarations:
            raise ValueError("Cannot identify the owning bootstrap's required secrets.")
        keys.update(ast.literal_eval(declarations[group]))
    return keys


def read_source(path: Path) -> str | None:
    if not path.exists():
        return None
    with path.open(newline="") as handle:
        return handle.read()


def custody(path: Path) -> None:
    parent = path.parent.stat()
    if path.parent.resolve() != path.parent or parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise ValueError("Env parent must be an owned directory without group/other write access.")
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError("Env custody is unknown; require an owned, private, single-link regular file.")


def amended(source: str, changes: dict[str, str]) -> str:
    pending = dict(changes)
    lines = []
    for line in source.splitlines(keepends=True):
        match = bootstrap.ENV_LINE.fullmatch(line.rstrip("\r\n"))
        if match and match["key"] in pending:
            value = pending.pop(match["key"])
            if match["value"].strip().strip("\"'") != value:
                line = match["key"] + "='" + value + "'\n"
        lines.append(line)
    text = "".join(lines)
    return text + ("\n" if text and not text.endswith("\n") and pending else "") + "".join(key + "='" + value + "'\n" for key, value in pending.items())


@contextlib.contextmanager
def owner_lock(item: dict):
    path = item["env"] if item["name"] == "edge" else item["env"].with_name(item["env"].name + ".lock")
    if item["name"] == "edge" and not path.exists():
        yield
        return
    exclusive = item["name"] == "backplane"
    flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if item["name"] != "edge" else 0) | (os.O_EXCL if exclusive else 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("Owning bootstrap lock custody is unknown.")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)
        if exclusive:
            path.unlink()


def publish(item: dict, changes: dict[str, str]) -> None:
    env = item["env"]
    custody(env)
    if read_source(env) != item["source"]:
        raise ValueError("Selected env changed during installation; rerun preflight.")
    source = item["source"]
    if source is None:
        source = "" if item["name"] == "backplane" else item["template_source"]
    updated = amended(source, changes)
    if updated == item["source"]:
        return
    fd, temporary = tempfile.mkstemp(prefix=".env.install-", dir=env.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            if item["source"] is not None:
                info = env.stat()
                os.fchmod(handle.fileno(), stat.S_IMODE(info.st_mode))
                os.fchown(handle.fileno(), -1, info.st_gid)
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        if item["source"] is None:
            os.link(temporary, env)
        else:
            os.replace(temporary, env)
        directory_fd = os.open(env.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)
    item["source"] = updated


def compose(item: dict) -> list[str]:
    command = ["docker", "compose", "--project-name", item["action"]["project"], "--project-directory", str(item["root"]),
               "--env-file", str(item["env"] if item["env"].exists() else Path("/dev/null"))]
    files = list(item["files"])
    mode = item["values"].get("PE_ACCESS_MODE", "local")
    if item["name"] == "edge" and mode in {"public", "proxy"}:
        override = item["root"] / ("compose." + mode + ".yaml")
        files = [file for file in files if (item["root"] / file).resolve() != override] + [str(override)]
    for filename in files:
        command += ["-f", str(item["root"] / filename)]
    for profile in item["profiles"]:
        command += ["--profile", profile]
    return command


def qualify(item: dict, inspect) -> None:
    name, root, recorded = item["name"], item["root"], item["recorded"]
    custody(item["env"])
    owner_source = (root / item["action"]["entrypoint"]).read_text()
    keys = secret_keys(name, owner_source, item["profiles"])
    if any(key in os.environ for key in keys):
        raise ValueError("Unset owning bootstrap secret exports; use its env file.")
    missing = {key for key in keys if not recorded.get(key)}
    if missing and name == "gateway":
        legacy = re.search(r"LEGACY_PROJECTS = (\([^\n]+\))", owner_source)
        if legacy:
            for project in ast.literal_eval(legacy[1]):
                if inspect(["docker", "volume", "ls", "--filter", "name=^" + re.escape(project) + "_", "--format", "{{.Name}}"]):
                    raise ValueError("Legacy Gateway data requires its owning upgrade procedure.")
    if missing and (item["resources"] or any(recorded.get(key) for key in keys - {"UI_USERNAME"})):
        raise ValueError("Restore the original complete env; existing secrets must not be regenerated.")
    if item["resources"] and not recorded:
        raise ValueError("Restore the original env before adopting existing resources.")
    if name in {"gateway", "observability"} and item["source"]:
        for line in item["source"].splitlines():
            match = bootstrap.ENV_LINE.fullmatch(line)
            if match and match["key"] in keys and (match["value"].startswith(("'", '"')) or "${" in match["value"]):
                raise ValueError("Repair managed secret syntax through the owning bootstrap.")
    if name == "backplane" and any(recorded.get(key) for key in keys):
        if any(key not in recorded for key in ("COMPOSE_PROJECT_NAME", "COMPOSE_FILE", "COMPOSE_PROFILES", "BP_BLOB_BACKEND")):
            raise ValueError("Backplane requires confirmation of its original selection through its owning bootstrap.")
    environment = dict(os.environ) | item["values"] | item["changes"]
    # Fixed interpolation sentinels are never persisted or passed to an owning bootstrap.
    environment.update({key: "0" * 64 for key in missing})
    rendered = json.loads(inspect(compose(item) + ["config", "--format", "json"], env=environment))
    services = rendered["services"]
    if name == "edge":
        item["pinned_peer"] = services["caddy"].get("networks", {}).get("platform", {}).get("ipv4_address")
    if name == "backplane":
        environment = services["server"]["environment"]
        if environment.get("BP_BLOB_BACKEND", "filesystem") != ("s3" if "blobs" in item["profiles"] else "filesystem") or bool(environment.get("BP_COMPUTE_URL")) != ("compute" in item["profiles"]):
            raise ValueError("Rendered Backplane capabilities differ from its native selection; use its migration procedure.")
    referenced = {rendered["volumes"][mount["source"]]["name"] for service in services.values()
                  for mount in service.get("volumes", []) if mount["type"] == "volume"}
    volumes = set(item["volumes"])
    for volume in referenced:
        found = inspect(["docker", "volume", "ls", "--filter", "name=^" + re.escape(volume) + "$", "--format", "{{.Name}}"])
        volumes.update(found.split())
    item["resources"] = item["resources"] or bool(volumes)
    if item["resources"] and (missing or not recorded):
        raise ValueError("Restore the original complete env before adopting durable resources.")
    unlabelled = set()
    for volume in sorted(volumes):
        project = json.loads(inspect(["docker", "volume", "inspect", "--format", '{{json (index .Labels "com.docker.compose.project")}}', volume]))
        if project is None:
            unlabelled.add(volume)
        elif project != item["action"]["project"]:
            raise ValueError("Volume ownership is foreign; use the owning recovery runbook without adopting or replacing data.")
    item["ports"] = [(port.get("host_ip", "0.0.0.0"), int(port["published"])) for service in services.values()
                     for port in service.get("ports", []) if port.get("protocol", "tcp") == "tcp" and "published" in port]
    containers = []
    for identity in item["ids"]:
        projection = '{"Id":{{json .Id}},"Image":{{json .Image}},"Mounts":{{json .Mounts}},"Labels":{{json .Config.Labels}},"Ports":{{json .NetworkSettings.Ports}},"Networks":{{json .NetworkSettings.Networks}},"Running":{{json .State.Running}},"Binds":{{json (index .HostConfig "Binds")}},"ExplicitMounts":{{json (index .HostConfig "Mounts")}}}'
        container = json.loads(inspect(["docker", "inspect", "--format", projection, identity]))
        service = container["Labels"].get("com.docker.compose.service")
        if service not in services or container["Labels"].get("com.docker.compose.project") != item["action"]["project"]:
            raise ValueError("Existing container selection differs; use the owning Checkpoint/upgrade procedure.")
        desired = services[service]
        image = inspect(["docker", "image", "inspect", "--format", "{{.Id}}", desired["image"]])
        if image != container["Image"]:
            raise ValueError("Effective image ID differs; use the owning Checkpoint/upgrade procedure.")
        desired_mounts = {}
        for mount in desired.get("volumes", []):
            if mount["type"] == "volume":
                desired_mounts[mount["target"]] = ("volume", rendered["volumes"][mount["source"]]["name"])
            elif mount["type"] == "bind":
                desired_mounts[mount["target"]] = ("bind", mount["source"])
        actual_mounts = {mount["Destination"]: (mount["Type"], mount.get("Name") if mount["Type"] == "volume" else mount["Source"])
                         for mount in container["Mounts"] if mount["Type"] in {"volume", "bind"}}
        extra = actual_mounts.keys() - desired_mounts.keys()
        if extra:
            inherited = json.loads(inspect(["docker", "image", "inspect", "--format", "{{json .Config.Volumes}}", desired["image"]])) or {}
            explicit = {mount["Target"] for mount in container.get("ExplicitMounts") or []}
            explicit.update(bind.split(":")[-2] if ":" in bind else bind for bind in container.get("Binds") or [])
            for target in extra:
                kind, volume = actual_mounts[target]
                labels = json.loads(inspect(["docker", "volume", "inspect", "--format", "{{json .Labels}}", volume])) if kind == "volume" else {}
                if target not in inherited or target in explicit or "com.docker.volume.anonymous" not in (labels or {}):
                    raise ValueError("Persistent mount or checkout differs; use the owning Checkpoint/upgrade procedure.")
                # Docker creates image-declared anonymous volumes absent from Compose config.
                unlabelled.add(volume)
                del actual_mounts[target]
        if actual_mounts != desired_mounts:
            raise ValueError("Persistent mount or checkout differs; use the owning Checkpoint/upgrade procedure.")
        containers.append(container)
    if len(containers) != len({c["Labels"]["com.docker.compose.service"] for c in containers}):
        raise ValueError("Duplicate service containers require the owning recovery procedure.")
    owned = {container["Id"] for container in containers}
    for volume in sorted(unlabelled):
        users = set(inspect(["docker", "ps", "-aq", "--no-trunc", "--filter", "volume=" + volume]).split())
        mounted = any(mount.get("Name") == volume for container in containers for mount in container["Mounts"])
        if not mounted or not users or not users <= owned:
            raise ValueError("Volume ownership is unlabelled and cannot be established from qualified containers; use the owning recovery runbook.")
    if name == "observability":
        marker = root / item["values"].get("OB_STATE_DIR", "data") / "installation/storage-mode"
        expected = "s3" if "s3" in item["profiles"] else "filesystem"
        if (item["resources"] and not marker.is_file()) or (marker.exists() and marker.read_text().strip() != expected):
            raise ValueError("Observability storage selection is unknown or differs; use its storage migration runbook.")
    item["containers"] = containers


def edge_peer(item: dict, inspect) -> str:
    ids = inspect(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=" + item["action"]["project"],
                   "--filter", "label=com.docker.compose.service=caddy"]).split()
    if len(ids) != 1:
        raise ValueError("Exactly one running Edge peer is required.")
    networks = json.loads(inspect(["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", ids[0]]))
    return str(ipaddress.IPv4Address(networks[item["action"]["network"]]["IPAddress"]))


def execute(prepared: list[dict], runner, inspect, refused=bootstrap.Refused) -> dict:
    report = {"completed": [], "stopped_at": None, "infrastructure": "unverified", "enrollment": "unverified"}
    name = "edge"
    try:
        # Directory flock serializes installers without a manifest or persistent lock file.
        with contextlib.ExitStack() as cleanup:
            fd = os.open(prepared[0]["root"], os.O_RDONLY | os.O_DIRECTORY)
            cleanup.callback(os.close, fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locks = []
            for item in prepared:
                name = item["name"]
                lock = contextlib.ExitStack()
                cleanup.enter_context(lock)
                lock.enter_context(owner_lock(item))
                locks.append(lock)
                if read_source(item["env"]) != item["source"]:
                    raise ValueError("Selected configuration changed; rerun preflight.")
                qualify(item, inspect)
            peer = None
            for item, lock in zip(prepared, locks):
                name = item["name"]
                changes = dict(item["changes"])
                if name != "edge" and name != "backplane":
                    trust_key = item["prefix"] + "_TRUSTED_PROXIES"
                    changes[trust_key] = item["recorded"].get(trust_key, peer + "/32")
                if name == "backplane" and item["recorded"].get("BP_RUSTFS_CONSOLE") == "true":
                    changes["BP_TRUSTED_PROXIES"] = item["recorded"].get("BP_TRUSTED_PROXIES", peer + "/32")
                if name != "edge":
                    trust = changes.get(item["prefix"] + "_TRUSTED_PROXIES", peer)
                    if {str(ipaddress.ip_interface(value).ip) for value in trust.split()} != {peer}:
                        raise ValueError("The pinned peer differs from the qualified proxy trust.")
                if name == "gateway" and prepared[0].get("connection"):
                    allowed = item["recorded"].get("LG_OPERATOR_ALLOW", "127.0.0.0/8 ::1").split()
                    changes["LG_OPERATOR_ALLOW"] = " ".join(dict.fromkeys(allowed + [peer]))
                publish(item, changes)
                lock.close()
                response = runner(item["command"], timeout=1800, quiet=True, cwd=str(item["root"]))
                if response.returncode:
                    raise ValueError("Owning bootstrap failed.")
                if name == "edge":
                    peer = edge_peer(item, inspect)
                    source = read_source(item["env"])
                    item["source"] = source
                    files = list(item["files"])
                    pin = str(item["root"] / "compose.tailscale.yaml")
                    if pin not in [str((item["root"] / file).resolve()) for file in files]:
                        files.append(pin)
                    pin_settings = {"PE_TAILSCALE_EDGE_IP": peer, "COMPOSE_FILE": ":".join(files)}
                    if item.get("connection"):
                        network = json.loads(inspect(["docker", "network", "inspect", item["action"]["network"]]))[0]
                        from tailscale_serve import bridge_gateway
                        pin_settings["PE_TRUSTED_PROXIES"] = bridge_gateway(network)
                    if amended(source, pin_settings) != source:
                        with owner_lock(item):
                            publish(item, pin_settings)
                        if runner(item["command"], timeout=1800, quiet=True, cwd=str(item["root"])).returncode or edge_peer(item, inspect) != peer:
                            raise ValueError("Pinned Edge peer could not be verified.")
                report["completed"].append(name)
    except (OSError, ValueError, KeyError, TypeError, IndexError, KeyboardInterrupt, refused) as error:
        report["detail"] = str(error) if isinstance(error, ValueError) else "Selected installation stopped; inspect the owning status record privately."
        report.update(stopped_at=name, error="installation_stopped", recovery="infra/backup/README.md" if name == "backplane" else "docs/operations/backup.md", next="Correct the owning installation and rerun the same selection; completed stacks and all data are retained.")
    if "backplane" in report["completed"] and report["stopped_at"] is None:
        report["next"] = "Complete Backplane enrollment with bp bootstrap and the selected capability file."
    return report
