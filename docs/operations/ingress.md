# Ingress and access modes

The Edge owns public TCP 80 and 443. Stacks join the same external network and retain their private HTTP ingresses. Replace `example.com` below with one domain shared by all three stacks.

| Hostname | Upstream Alias |
| --- | --- |
| `example.com` | `lg-gateway:80`, with local `/health` and a fallback page |
| `litellm.example.com` | `lg-gateway:80` |
| `langfuse.example.com` | `lg-gateway:80` |
| `s3.example.com` | `lg-gateway:80` |
| `backplane.example.com` | `bp-server:3000` |
| `grafana.example.com` | `ob-gateway:80` |

## Local Mode

The template uses `PE_PUBLIC_DOMAIN=localhost`, `PE_SCHEME=http`, `PE_TLS_ISSUER=none` and `PE_BIND_HOST=127.0.0.1`. Bootstrap needs no installed upstream to become ready. With no gateway the root page retains HTTP 502 and lists configured hosts and upstream availability; other absent-stack hostnames return 502.

For local sibling stacks use `localhost` as their domain and `http` as their public and listen schemes, retain issuer `none`, and give their Caddys the example spare loopback ports below. Set `BP_PUBLIC_URL=http://backplane.localhost`. Use `curl -H 'Host: grafana.localhost' http://127.0.0.1/` for probes because system resolvers may not resolve `*.localhost`.

## Public Mode and DNS

Set A and, where IPv6 is configured, AAAA records for all six names to the host. The root needs its own record even if a wildcard record covers subdomains. Every configured name must resolve correctly and reach Caddy for ACME issuance, whether or not its application stack is running. Forward TCP 80 and 443 through the host firewall and any NAT. An AAAA record must not point to an unreachable IPv6 listener.

Set these exact lines in the edge `.env`:

```sh
PE_PUBLIC_DOMAIN=example.com
PE_SCHEME=https
PE_TLS_ISSUER=acme
PE_BIND_HOST=0.0.0.0
PE_HTTP_PORT=80
PE_HTTPS_PORT=443
PE_PLATFORM_NETWORK=platform
PE_ACME_EMAIL=ops@example.com
```

Only the ACME issuer uses `PE_ACME_EMAIL`. Caddy stores and renews certificates in `edge-data`. HTTP `/health` intentionally stays available without a redirect. Healthy means Caddy answers, not that upstreams are healthy. Other HTTP requests for configured hosts redirect to HTTPS; the `:80` catch-all returns 404 for unknown hosts on every path except `/health`. For public ACME, externally reachable ports remain 80 and 443 even if NAT maps them to different `PE_HTTP_PORT` and `PE_HTTPS_PORT` values. Nonstandard direct HTTPS URLs require an explicit port in clients; the normal redirect targets port 443.

## Per-stack settings behind the edge

The sibling host ports below are examples; choose unused loopback ports on your host.

In the LLM gateway `.env`:

```sh
LG_PUBLIC_DOMAIN=example.com
LG_SCHEME=https
LG_LISTEN_SCHEME=http
LG_TLS_ISSUER=none
LG_BIND_HOST=127.0.0.1
LG_HTTP_PORT=18080
LG_HTTPS_PORT=18443
LG_PUBLIC_PORT_SUFFIX=
LG_PLATFORM_NETWORK=platform
```

In the observability stack `.env`:

```sh
OB_PUBLIC_DOMAIN=example.com
OB_SCHEME=https
OB_LISTEN_SCHEME=http
OB_TLS_ISSUER=none
OB_BIND_HOST=127.0.0.1
OB_HTTP_PORT=18180
OB_HTTPS_PORT=18543
OB_PUBLIC_PORT_SUFFIX=
OB_PLATFORM_NETWORK=platform
OB_GATEWAY_HEALTH_HOST=example.com
OB_GATEWAY_URL=https://example.com
OB_BACKPLANE_URL=https://backplane.example.com
```

`LG_LISTEN_SCHEME` and `OB_LISTEN_SCHEME` must be supported by the installed stack revisions. Their Caddys must trust forwarded headers from the private Platform Network. The public port suffix stays empty: the spare loopback port belongs to the internal ingress, while browser URLs use the Edge on 443. Each stack still publishes its spare ports to loopback for local diagnostics; only the Edge occupies public 80 and 443.

In the backplane `.env`:

