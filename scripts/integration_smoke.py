#!/usr/bin/env python3
"""Shared-host acceptance against installed applications, never stubs."""
import json
import os
import subprocess
from pathlib import Path

import bootstrap


def checked(argv):
    return subprocess.check_output(argv, text=True)


def main():
    parser = bootstrap.ArgumentParser(description=__doc__)
    parser.add_argument("--check-network", action="store_true")
    parser.add_argument("--ca-file", type=Path)
    args = parser.parse_args()
    network = os.environ["PE_PLATFORM_NETWORK"]
    if args.check_network:
        networks = checked(["docker", "network", "ls", "--format", "{{.Name}}"]).splitlines()
        if network not in networks:
            print(f"SKIP shared-host acceptance: platform network {network} is absent (0 routes checked)")
            return 77
        members = json.loads(checked(["docker", "network", "inspect", network]))[0]["Containers"]
        aliases = set()
        if members:
            containers = json.loads(checked(["docker", "inspect", *members]))
            for container in containers:
                if container["State"]["Running"] and container["Config"]["Labels"].get("com.docker.compose.project"):
                    aliases.update(container["NetworkSettings"]["Networks"][network].get("Aliases") or [])
        missing = {"lg-gateway", "ob-gateway", "bp-server"} - aliases
        if missing:
            print(f"SKIP shared-host acceptance: missing sibling aliases {', '.join(sorted(missing))} "
                  f"on {network} (0 routes checked)")
            return 77
        return 0
    domain, scheme = os.environ["PE_PUBLIC_DOMAIN"], os.environ["PE_SCHEME"]
    port = os.environ["PE_HTTPS_PORT" if scheme == "https" else "PE_HTTP_PORT"]
    routes = [(domain, "/health/litellm", "litellm"),
              (f"litellm.{domain}", "/health/liveliness", "litellm"),
              (f"langfuse.{domain}", "/api/public/health", "langfuse"),
              (f"s3.{domain}", "/health/ready", "s3"),
              (f"backplane.{domain}", "/health/ready", "backplane"),
              (f"grafana.{domain}", "/api/health", "grafana")]
    for host, path, app in routes:
        argv = ["curl", "--noproxy", "*", "--max-time", "15", "-sS", "--fail",
                "--resolve", f"{host}:{port}:127.0.0.1", "-H", f"Host: {host}"]
        if scheme == "https":
            argv += ["--cacert", str(args.ca_file)]
        # Retain status explicitly: curl --fail alone also accepts redirects.
        raw = checked(argv + ["-w", "\n%{http_code}", f"{scheme}://{host}:{port}{path}"])
        body, status = raw.rsplit("\n", 1)
        assert status == "200", f"{host}{path}: expected 200, got {status}"
        assert "|" not in body and "<html" not in body.lower(), f"{host}: stub or HTML, not health"
        # Gateway public health preserves status but suppresses LiteLLM and Langfuse bodies.
        if app in {"litellm", "langfuse"}:
            assert body == "", f"{host}: public health must have an empty body"
        if app in {"backplane", "grafana"}:
            health = json.loads(body)
            if app == "grafana":
                assert health.get("database") == "ok", f"{host}: database not healthy"
            else:
                assert str(health.get("status", "")).lower() == "ready", f"{host}: unexpected health"
        print(f"ok: {scheme} {host}{path}: real {app} health")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
