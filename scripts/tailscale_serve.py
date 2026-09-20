#!/usr/bin/env python3
"""Expose the local Edge console through Tailscale HTTPS without replacing other endpoints."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

import bootstrap


def checked(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError(f"{argv[0]} command failed; check Tailscale login and permissions (use sudo if required)")
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
    funnel = serve.get("AllowFunnel", {}).get(f"{name}:{port}", False)
    if funnel:
        raise ValueError("selected port is used by Funnel; choose another HTTPS port")
    if tcp and not tcp.get("HTTPS"):
        raise ValueError("selected port has a non-HTTPS listener; choose another port")
    if ((existing and existing != target) or ("/" in web and not existing)) and not replace:
        raise ValueError("selected root endpoint already exists; use another port or --replace")
    return {
        "url": f"https://{name}" + (f":{port}" if port != 443 else "") + "/",
        "command": ["tailscale", "serve", "--bg", f"--https={port}", "--yes", target],
        "undo": ["tailscale", "serve", f"--https={port}", "--set-path=/", "off"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(__file__).resolve().parent.parent / ".env")
    parser.add_argument("--https-port", type=int, default=443)
    parser.add_argument("--replace", action="store_true", help="replace only the selected HTTPS root handler")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        settings = bootstrap.settings_for(bootstrap.read_env(args.env_file))
        if settings["PE_ACCESS_MODE"] not in {"local", "proxy"}:
            raise ValueError("console sharing requires PE_ACCESS_MODE=local or proxy; public redirects are not a Serve backend")
        configuration = plan(json.loads(checked(["tailscale", "status", "--json"])),
                             json.loads(checked(["tailscale", "serve", "status", "--json"])) or {},
                             args.https_port, int(settings["PE_HTTP_PORT"]), args.replace)
        if not args.dry_run:
            bootstrap.probe(dict(settings, PE_SCHEME="http"), "edge-console.invalid")
            checked(configuration["command"])
            # Normal certificate verification is deliberately retained.
            with urllib.request.urlopen(configuration["url"] + "health", timeout=15) as response:
                if response.status != 200:
                    raise ValueError("Tailscale endpoint did not return healthy")
        print(json.dumps(dict(configuration, applied=not args.dry_run,
                              scope="Edge console; application URLs still require their configured DNS and origins")))
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired, bootstrap.Refused) as error:
        print(json.dumps({"error": "tailscale_serve_failed", "detail": str(error),
                          "next": "If Serve was applied but verification failed, inspect tailscale serve status before retrying."}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