```sh
BP_PUBLIC_URL=https://backplane.example.com
BP_BIND_HOST=127.0.0.1
BP_PORT=3000
```

Start its core deployment without enabling its optional `edge` profile. The backplane server must join the external `platform` network as `bp-server` (and keep its default network for datastore access). **The backplane ignores forwarded headers by design: `BP_PUBLIC_URL` must be `https://backplane.<domain>`.** The Edge forwards to `bp-server:3000`; keep the backplane `edge` profile off.

Do not attach a datastore to the Platform Network. All members of this network are trusted infrastructure.

## Rollout and diagnosis

1. Render the edge settings with `python3 scripts/bootstrap.py --render-only`, then edit `.env` for the desired mode. This writes only the env file, with mode 0600.
2. Apply the per-stack settings and recreate their Caddys to release public ports: `docker compose up -d caddy` from each stack directory. For a new stack, render its env first and perform its documented bootstrap preparation. Do not enable the backplane's edge profile.
3. Run `python3 scripts/bootstrap.py` in platform-edge. It creates the external network if missing, names a conflicting container before publishing, and starts Caddy with `docker compose up --wait`. It probes `127.0.0.1` on the selected HTTP or HTTPS port with the domain as Host. HTTPS uses that domain as SNI, validates the ACME chain or trusts the public internal CA root read from the volume, and reports the root SHA-256 fingerprint and leaf `notAfter`. It never disables TLS verification. The publish address must accept loopback connections (use `127.0.0.1` or `0.0.0.0` for these host probes).
4. Start the remaining prepared stacks. Probe their application hostnames through the Edge. A missing alias must affect only its own hostname. Edge `/health` proves only Edge readiness.

Both sibling bootstraps must probe **their own local HTTP listener**, independently of
public URLs: gateway uses `http://127.0.0.1:18080/health/<app>` and observability uses
`http://127.0.0.1:18180/health/<app>`, each with `Host: example.com`. Their HTTP listen
scheme and spare port must never rewrite the public HTTPS origin or its empty port
suffix. This is a required sibling revision, not an operator workaround. Install the
gateway and observability bootstrap fixes before accepting a shared-host rollout.

```sh
curl -fsS -H 'Host: example.com' http://127.0.0.1:18080/health/litellm
curl -fsS -H 'Host: example.com' http://127.0.0.1:18180/health/grafana
```

Inspect `docker compose logs caddy` for certificate or upstream errors. Caddy's admin API listens only on `localhost:2019` inside its container, with no published port; apply route changes with `docker compose restart caddy`. Bootstrap can be rerun safely; it allows its own existing Caddy to hold the requested ports. Unexpected host processes or a concurrent port claim cause a Compose error, reported with exit code 3. Other refusals are exit 1, bad CLI usage is exit 2, readiness is exit 0. Runtime failures and argparse usage errors are one JSON line on stderr with `error` and `detail`.

To return to standalone mode, stop the Edge, restore each stack's listen scheme, TLS issuer, public domain and chosen published ports, then recreate the stack gateway. Preserve the Edge volumes unless explicitly retiring its certificates.

## Internal CA and state

For private DNS, set `PE_SCHEME=https` and `PE_TLS_ISSUER=internal`. The stack settings remain HTTPS publicly and HTTP internally. Export the Edge root after it starts:

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
internal-CA HTTPS, using the host's loopback listener and configured Host/SNI:

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
The normal smoke uses stubs to also exercise missing siblings, forwarding, HSTS on all
six HTTPS hosts, empty uncached console probes, metrics and container hardening.
This integration does not prove external DNS, NAT or ACME issuance; verify those from
an external client as the final public rollout check.

## Metrics and certificate expiry

The root `/metrics` is restricted by the socket peer's IP using `PE_METRICS_ALLOW`
(default `127.0.0.0/8 ::1`). Add only the scraper container's Platform Network IPv4
address as a `/32` (IPv6 `/128`), for example
`PE_METRICS_ALLOW="127.0.0.0/8 ::1 172.20.0.10/32"` for a scraper at `172.20.0.10`.
Reserve that address in the scraper's Compose configuration and verify its actual
address before allowing it. Scrape `pe-edge:80` on the Platform Network with the root
Host header in HTTP mode, or the root hostname and verified TLS in HTTPS mode.
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
external TLS probes must cover all six names. HSTS is `max-age=31536000` on HTTPS
sites, with no preload or includeSubDomains. Console probes expose status and an empty
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
