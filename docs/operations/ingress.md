# Ingress and access modes

Edge owns host ports 80 and 443. Its default loopback binding makes them accessible only on that host. Stacks join the same external network and retain their private HTTP ingresses. Replace `example.com` below with one domain shared by all three stacks.

| Hostname | Upstream Alias |
| --- | --- |
| `example.com` | `lg-gateway:80`, with local `/health` and a fallback page |
| `litellm.example.com` | `lg-gateway:80` |
| `langfuse.example.com` | `lg-gateway:80` |
| `s3.example.com` | `lg-gateway:80` |
| `backplane.example.com` | `bp-server:3000` |
| `grafana.example.com` | `ob-gateway:80` |

## Access modes

Fresh installs select `PE_ACCESS_MODE=local`. The modes describe the listener independently
of the application's configured browser URL (`PE_SCHEME` and `PE_PUBLIC_DOMAIN`):

| Mode | HTTP | HTTPS | Intended use |
| --- | --- | --- | --- |
| Local (`local`) | Available without redirects | Self-signed HTTPS | Start without a domain; trust the local certificate authority to remove browser warnings |
| Public (`public`) | Redirects to HTTPS, except `/health` | Automatically renewed, publicly trusted certificate | Use your own domain |
| Behind another gateway (`proxy`) | Internal connection from that gateway | Handled by the other gateway | Run behind Platform Edge or another HTTPS gateway |

Choose the mode; certificate setup follows automatically. For local HTTPS, Caddy creates
a self-signed root certificate and uses it to sign the server certificates. The trust guide
below explains how to install that public root. Public mode obtains trusted certificates
for your domain. Behind another gateway, that gateway owns the certificates.

The template uses `PE_PUBLIC_DOMAIN=localhost`, `PE_SCHEME=http` for browser URLs, and
`PE_BIND_HOST=127.0.0.1`. Both 80 and 443 serve real listeners in local mode. Readiness
checks both protocols, verifies HTTPS using the exported public CA root, and reports its
fingerprint and leaf expiry. Browsers need that root in their trust store for direct HTTPS;
HTTP stays usable without trust setup. Binding `127.0.0.1` does not bind every loopback IP.
LAN exposure is an explicit bind-address choice. Configured application hostnames receive
local certificates; `127.0.0.1` also has an HTTPS console and health endpoint.

The HTTP console accepts arbitrary hostnames, including a Tailscale machine name.
Unknown-host `/health` is 200; other unknown paths remain 404, including `/metrics`.
Application routes still require their configured hostnames. Console links use the
configured application domain instead of inventing subdomains under an IP or Tailscale name.
The configured root hostname still proxies to the gateway and retains the intentional 502
fallback if that gateway is missing. Bootstrap does not require any sibling stack.

For local sibling stacks behind Edge, choose their proxy access mode, keep internal HTTP,
and configure the configured browser URL for the address customers will use. Applications
with authentication or generated links still need one configured application URL even though Edge
accepts both HTTP and HTTPS. Give sibling Caddys spare loopback HTTP ports. Keep the
backplane's optional standalone edge profile off and set its explicit `BP_PUBLIC_URL`.

## Tailscale console sharing

Tailscale Serve is optional and owns browser-facing HTTPS; it forwards to Edge's local
HTTP listener. It does not require clients to trust Caddy's internal CA. After starting Edge
in `local` mode, review and apply the helper:

```sh
python3 scripts/tailscale_serve.py --https-port 8443 --dry-run
sudo python3 scripts/tailscale_serve.py --https-port 8443
```

Use `--env-file /absolute/path/.env` for another installation. `sudo` is only needed if
Tailscale permissions require it. Login, MagicDNS and tailnet HTTPS approval must be
completed first; the Tailscale CLI provides the approval link when necessary. The helper
checks readiness, preserves other endpoints, refuses collisions unless `--replace` is
explicit, verifies the resulting HTTPS health URL, and prints the URL and undo command.
It never enables Funnel or modifies the host's trust stores. A failed verification can leave
the requested Serve mapping applied; inspect `tailscale serve status` before retrying.

For the default external HTTPS port, the underlying command is:

```sh
sudo tailscale serve --bg --https=443 http://127.0.0.1:80
```

Select another free external port if 443 is already in use. Never target local HTTP port 443,
or accidentally configure HTTPS on external port 80 while expecting plain HTTP clients.
A Serve URL provides console access, not automatic application subdomains. Full-stack
access retains configured domains and origins. A public deployment can use those same
names over Tailscale with appropriate private DNS, routing and certificate reachability.

For `PE_ACCESS_MODE=proxy`, bootstrap automatically selects `compose.proxy.yaml` to
publish only HTTP. Direct Compose commands must use both files:

