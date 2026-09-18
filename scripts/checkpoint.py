#!/usr/bin/env python3
"""Capture, restore or deliberately destroy certificate state."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import bootstrap

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ("edge-data.tar", "edge-config.tar")
CA_PATH = "caddy/pki/authorities/local/root.crt"
COMMAND_TIMEOUT = 120
TRANSFER_TIMEOUT = 1800


def command_failed(stderr: str | bytes, diagnostics: Path, *, reason: str = "command failed") -> None:
    diagnostics.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(diagnostics, 0o700)
    path = diagnostics / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".log")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(stderr.encode() if isinstance(stderr, str) else stderr)
    raise RuntimeError(f"{reason}; diagnostics: {path}")


def checked(argv: list[str], diagnostics: Path, *, timeout: float | None = None) -> str:
    # Terminal interrupts must reach our handler, not kill a stop/start CLI mid-operation.
    timeout = COMMAND_TIMEOUT if timeout is None else timeout
    try:
        result = bootstrap.run(argv, start_new_session=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        command_failed(error.stderr or b"", diagnostics, reason=f"command timed out after {timeout:g} seconds")
    if result.returncode:
        command_failed(result.stderr, diagnostics)
    return result.stdout.strip()


def inventory(directory: Path) -> dict:
    result = {}
    for name in ARTIFACTS:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"missing or unsafe artifact: {name}")
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        result[name] = {"size": path.stat().st_size, "sha256": digest}
    return result


def archive_fingerprint(directory: Path) -> str | None:
    fingerprint = None
    for name in ARTIFACTS:
        with tarfile.open(directory / name, "r:") as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                    raise ValueError(f"unsafe archive entry in {name}")
                if path.parts and path.parts[0] == bootstrap.RESTORE_MARKER:
                    raise ValueError(f"incomplete restore marker in {name}")
                if name == "edge-data.tar" and str(path) == CA_PATH:
                    if not member.isfile() or member.size > 65536:
                        raise ValueError("invalid or oversized CA certificate")
                    with archive.extractfile(member) as handle:
                        fingerprint = bootstrap.ca_fingerprint(handle.read(65536).decode("ascii"))
    return fingerprint


def manifest(directory: Path, images: dict, commit: str, dirty: bool = False) -> dict:
    # Deliberately accept no env values: only public identifiers and artifact hashes.
    return {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
            "git_commit": commit, "git_dirty": dirty, "images": images, "caddy_stopped": True,
            "ca_sha256": archive_fingerprint(directory), "artifacts": inventory(directory)}


class Stack:
    def __init__(self, env_file: Path):
        self.env_file = env_file.resolve()
        self.settings = bootstrap.settings_for(bootstrap.read_env(self.env_file))
        self.repository = Path(self.settings["PE_BACKUP_DIR"])
        if not self.repository.is_absolute():
            self.repository = ROOT / self.repository
        self.diagnostics = self.repository / ".diagnostics"
        self.dc = bootstrap.compose_command(ROOT, self.env_file)
        config = json.loads(checked(self.dc + ["config", "--format", "json"], self.diagnostics))
        self.project = config["name"]
        self.images = {name: service["image"] for name, service in config["services"].items()}
        self.image = self.images["caddy"]
        self.volumes = bootstrap.volume_names(self.settings)
        actual = [config["volumes"][name]["name"] for name in ("edge-data", "edge-config")]
        if actual != self.volumes or set(self.images) != {"caddy"}:
            raise ValueError("unexpected Compose services or volumes")

    def helper(self, volume: str, *command: str, readonly: bool = True) -> list[str]:
        return ["docker", "run", "--rm", "-i", "--network", "none", "--read-only",
                "--security-opt", "no-new-privileges:true", "--mount",
                f"type=volume,src={volume},dst=/state" + (",readonly" if readonly else ""),
                "--entrypoint", "sh", self.image, "-ec", *command]

    def require_capture_provenance(self) -> None:
        ids = checked(self.dc + ["ps", "-aq", "caddy"], self.diagnostics).split()
        if len(ids) != 1:
            raise ValueError("Checkpoint requires exactly one existing Caddy container")
        containers = json.loads(checked(["docker", "inspect", *ids], self.diagnostics))
        if len(containers) != 1 or containers[0].get("Config", {}).get("Image") != self.image:
            raise ValueError("Caddy image differs from the Checkpoint image pin")
        mounts = {m["Destination"]: m.get("Name") for m in containers[0].get("Mounts", [])
                  if m.get("Type") == "volume"}
        if mounts != dict(zip(("/data", "/config"), self.volumes)):
            raise ValueError("Caddy persistent mounts differ from the Checkpoint volumes")

    def require_stopped(self) -> None:
        for volume in self.volumes:
            if checked(["docker", "ps", "-q", "--filter", f"volume={volume}"], self.diagnostics):
                raise ValueError(f"volume has a running consumer: {volume}")

    def stream(self, argv: list[str], path: Path, restore: bool = False) -> None:
        with path.open("rb" if restore else "xb") as handle:
            try:
                result = bootstrap.run_detached(argv, stdin=handle if restore else subprocess.DEVNULL,
                                                stdout=subprocess.DEVNULL if restore else handle,
                                                timeout=TRANSFER_TIMEOUT)
            except subprocess.TimeoutExpired as error:
                command_failed(error.stderr or b"", self.diagnostics,
                               reason=f"transfer timed out after {TRANSFER_TIMEOUT:g} seconds")
            if result.returncode:
                command_failed(result.stderr, self.diagnostics)
            if not restore:
                handle.flush()
                os.fsync(handle.fileno())


def backup(stack: Stack, directory: Path) -> None:
    stack.require_capture_provenance()
    commit = checked(["git", "-C", str(ROOT), "rev-parse", "HEAD"], stack.diagnostics)
    dirty = bool(checked(["git", "-C", str(ROOT), "status", "--porcelain"], stack.diagnostics))
    for volume in stack.volumes:
        checked(["docker", "volume", "inspect", volume], stack.diagnostics)
        marker = checked(stack.helper(volume,
                         f"if test -e /state/{bootstrap.RESTORE_MARKER}; then echo marker; "
                         "else ls /state >/dev/null; fi"), stack.diagnostics)
        if marker:
            raise ValueError(f"restore marker present in {volume}")
    expected = 0
    for volume in stack.volumes:
        size = checked(stack.helper(volume, "du -sk /state"), stack.diagnostics).split()
        if not size or not size[0].isdigit():
            raise ValueError(f"cannot estimate volume size: {volume}")
        expected += int(size[0]) * 1024 + 10240
    if shutil.disk_usage(directory.parent).free < expected:
        raise ValueError("insufficient free space for Checkpoint")
    running = bool(checked(stack.dc + ["ps", "--status", "running", "-q", "caddy"], stack.diagnostics))
    directory.mkdir(mode=0o700)
    interrupted = False
    def defer_signal(signum, frame):
        nonlocal interrupted
        interrupted = True
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    try:
        # Let stop finish before start, and never interrupt the resumption subprocess.
        for sig in handlers:
            signal.signal(sig, defer_signal)
        if running:
            checked(stack.dc + ["stop", "--timeout", "30", "caddy"], stack.diagnostics)
        if interrupted:
            raise RuntimeError("interrupted; resuming Caddy if it was running")
        stack.require_stopped()
        if interrupted:
            raise RuntimeError("interrupted; resuming Caddy if it was running")
        for volume, name in zip(stack.volumes, ARTIFACTS):
            stack.stream(stack.helper(volume, "tar -C /state -cf - ."), directory / name)
            if interrupted:
                raise RuntimeError("interrupted; resuming Caddy if it was running")
    finally:
        pending = sys.exc_info()[0]
        try:
            if running:
                try:
                    deadline = time.monotonic() + 300
                    def remaining():
                        return max(0, min(20, deadline - time.monotonic()))
                    def detached(argv):
                        return bootstrap.run(argv, start_new_session=True, timeout=remaining())
                    is_running = stack.dc + ["ps", "--status", "running", "-q", "caddy"]
                    last = "Caddy is not running"
                    while time.monotonic() < deadline:
                        try:
                            checked(stack.dc + ["start", "caddy"], stack.diagnostics, timeout=remaining())
                            if checked(is_running, stack.diagnostics, timeout=remaining()):
                                # An in-flight stop can finish after start was a no-op.
                                bootstrap.wait_ready(stack.settings, ROOT, stack.env_file, detached, remaining())
                                if (checked(is_running, stack.diagnostics, timeout=remaining())
                                        and time.monotonic() < deadline):
                                    break
                            last = "Caddy is not running"
                        except subprocess.TimeoutExpired:
                            last = "Caddy resumption command timed out"
                        except (OSError, RuntimeError, bootstrap.Refused) as error:
                            last = getattr(error, "detail", "") or str(error)
                        time.sleep(min(3, remaining()))
                    else:
                        raise RuntimeError(f"Caddy did not resume within 300 seconds: {last}")
                except (OSError, RuntimeError, bootstrap.Refused) as error:
                    if pending is None:
                        raise
                    print(f"Caddy resumption also failed: {error}", file=sys.stderr)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        if interrupted and pending is None:
            raise RuntimeError("interrupted; Caddy resumption completed")
    # Publish completion only after capture and service resumption succeed.
    document = manifest(directory, stack.images, commit, dirty)
    with (directory / "manifest.json").open("x") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    prune(directory.parent, int(stack.settings["PE_BACKUP_KEEP"]))
    print(json.dumps({"checkpoint": str(directory), "ca_sha256": document["ca_sha256"]}))


def prune(repository: Path, keep: int) -> None:
    if keep < 1:
        raise ValueError("PE_BACKUP_KEEP must be at least 1")
    complete = []
    for directory in sorted(repository.iterdir()):
        if (not re.fullmatch(r"\d{8}T\d{12}Z", directory.name)
                or directory.is_symlink() or not directory.is_dir()):
            continue
        path = directory / "manifest.json"
        if path.is_symlink() or not path.is_file():
            continue
        try:
            document = json.loads(path.read_text())
            if (isinstance(document, dict) and document.get("version") == 1
                    and document.get("caddy_stopped") is True
                    and document.get("artifacts") == inventory(directory)):
                complete.append(directory)
        except (OSError, ValueError):
            continue
    for directory in complete[:-keep]:
        shutil.rmtree(directory)


def restore(stack: Stack, directory: Path) -> None:
    document = json.loads((directory / "manifest.json").read_text())
    if document.get("version") != 1 or document.get("caddy_stopped") is not True:
        raise ValueError("unsupported or incomplete Checkpoint")
    if document["images"] != stack.images:
        raise ValueError("restore requires the Checkpoint image pins")
    if inventory(directory) != document["artifacts"]:
        raise ValueError("Checkpoint checksum mismatch")
    if archive_fingerprint(directory) != document["ca_sha256"]:
        raise ValueError("Checkpoint CA fingerprint mismatch")
    if checked(stack.dc + ["ps", "--status", "running", "-q"], stack.diagnostics):
        raise ValueError("restore requires the project stopped")
    stack.require_stopped()
    for volume in stack.volumes:
        checked(["docker", "volume", "create", volume], stack.diagnostics)
    for volume in stack.volumes:
        try:
            result = bootstrap.run(stack.helper(volume, 'entries=$(ls -A /state); test -z "$entries"'),
                                   start_new_session=True, timeout=COMMAND_TIMEOUT)
        except subprocess.TimeoutExpired as error:
            command_failed(error.stderr or b"", stack.diagnostics, reason="restore state check timed out")
        if result.returncode:
            raise ValueError(f"restore refused: non-empty or unreadable volume {volume}")
    for volume in stack.volumes:
        checked(stack.helper(volume, f"touch /state/{bootstrap.RESTORE_MARKER}", readonly=False), stack.diagnostics)
    for volume, name in zip(stack.volumes, ARTIFACTS):
        stack.stream(stack.helper(volume, "tar -C /state -xf -", readonly=False), directory / name, True)
    for volume in stack.volumes:
        checked(stack.helper(volume, f"rm /state/{bootstrap.RESTORE_MARKER}", readonly=False), stack.diagnostics)
    print(json.dumps({"restored": str(directory), "ca_sha256": document["ca_sha256"],
                      "next": "Run bootstrap to start Caddy and verify TLS."}))


def confirm_destroy(project: str, volumes: list[str]) -> None:
    print(f"Delete project {project} and volumes {', '.join(volumes)}. Type {project}:", file=sys.stderr)
    if sys.stdin.readline().rstrip("\n") != project:
        raise ValueError("destroy refused: project name did not match")


def main() -> int:
    parser = bootstrap.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backup", "restore", "destroy"))
    parser.add_argument("checkpoint", nargs="?", type=Path)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    if (args.command == "restore") != (args.checkpoint is not None):
        parser.error("only restore requires a Checkpoint directory")
    os.umask(0o077)
    with args.env_file.open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stack = Stack(args.env_file)
        if args.command == "destroy":
            confirm_destroy(stack.project, stack.volumes)
            checked(stack.dc + ["down", "--remove-orphans"], stack.diagnostics)
            checked(["docker", "volume", "rm", *stack.volumes], stack.diagnostics)
        else:
            repository = stack.repository
            repository.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (repository / ".checkpoint.lock").open("a") as repository_lock:
                fcntl.flock(repository_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if args.command == "backup":
                    backup(stack, repository / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
                else:
                    restore(stack, args.checkpoint.resolve())
    return 0


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise RuntimeError("interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, KeyError, tarfile.TarError, bootstrap.Refused, KeyboardInterrupt) as error:
        print(json.dumps({"error": "checkpoint_failed", "detail": str(error)}), file=sys.stderr)
        raise SystemExit(1)
