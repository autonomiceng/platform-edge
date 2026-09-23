#!/usr/bin/env python3
"""Prove a disposable internal CA survives a Checkpoint restore."""
import json
import os
import re
import signal
import sys
import subprocess
import tempfile
import ipaddress
import time
import zlib
from pathlib import Path

import bootstrap

ROOT = Path(__file__).resolve().parent.parent


def main():
    project = os.environ.get("SMOKE_PROJECT", "platform-edge-drill")
    if not re.fullmatch(r"platform-edge-drill(?:-[a-z0-9-]+)?", project):
        raise ValueError("SMOKE_PROJECT must be platform-edge-drill or platform-edge-drill-<suffix>")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PE_", "COMPOSE_"))}
    env.update(COMPOSE_PROJECT_NAME=project, PE_VOLUME_PREFIX=project,
               PE_PUBLIC_DOMAIN="localhost", PE_SCHEME="https", PE_TLS_ISSUER="internal",
               PE_BIND_HOST="127.0.0.1", PE_HTTP_PORT=os.environ.get("SMOKE_HTTP_PORT", "18380"),
               PE_HTTPS_PORT=os.environ.get("SMOKE_HTTPS_PORT", "18743"),
               PE_PLATFORM_NETWORK=f"{project}-platform", PE_ACME_EMAIL="")
    # Bootstrap creates the drill network; a /24 under 172.16 keeps it clear of installed
    # and Docker-created networks, and the project name picks the octet. SMOKE_PLATFORM_SUBNET
    # moves it when 172.16.0.0/16 is taken on the host.
    subnet = ipaddress.IPv4Network(os.environ.get("SMOKE_PLATFORM_SUBNET", f"172.16.{zlib.crc32(project.encode()) % 256}.0/24"))
    if subnet.prefixlen != 24:
        raise ValueError("SMOKE_PLATFORM_SUBNET must be an IPv4 /24")
    env.update(PE_PLATFORM_SUBNET=str(subnet), PE_PLATFORM_IP_RANGE=str(list(subnet.subnets(prefixlen_diff=1))[1]),
               PE_EDGE_IP=str(subnet.network_address + 2))

    def run(*argv, input=None):
        result = subprocess.run(argv, cwd=ROOT, env=env, text=True, input=input,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode:
            raise RuntimeError(f"{' '.join(argv)}: {result.stderr}")
        return result.stdout.strip()

    volumes = bootstrap.volume_names(env)
    if run("docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"):
        raise ValueError("drill project already exists")
    existing = run("docker", "volume", "ls", "--format", "{{.Name}}").splitlines()
    if any(volume in existing for volume in volumes):
        raise ValueError("drill volumes already exist")
    if env["PE_PLATFORM_NETWORK"] in run("docker", "network", "ls", "--format", "{{.Name}}").splitlines():
        raise ValueError("drill network already exists")
    with tempfile.TemporaryDirectory(prefix="platform-edge-drill-") as work:
        env["PE_BACKUP_DIR"] = str(Path(work) / "backups")
        env_file = str(Path(work) / ".env")
        dc = ["docker", "compose", "--env-file", env_file]
        try:
            initial = json.loads(run("python3", "scripts/bootstrap.py", "--env-file", env_file))
            fingerprint = initial["certificate"]["ca_sha256"]
            captured = json.loads(run("scripts/backup.sh", "--env-file", env_file))
            if not (captured["ca_sha256"] == fingerprint):
                raise AssertionError("Checkpoint CA differs")
            started = time.monotonic()
            run("scripts/destroy.sh", "--env-file", env_file, input=project + "\n")
            run("scripts/restore.sh", captured["checkpoint"], "--env-file", env_file)
            restored = json.loads(run("python3", "scripts/bootstrap.py", "--env-file", env_file))
            if not (restored["certificate"]["ca_sha256"] == fingerprint):
                raise AssertionError("restored CA differs")
            if not (restored["certificate"]["not_after_seconds"] > time.time()):
                raise AssertionError("TLS certificate expired")
            result = (f"BACKUP DRILL PASSED (3 checks: Checkpoint CA, restored CA, verified TLS); "
                      f"RTO={time.monotonic() - started:.2f}s; CA SHA256={fingerprint}")
        finally:
            # Only resources whose names were absent at preflight belong to this run.
            pending = sys.exc_info()[0]
            failures = []
            def cleanup(*argv):
                try:
                    return run(*argv)
                except (OSError, RuntimeError) as error:
                    failures.append(str(error))
                    print(f"cleanup failed: {error}", file=sys.stderr)
                    return None
            cleanup(*dc, "down", "--remove-orphans")
            existing = cleanup("docker", "volume", "ls", "--format", "{{.Name}}")
            if existing is not None:
                for volume in volumes:
                    if volume in existing.splitlines():
                        cleanup("docker", "volume", "rm", volume)
            cleanup("docker", "network", "rm", env["PE_PLATFORM_NETWORK"])
            if failures and pending is None:
                raise RuntimeError("drill cleanup failed; inspect the reported resources")
        print(result)


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt("drill interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        main()
    except (OSError, ValueError, RuntimeError, AssertionError, KeyboardInterrupt) as error:
        raise SystemExit(f"FAIL: {error}")
