"""Tailnet Origins: one Tailscale node per routed hostname, started by `bootstrap.py --tailscale`.

Bootstrap starts the selected sidecars of `compose.tailscale.yaml`, waits until each reports
Running under its expected MagicDNS name, records the tailnet domain once in `.env`, then probes
every origin over HTTPS from this host. The auth key is read only to check that it is set.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import time

import bootstrap

OVERLAY = "compose.tailscale.yaml"
# Node names; `console` is the root site and takes PE_ROOT_HOST as its name.
APPS = ("console", "litellm", "langfuse", "s3", "rustfs", "backplane", "grafana")
DOMAIN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net")
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
ENROLL_TIMEOUT = 120.0
# The first TLS handshake of a node blocks while Tailscale obtains its certificate.
PROBE_TIMEOUT = 180.0


def add_arguments(parser) -> None:
    parser.add_argument("--tailscale", action="store_true",
                        help="start one Tailscale node per hostname in PE_TS_APPS, record PE_TAILNET_DOMAIN and probe the origins")


def check_settings(settings: dict[str, str]) -> None:
    """Formats of the Tailnet settings; they apply whether or not --tailscale is given."""
    if settings["PE_TAILNET_DOMAIN"] and not DOMAIN.fullmatch(settings["PE_TAILNET_DOMAIN"]):
        raise bootstrap.Refused("invalid_settings", "PE_TAILNET_DOMAIN must be the tailnet's MagicDNS domain, such as tail1234.ts.net")
    if settings["PE_TS_TAG"] and not re.fullmatch(r"tag:[a-zA-Z0-9-]+", settings["PE_TS_TAG"]):
        raise bootstrap.Refused("invalid_settings", "PE_TS_TAG must be a Tailscale tag such as tag:platform, or empty")
    root = settings["PE_ROOT_HOST"]
    if root and (not LABEL.fullmatch(root) or root in APPS[1:]):
        raise bootstrap.Refused("invalid_settings", "PE_ROOT_HOST must be one DNS label that is not an application name")
    unknown = [name for name in settings["PE_TS_APPS"].split(",") if name not in APPS]
    if unknown:
        raise bootstrap.Refused("invalid_settings", f"PE_TS_APPS names unknown nodes {', '.join(unknown)}; choose from {', '.join(APPS)}")


def check_ready(settings: dict[str, str]) -> None:
    """What --tailscale needs before anything starts."""
    if not settings["PE_TS_AUTHKEY"]:
        raise bootstrap.Refused("tailscale_auth_key", "set PE_TS_AUTHKEY in .env to a reusable, non-ephemeral, tagged auth key; "
                                "see docs/operations/ingress.md#access-everything-through-tailscale")
    # A public bind would serve the plain-HTTP Tailnet hosts to anyone who forges the Host header.
    if settings["PE_ACCESS_MODE"] not in {"local", "proxy"} or settings["PE_BIND_HOST"] != "127.0.0.1":
        raise bootstrap.Refused("invalid_settings", "--tailscale needs local or proxy mode with PE_BIND_HOST=127.0.0.1")


def selected(settings: dict[str, str]) -> tuple[str, ...]:
    return tuple(app for app in APPS if app in settings["PE_TS_APPS"].split(","))


def node_name(app: str, settings: dict[str, str]) -> str:
    return (settings["PE_ROOT_HOST"] or "platform") if app == "console" else app


def origins(settings: dict[str, str], apps: tuple[str, ...]) -> dict[str, str]:
    """Browser origin per selected node; the domain placeholder stands in until enrollment records it."""
    domain = settings["PE_TAILNET_DOMAIN"] or "<PE_TAILNET_DOMAIN>"
    return {app: f"https://{node_name(app, settings)}.{domain}" for app in apps}


def volume_names(settings: dict[str, str], apps: tuple[str, ...]) -> list[str]:
    return [f"{settings['PE_VOLUME_PREFIX']}_ts-{app}" for app in apps]


def enrolled(output: str, name: str) -> str | None:
    """The tailnet domain of a Running node enrolled as `name`, or None while it is still enrolling."""
    try:
        status = json.loads(output)
        state = status["BackendState"]
        dns_name = (status.get("Self") or {}).get("DNSName") or ""
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    label, _, domain = dns_name.rstrip(".").lower().partition(".")
    if state != "Running" or not domain:
        return None
    if label != name:
        raise bootstrap.Refused("tailscale_name_taken",
                                f"the node for {name} enrolled as {dns_name.rstrip('.')}: another machine on the tailnet already holds "
                                f"that name. Remove or rename it in the Tailscale admin console, remove this node too, "
                                f"then rerun bootstrap --tailscale")
    return domain


def wait_enrolled(runner, dc: list[str], settings: dict[str, str], apps: tuple[str, ...],
                  timeout: float = ENROLL_TIMEOUT) -> str:
    """Wait until every selected node runs under its expected name; all must share one tailnet."""
    deadline = time.monotonic() + timeout
    domains: dict[str, str] = {}
    pending = list(apps)
    while pending:
        for app in list(pending):
            result = runner(dc + ["exec", "-T", f"ts-{app}", "tailscale", "status", "--json"])
            domain = enrolled(result.stdout, node_name(app, settings)) if result.returncode == 0 else None
            if domain:
                domains[app] = domain
                pending.remove(app)
        if pending:
            if time.monotonic() >= deadline:
                raise bootstrap.Refused("not_ready", f"Tailscale nodes {', '.join('ts-' + app for app in pending)} did not reach "
                                        "Running within the deadline; check `docker compose -f compose.yaml -f "
                                        f"{OVERLAY} --profile ts-{pending[0]} logs ts-{pending[0]}` for an expired or "
                                        "untagged auth key")
            time.sleep(3)
    if len(set(domains.values())) != 1:
        raise bootstrap.Refused("tailscale_domain_mismatch", "the nodes enrolled in different tailnets: "
                                + ", ".join(f"ts-{app}={domain}" for app, domain in sorted(domains.items())))
    domain = next(iter(domains.values()))
    recorded = settings["PE_TAILNET_DOMAIN"]
    if recorded and recorded != domain:
        raise bootstrap.Refused("tailscale_domain_mismatch", f"PE_TAILNET_DOMAIN={recorded} but the nodes enrolled in {domain}; "
                                "clear the setting (and any sibling origins derived from it) to move Edge to this tailnet")
    return domain


def probe(host: str, timeout: float) -> int:
    """HTTP status of https://<host>/health, verified against the system trust store."""
    connection = http.client.HTTPSConnection(host, 443, timeout=timeout, context=ssl.create_default_context())
    try:
        connection.request("GET", "/health", headers={"Host": host})
        return connection.getresponse().status
    finally:
        connection.close()


def probe_origins(urls: dict[str, str], timeout: float = PROBE_TIMEOUT) -> dict[str, int] | str:
    """Status per origin, or the reason the probe was skipped when this host is not on the tailnet."""
    hosts = {app: url.removeprefix("https://") for app, url in urls.items()}
    try:
        socket.getaddrinfo(next(iter(hosts.values())), 443)
    except OSError:
        return "skipped: this host does not resolve the tailnet names; verify the origins from a tailnet member"
    deadline = time.monotonic() + timeout
    statuses: dict[str, int] = {}
    last = ""
    for app, host in hosts.items():
        while True:
            try:
                statuses[app] = probe(host, min(30.0, max(1.0, deadline - time.monotonic())))
                break
            except (OSError, http.client.HTTPException) as error:
                last = f"{host}: {error}"
            if time.monotonic() >= deadline:
                raise bootstrap.Refused("not_ready", last + "; the tailnet policy must let this host reach the nodes' tag on port 443")
            time.sleep(3)
    return statuses