```sh
docker compose -f compose.yaml -f compose.proxy.yaml config
```

This requires Compose 2.24.4+. Behind another gateway, browser URLs default to HTTPS; set `PE_SCHEME=http` only for an HTTP origin.
Edge forwards that configured scheme, not an arbitrary incoming forwarded header. Keep
this HTTP ingress bound to loopback or a trusted ingress network. Real client-IP trust is
not enabled automatically and metrics authorization remains based on socket peers.

## Public HTTPS with your own domain

Set A and, where IPv6 is configured, AAAA records for all six names to the host. The root needs its own record even if a wildcard record covers subdomains. Every configured name must resolve correctly and reach Caddy to obtain trusted certificates, whether or not its application stack is running. Forward TCP 80 and 443 through the host firewall and any NAT. An AAAA record must not point to an unreachable IPv6 listener.

Set these exact lines in the edge `.env`:

```sh
PE_ACCESS_MODE=public
PE_PUBLIC_DOMAIN=example.com
PE_SCHEME=https
PE_BIND_HOST=0.0.0.0
PE_HTTP_PORT=80
PE_HTTPS_PORT=443
PE_PLATFORM_NETWORK=platform
PE_ACME_EMAIL=ops@example.com
```

Bootstrap selects `compose.public.yaml` in public mode so an empty `PE_SCHEME` uses HTTPS. When running Compose directly, include both `-f compose.yaml -f compose.public.yaml`.

Only public mode uses `PE_ACME_EMAIL`. Caddy stores and renews certificates in `edge-data`. HTTP `/health` intentionally stays available without a redirect. Healthy means Caddy answers, not that upstreams are healthy. Other HTTP requests for configured hosts redirect to HTTPS; the `:80` catch-all returns 404 for unknown hosts on every path except `/health`. For public certificates, externally reachable ports remain 80 and 443 even if NAT maps them to different `PE_HTTP_PORT` and `PE_HTTPS_PORT` values. Nonstandard direct HTTPS URLs require an explicit port in clients; the normal redirect targets port 443.

## Per-stack settings behind the edge

The sibling host ports below are examples; choose unused loopback ports on your host.
Start Edge first so its network exists. Reserve a free address on that network for Edge
using a local `compose.override.yaml`, then run its bootstrap again. For example, if the
network has subnet `172.30.0.0/24` and `172.30.0.10` is reserved and unused:

```yaml
services:
  caddy:
    networks:
      platform:
        ipv4_address: 172.30.0.10
```

Use the subnet and a reserved address from your own Docker network, not this example.
Verify the assigned address before configuring the sibling trust lists:

```sh
docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$(docker compose ps -q caddy)"
```

Replace `192.0.2.2/32` below with that address followed by `/32`. The trust list lets the
stack accept the browser's protocol and client address from Edge. It does not grant
operator access to every request through Edge.

In the LLM gateway `.env`:

```sh
LG_ACCESS_MODE=proxy
LG_PUBLIC_DOMAIN=example.com
LG_SCHEME=https
LG_BIND_HOST=127.0.0.1
LG_HTTP_PORT=18080
LG_PUBLIC_PORT_SUFFIX=
LG_PLATFORM_NETWORK=platform
LG_TRUSTED_PROXIES=192.0.2.2/32
```

In the observability stack `.env`:

```sh
OB_ACCESS_MODE=proxy
OB_PUBLIC_DOMAIN=example.com
OB_SCHEME=https
OB_BIND_HOST=127.0.0.1
OB_HTTP_PORT=18180
OB_PUBLIC_PORT_SUFFIX=
OB_PLATFORM_NETWORK=platform
OB_TRUSTED_PROXIES=192.0.2.2/32
OB_GATEWAY_HEALTH_HOST=example.com
OB_GATEWAY_URL=https://example.com
OB_BACKPLANE_URL=https://backplane.example.com
```

Replace `192.0.2.2/32` with Edge’s actual address on the shared Docker network. Reserve that address in a local Compose override so recreating Edge does not change it. Trust only that address, not the entire network. The public port suffix stays empty: the spare loopback HTTP port is for local diagnostics, while browsers use Edge on 443. Behind another gateway, stacks publish only their HTTP port.

In the backplane `.env`:

```sh
BP_ACCESS_MODE=proxy
BP_PUBLIC_URL=https://backplane.example.com
BP_BIND_HOST=127.0.0.1
BP_PORT=3000
```

Start its core deployment without enabling its optional `edge` profile. The backplane server must join the external `platform` network as `bp-server` (and keep its default network for datastore access). **The backplane ignores forwarded headers by design: `BP_PUBLIC_URL` must be `https://backplane.<domain>`.** The Edge forwards to `bp-server:3000`; keep the backplane `edge` profile off.

Do not attach a datastore to the Platform Network. All members of this network are trusted infrastructure.

## Rollout and diagnosis

1. Render the edge settings with `python3 scripts/bootstrap.py --render-only`, then edit `.env` for the desired mode. This writes only the env file, with mode 0600.
2. If an installed stack already owns ports 80 or 443, give its gateway spare loopback ports and run its bootstrap to release those ports. Keep its current access mode until Edge’s reserved address is known. For a new stack, prepare its env using its documented bootstrap. Keep the backplane’s optional `edge` profile off.
3. Run `python3 scripts/bootstrap.py` in platform-edge. It creates the external network if missing, names a conflicting container before publishing, and starts Caddy with `docker compose up --wait`. It probes `127.0.0.1` on the selected HTTP or HTTPS port with the domain as Host. HTTPS uses that domain as SNI, validates the public certificate or trusts the installation’s self-signed root certificate read from the volume, and reports the root SHA-256 fingerprint and leaf `notAfter`. It never disables TLS verification. The publish address must accept loopback connections (use `127.0.0.1` or `0.0.0.0` for these host probes).
4. Apply the per-stack settings above, including Edge’s reserved address in the trust lists. Run each stack’s bootstrap so it selects the matching Compose files and starts its services. Probe application hostnames through Edge. A missing stack must affect only its own hostnames. Edge `/health` proves only Edge readiness.

Both sibling bootstraps must probe **their own local HTTP listener**, independently of
public URLs: gateway uses `http://127.0.0.1:18080/health/<app>` and observability uses
`http://127.0.0.1:18180/health/<app>`, each with `Host: example.com`. Their internal HTTP ports must not appear in browser URLs. Use the current gateway
and observability revisions, which select these probes automatically.

```sh
curl -fsS -H 'Host: example.com' http://127.0.0.1:18080/health/litellm
curl -fsS -H 'Host: example.com' http://127.0.0.1:18180/health/grafana
```

Inspect `docker compose logs caddy` for certificate or upstream errors. Caddy's admin API listens only on `localhost:2019` inside its container, with no published port; apply route changes with `docker compose restart caddy`. Bootstrap can be rerun safely; it allows its own existing Caddy to hold the requested ports. Unexpected host processes or a concurrent port claim cause a Compose error, reported with exit code 3. Other refusals are exit 1, bad CLI usage is exit 2, readiness is exit 0. Runtime failures and argparse usage errors are one JSON line on stderr with `error` and `detail`.

To run a stack on its own again, select its local or public access mode, set its domain and free host ports, then run its bootstrap. Stop Edge first if you want to reuse ports 80 and 443. Preserve the Edge volumes unless explicitly retiring its certificates.

## Trusting local HTTPS certificates

For private DNS, use `PE_ACCESS_MODE=local`, your configured domain, and `PE_SCHEME=https` if HTTPS is the configured application URL. The stack settings remain HTTPS publicly and HTTP internally. Export the Edge root after it starts:

```sh
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./platform-edge-root.crt
```

Install this public root certificate into each client's trust store. Keep certificate verification enabled. Back up the `edge-data` and `edge-config` volumes securely before upgrades. Internal CA private keys stay in `edge-data`; export only `root.crt` to clients. Losing this volume replaces the internal CA and requires redistributing trust. Apply new image pins only after validation and smoke pass, and preserve volumes when rolling back the image.

## Shared-host acceptance

After all sibling bootstraps pass locally, run:

```sh
SMOKE_INTEGRATION=1 SMOKE_DOMAIN=example.com PE_PLATFORM_NETWORK=platform scripts/smoke.sh
```

This requires running Compose siblings with `lg-gateway`, `ob-gateway` and `bp-server`
on that network. It prints `SKIP` with the missing aliases/network and zero checked
routes when a sibling is absent; a skip is **not acceptance**. The test starts its own
edge on spare loopback ports, uses its project name as its disposable volume prefix,
and leaves sibling containers and volumes alone. Compose 2.24.4+ is required for the
integration override that prevents shadowing the installed `pe-edge` alias. Set
`SMOKE_HTTP_PORT`, `SMOKE_HTTPS_PORT` or `SMOKE_PROJECT` to avoid existing resources.
`SMOKE_DOMAIN` must equal the siblings' configured public domain (default `localhost`);
these controls are explicit shell settings, not inferred from the installed `.env`.

Six real application endpoints must return 200, first over HTTP and then verified
self-signed HTTPS, using the host's loopback listener and configured Host/SNI:

| Host | Health endpoint |
| --- | --- |
| root | `/health/litellm` (real LiteLLM, not Edge liveness) |
| litellm | `/health/liveliness` |
| langfuse | `/api/public/health` |
| s3 | `/health/ready` |
| backplane | `/health/ready` |
| grafana | `/api/health` |

LiteLLM and Langfuse probes require HTTP 200 and empty public response bodies.
Backplane and Grafana probes validate their JSON health responses.
The S3 route uses RustFS's [documented health endpoints](https://docs.rustfs.com/en/operations/status-check).
The normal smoke uses stubs to also exercise missing siblings, forwarding, local HTTPS without HSTS on all
six hosts, empty uncached console probes, metrics and container hardening.
This integration does not prove external DNS, port forwarding or public certificate issuance; verify those from
an external client as the final public rollout check.

## Metrics and certificate expiry

The root `/metrics` is restricted by the socket peer's IP using `PE_METRICS_ALLOW`
(default `127.0.0.0/8 ::1`). Add only the scraper container's Platform Network IPv4
address as a `/32` (IPv6 `/128`), for example
`PE_METRICS_ALLOW="127.0.0.0/8 ::1 172.20.0.10/32"` for a scraper at `172.20.0.10`.
Reserve that address in the scraper's Compose configuration and verify its actual
address before allowing it. Scrape `http://pe-edge:80/metrics` on the Platform Network in every access mode.
This explicit internal hostname serves metrics only and uses the same socket-peer
allowlist as the application root metrics route. Set `OB_SCRAPE_EDGE=true` in observability
after reserving and allowing its scraper IP; leave it disabled when Edge is absent.
Never allow the whole subnet or its Docker bridge gateway. Published-port connections
relayed by Docker can all appear as that gateway, including remote clients under
rootless Docker or IPv6-to-IPv4 proxying. Allowing it can make metrics public. A host
probe seen as the gateway must stay denied; use the scraper's network path instead.
The outermost Edge does not trust forwarded client IP headers; they cannot grant access.
In a cloud VPC, "private" means every tenant, so private address ranges are not an access policy.
It combines native Caddy metrics (`/metrics/caddy`, under the same restriction) with
`pe_certificate_not_after_seconds` and `pe_certificate_checked_seconds` from the
bootstrap-generated `/config/pe-certificate.prom` textfile. Admin stays container-local.
[Caddy metrics](https://caddyserver.com/docs/metrics) do not expose leaf certificate
expiry; the combined response uses [Caddy templates](https://caddyserver.com/docs/caddyfile/directives/templates).

Run `python3 scripts/bootstrap.py --probe-only` every five minutes to refresh the root
leaf observation after automatic renewal, as shown in [backup](backup.md). Failure
leaves the last successful value; alert on expiry, stale/absent observations and scrape
failure. The root's expiry does not cover independently issued subdomain certificates:
external TLS probes must cover all six names. HSTS is `max-age=31536000` in public mode, with no preload or includeSubDomains. Local mode deliberately has no HSTS. Console probes expose status and an empty
body, with `Cache-Control: no-store`, including failed upstream connections.

See [backup and restore](backup.md) before adopting the external volume names or changing
image pins. `down -v` is not a safe retirement workflow for old revisions.

## Checkpoint settings

`PE_BACKUP_DIR` selects the protected backup repository. Relative paths resolve against
this checkout; the default is `./backups`. The directory is created if absent. Verify
an expected mount before capture. `PE_BACKUP_KEEP` defaults to `7`, has a minimum of `1`,
and retains that many complete Checkpoints after a successful capture and resumption.
Incomplete sets and diagnostics are retained for operator inspection. See the
[backup procedure](backup.md) for encryption, replication and recovery.

## Runtime logs

Caddy emits JSON access logs on stdout and runtime/error diagnostics on stderr. Docker
uses `journald` with `cache-disabled=true`: there are no application-managed or Docker
JSON/cache log files. Journal persistence and retention remain the host's policy; bootstrap
never changes it. This default requires a Docker host with journald. On a non-systemd host,
choose a supported Docker logging driver via an operator-owned Compose override and
review its storage behavior before starting the stack.

```sh
docker compose logs --tail=100 -f caddy
journalctl CONTAINER_NAME=platform-edge-caddy-1
```

The observability stack is optional. Its Alloy Docker discovery and `loki.source.docker`
reader can collect this container through Docker's journal reader, labelling it with
`compose_project=platform-edge` and `service=caddy`. Loki's retained data is observability
product state, not a second local runtime log file. Without Alloy, Caddy continues running
and the host journal remains available. Host `tailscaled` messages are not container logs;
inspect them with `journalctl -u tailscaled` or configure a separate journal pipeline.

Caddy removes request and response headers and query strings from access logs and
request-bearing error diagnostics. Do not put credentials in URL paths. URLs and request
IDs remain log fields, not Loki index labels.
