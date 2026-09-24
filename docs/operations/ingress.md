# Ingress and access modes

How Edge is reached and how the sibling stacks sit behind it: hostnames, the three access
modes, certificates, the bundle settings each stack receives, metrics, logs and the checks
that prove a shared host works. Replace `example.com` below with the one domain shared by
every stack.

- [Hostnames and modes](#hostnames-and-modes)
- [Local Mode (default)](#local-mode-default)
- [Public Mode](#public-mode)
- [Proxy Mode](#proxy-mode)
- [Corporate certificates and private ACME](#corporate-certificates-and-private-acme)
- [Access everything through Tailscale](#access-everything-through-tailscale)
- [Install the bundle](#install-the-bundle)
- [Bundle settings per stack](#bundle-settings-per-stack)
- [Trusting local HTTPS certificates](#trusting-local-https-certificates)
- [Image overrides](#image-overrides)
- [Metrics and certificate expiry](#metrics-and-certificate-expiry)
- [Runtime logs](#runtime-logs)
- [Status routes](#status-routes)
- [Shared-host acceptance](#shared-host-acceptance)
- [Troubleshooting](#troubleshooting)

## Hostnames and modes

Edge owns host ports 80 and 443. Its default loopback binding makes them reachable only on
that host. Stacks join the same external network and keep their private HTTP ingresses.

| Hostname | Upstream Alias |
| --- | --- |
| `example.com` | Edge console at `/`; other paths go to `lg-gateway:80` |
| `litellm.example.com` | `lg-gateway:80` |
| `langfuse.example.com` | `lg-gateway:80` |
| `s3.example.com` | `lg-gateway:80` |
| `rustfs.example.com` | `lg-gateway:80` (admin console) |
| `backplane.example.com` | `bp-server:3000` |
| `grafana.example.com` | `ob-gateway:80` |

All seven hostnames are configured even when some stacks are absent; a missing stack
returns 502 on its own hostnames and nothing else changes. `PE_ACCESS_MODE` selects the
listeners; `PE_SCHEME` and `PE_PUBLIC_DOMAIN` form the browser URLs the siblings receive:

| Mode | HTTP | HTTPS | Issuer (`PE_TLS_ISSUER`) | Use |
| --- | --- | --- | --- | --- |
| Local (`local`) | Served without redirects | Self-signed, on loopback | `internal` (default) or `files` | Start without a domain; install the local CA root to remove browser warnings |
| Public (`public`) | Redirects to HTTPS, except `/health` | Trusted certificate for your domain | `acme` (default; Let's Encrypt or `PE_ACME_CA`) or `files` | Your own domain |
| Proxy (`proxy`) | From the other gateway | Handled by the other gateway | Unused | Behind another HTTPS gateway |

Bootstrap selects `compose.public.yaml` or `compose.proxy.yaml` for those modes, and the
issuer's overlays after it. Direct Compose commands must list the same files, for example
`docker compose -f compose.yaml -f compose.public.yaml config`; Compose 2.24.4 or newer is
required. Bootstrap refuses an issuer the mode cannot use.

## Local Mode (default)

The template uses `PE_ACCESS_MODE=local`, `PE_PUBLIC_DOMAIN=localhost`, an empty
`PE_SCHEME` (HTTP browser URLs) and `PE_BIND_HOST=127.0.0.1`. Both 80 and 443 serve real
listeners. Readiness checks both protocols, verifies HTTPS with the exported public CA root
and reports its fingerprint and leaf expiry. Browsers need that root in their trust store
for direct HTTPS ([trusting local HTTPS certificates](#trusting-local-https-certificates));
HTTP works without it. Binding `127.0.0.1` does not bind every loopback IP, and LAN
exposure is an explicit bind-address choice.

The HTTP console accepts arbitrary hostnames: unknown-host `/health` is 200, other unknown
paths are 404, including `/metrics`. Application routes require their configured
hostnames. Configured hostnames receive local certificates; `127.0.0.1` also has an HTTPS
console and health endpoint. Console links use the Tailnet Origins of the selected nodes
once a tailnet is recorded, and the configured domain otherwise.

For private DNS, keep `local`, set your domain and `PE_SCHEME=https`, and distribute the
public CA root. The siblings then use HTTPS browser URLs and HTTP behind Edge.

## Public Mode

Set A and, where IPv6 is configured, AAAA records for all seven names to the host. The
root needs its own record even if a wildcard covers the subdomains. Every name must
resolve and reach Caddy to obtain a certificate, whether or not its stack runs. Forward
TCP 80 and 443 through the host firewall and any NAT; an AAAA record must not point to an
unreachable IPv6 listener. Then set in the Edge `.env`:

```sh
PE_ACCESS_MODE=public
PE_PUBLIC_DOMAIN=example.com
PE_SCHEME=https
PE_BIND_HOST=0.0.0.0
PE_HTTP_PORT=80
PE_HTTPS_PORT=443
PE_ACME_EMAIL=ops@example.com
```

Run the [bundle](#install-the-bundle) again so the siblings receive the HTTPS origins. Only
the `acme` issuer uses `PE_ACME_EMAIL`. Caddy stores and renews certificates in
`edge-data`. HTTP `/health` stays available without a redirect; healthy means Caddy
answers, not that upstreams are healthy. Other HTTP requests for configured hosts redirect
to HTTPS; the `:80` catch-all returns 404 for unknown hosts on every path except `/health`.
HSTS is `max-age=31536000` without preload or `includeSubDomains`. For public
certificates, the externally reachable ports remain 80 and 443 even if NAT maps them to
other `PE_HTTP_PORT` and `PE_HTTPS_PORT` values; nonstandard direct HTTPS URLs need an
explicit port in clients, and the redirect targets port 443.

## Proxy Mode

`PE_ACCESS_MODE=proxy` publishes only HTTP (`compose.proxy.yaml`) for another gateway that
handles HTTPS in front of Edge. Browser URLs default to HTTPS; set `PE_SCHEME=http` only
for an HTTP origin. Edge forwards that configured scheme to the stacks, never an incoming
forwarded header. Keep the listener bound to loopback or a trusted ingress network. Client
addresses are not trusted automatically (`PE_TRUSTED_PROXIES` lists exact peers), and
metrics authorization stays based on socket peers.

## Corporate certificates and private ACME

`PE_TLS_ISSUER` selects where certificates come from, independently of the access mode:
`internal` (Edge's own CA, Local Mode), `acme` (Public Mode) or `files` (Local or Public
Mode). Proxy Mode ignores it. Relative `PE_TLS_DIR`, `PE_TLS_CA` and `PE_ACME_CA_ROOT`
paths resolve against this checkout.

### Private or alternative ACME CA

Set `PE_ACME_CA` to the CA's ACME directory URL (`https://`). For a CA whose chain is not
in the public trust stores (step-ca, an ACME-enabled corporate CA), set `PE_ACME_CA_ROOT`
to its CA certificate in PEM form: Caddy trusts it when talking to the ACME server, and
bootstrap's readiness probe trusts it for the issued server certificates. A CA that
requires external account binding takes `PE_ACME_EAB_KEY_ID` and `PE_ACME_EAB_HMAC`,
always together.

```sh
PE_ACCESS_MODE=public
PE_PUBLIC_DOMAIN=example.internal
PE_TLS_ISSUER=acme
PE_ACME_EMAIL=ops@example.internal
PE_ACME_CA=https://ca.example.internal/acme/acme/directory
PE_ACME_CA_ROOT=/etc/ssl/corp/root_ca.crt
PE_ACME_EAB_KEY_ID=
PE_ACME_EAB_HMAC=
```

Bootstrap selects `compose.acme-ca-root.yaml` (mounts the trust file read-only at
`/certs/acme-ca-root.crt`) and `compose.acme-eab.yaml` when those settings are set; direct
Compose commands list the same files after `compose.public.yaml`. Only the HTTP-01 and
TLS-ALPN-01 challenges are available: the ACME server must reach the host on TCP 80 and
443 for every configured name, and DNS-01 is not offered. Account keys and issued
certificates stay in `edge-data`.

### Certificate and key files

Put the server certificate chain in `tls.crt` and its unencrypted private key in `tls.key`
inside one directory:

```sh
PE_TLS_ISSUER=files
PE_TLS_DIR=/etc/ssl/platform-edge
PE_TLS_CA=/etc/ssl/corp/root_ca.crt
```

The certificate must cover every configured hostname, the root domain and the six
subdomains, by name or by a one-label wildcard (`*.example.com` covers
`grafana.example.com`, not `example.com`). Bootstrap reads the subject alternative names
with `openssl` and refuses a certificate that leaves a hostname uncovered. `PE_TLS_CA` is
the issuing CA in PEM form for bootstrap's readiness probe; leave it empty when that CA is
in the host's trust store. Bootstrap selects `compose.files.yaml`, which mounts `PE_TLS_DIR`
read-only at `/certs` and never creates it; direct Compose commands add `-f compose.files.yaml`.

Caddy runs as uid 0 with every capability dropped, so it reads the files by permission
bits: own `tls.key` by root with mode 0600, and keep `tls.crt` and the CA file readable.
Bootstrap checks this from a throwaway container before starting and refuses with
`tls_files_unreadable`. In Local Mode, `127.0.0.1` keeps its internal-CA certificate for
the console and health endpoint; application hostnames use the files.

Replace a certificate by writing the new pair into the directory, then reload:

```sh
docker compose exec caddy caddy reload --force --config /etc/caddy/Caddyfile
python3 scripts/bootstrap.py --probe-only
```

`--force` matters: the configuration is unchanged, and without it Caddy skips the reload
and keeps serving the old certificate. Edge does not watch the directory or renew file
certificates, so alert on `pe_certificate_not_after_seconds`, which `--probe-only`
refreshes after verifying the handshake. File certificates are outside the Checkpoint
volumes; back them up with your PKI.

## Access everything through Tailscale

Private access from your own devices without exposing anything to the internet: one
Tailscale node per hostname inside the Edge project, each serving
`https://<name>.<tailnet>.ts.net`. Setup, day two and troubleshooting are in the
[Tailscale runbook](tailscale.md); the design is [ADR-0004](../adr/0004-tailscale-sidecars.md).

## Install the bundle

One command installs or reconfigures the sibling stacks behind Edge. Run it from the Edge
checkout after choosing the Edge settings; Edge bootstraps first, then each selected stack:

```sh
python3 scripts/bootstrap.py --with gateway --with observability --with backplane \
  --capability-file /home/operator/private/backplane-enrollment
```

Checkouts default to the sibling directories `../llm-gateway-stack`, `../observability-stack`
and `../agent-backplane`; `--gateway-dir`, `--observability-dir` and `--backplane-dir` point
elsewhere. `--capability-file` is required with `--with backplane`. A missing checkout or
`.env.example`, exported shell settings of a selected stack (`LG_`, `OB_`, `BP_`) or any
`COMPOSE_` setting are refused (exit 1) before anything is written.

For each stack, in the order gateway, observability, backplane, bootstrap copies
`.env.example` to `.env` (mode 0600) when it is absent, takes the stack's own env lock,
writes the [bundle settings](#bundle-settings-per-stack) with an atomic replacement that
keeps every other line byte for byte, releases the lock and runs the stack's bootstrap from
its checkout: `python3 scripts/bootstrap.py`, for Backplane with `--capability-file PATH
--access-mode proxy --public-url URL`. Later runs reuse Backplane's recorded selection; a
Backplane `.env` that holds core secrets or `COMPOSE_FILE` but no `COMPOSE_PROFILES` is
refused (`bundle_backplane_selection_required`) until its bootstrap has run once with
`--profile` for each existing profile, or `--profile ''` for core only, and a recorded
`gateway` profile from before Backplane removed its internal gateway is refused
(`bundle_gateway_profile_retired`) until it is dropped. The sibling's output goes to the
terminal; env contents are never printed. With observability selected, the gateway also
receives `LG_METRICS=true` so its datastore exporters run for Observability to scrape.
Secrets and every other setting stay the stack's own: set `LG_BACKUP_DIR`,
`LANGFUSE_INIT_USER_EMAIL`, `BP_BACKUP_DIR` and the Observability alert destination in
those `.env` files yourself.

Reruns are idempotent: a key that already holds its value is not rewritten, and the stack
bootstraps run again. `--dry-run` validates the Edge settings, renders its Compose
configuration, prints one JSON line per selected stack with the checkout, the keys it would
write and the command it would run, and writes nothing.

The first failing stack bootstrap stops the run with exit 3 and a JSON error naming the
stack, its exit code and the rerun command; earlier stacks stay ready and later stacks are
untouched. Fix the reported problem and rerun the same command. Backplane enrollment
(`bp bootstrap`) remains the separate step its bootstrap prints.

## Bundle settings per stack

`--with` writes the settings below; this table is the reference for setting them by hand.
The values come from Edge: `PE_PUBLIC_DOMAIN`, `PE_SCHEME` (HTTPS when unset),
`PE_PLATFORM_NETWORK`, `PE_PLATFORM_SUBNET`, `PE_PLATFORM_IP_RANGE` and `PE_EDGE_IP` as the
`/32` trust entry. The ports are the Platform Contract's bundle ports; the public port
suffix stays empty because browsers use Edge on 443 and the loopback HTTP port is for
local diagnostics only.

The Platform Network has one allocation, defined by the
[platform contract](../conventions.md#platform-contract): whichever bootstrap runs first
creates `platform` with `--subnet 172.30.0.0/24 --ip-range 172.30.0.128/25 --gateway
172.30.0.1` (`PE_PLATFORM_SUBNET`, `PE_PLATFORM_IP_RANGE`; the gateway is derived), and
every bootstrap refuses an existing network whose subnet or ip-range differs
(`platform_network_mismatch`, see [troubleshooting](#network-cutover)). Edge always holds
`PE_EDGE_IP` (`172.30.0.2`), a fixed `ipv4_address` outside the dynamic range, so the
siblings trust that one address and nothing is discovered. Change the three settings
together, in every stack, only when the default subnet collides with your host's routing.

In the LLM gateway `.env`:

```sh
LG_ACCESS_MODE=proxy
LG_PUBLIC_DOMAIN=example.com
LG_SCHEME=https
LG_BIND_HOST=127.0.0.1
LG_HTTP_PORT=18080
LG_PUBLIC_PORT_SUFFIX=
LG_PLATFORM_NETWORK=platform
LG_PLATFORM_SUBNET=172.30.0.0/24
LG_PLATFORM_IP_RANGE=172.30.0.128/25
LG_TRUSTED_PROXIES=172.30.0.2/32
LG_METRICS=true
```

`LG_METRICS=true` is written only when observability is also selected. The five browser
origin keys (`LG_CONSOLE_URL`, `LG_LITELLM_URL`, `LG_LANGFUSE_URL`, `LG_S3_URL`,
`LG_RUSTFS_URL`) are written empty, so the gateway derives them from its public domain,
or with the Tailnet Origins when a tailnet selection is recorded.

In the observability stack `.env`:

```sh
OB_ACCESS_MODE=proxy
OB_PUBLIC_DOMAIN=example.com
OB_SCHEME=https
OB_BIND_HOST=127.0.0.1
OB_HTTP_PORT=18180
OB_PUBLIC_PORT_SUFFIX=
OB_PLATFORM_NETWORK=platform
OB_PLATFORM_SUBNET=172.30.0.0/24
OB_PLATFORM_IP_RANGE=172.30.0.128/25
OB_TRUSTED_PROXIES=172.30.0.2/32
```

`OB_GRAFANA_URL` is written empty or with the Grafana Tailnet Origin. The bundle also
writes `OB_GATEWAY_HEALTH_HOST`, `OB_GATEWAY_URL` and `OB_BACKPLANE_URL`; current
Observability revisions no longer read them, and the lines are harmless.

In the backplane `.env`:

```sh
BP_ACCESS_MODE=proxy
BP_PUBLIC_URL=https://backplane.example.com
BP_BIND_HOST=127.0.0.1
BP_PORT=3000
BP_PLATFORM_NETWORK=platform
BP_PLATFORM_SUBNET=172.30.0.0/24
BP_PLATFORM_IP_RANGE=172.30.0.128/25
```

The bundle also writes `BP_TRUSTED_PROXIES=172.30.0.2/32`; Backplane ignores forwarded
headers by design and no longer reads it. Edge reaches the Backplane server directly at
`bp-server:3000`, so Backplane needs no Caddy behind Edge: keep its `edge` profile off and
set `BP_PUBLIC_URL` as the explicit browser origin. Edge keeps the operator-route denial
(`/health/operations` and `/metrics` answer 404), strips `Authorization` from
`/health/ready`, forwards `Host` and `X-Forwarded-Proto`, and streams event responses
unbuffered.

Behind Edge every stack publishes only its loopback HTTP port and issues no certificates.
Trust only Edge's fixed address, never the whole network. Do not attach a datastore to the
Platform Network; all members of this network are trusted infrastructure. To run a stack
on its own again, select its local or public access mode, set its domain and free host
ports, then run its bootstrap; stop Edge first to reuse ports 80 and 443, and preserve the
Edge volumes unless you are retiring its certificates.

## Trusting local HTTPS certificates

Local Mode issues certificates from Caddy's own CA in `edge-data`. Export the public root
after Edge starts and install it into each client's trust store; keep certificate
verification enabled:

```sh
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./platform-edge-root.crt
```

Internal CA private keys stay in `edge-data`; export only `root.crt`. Losing that volume
replaces the internal CA and requires redistributing trust, so take a
[Checkpoint](backup.md) before distributing the root and after any intentional CA change.

## Image overrides

`PE_CADDY_IMAGE` accepts a complete image reference, for example `local/edge:experiment`
or `registry.example/team/caddy:test`, in `.env`. Empty or unset selects the shipped
validated tag and digest in `compose.yaml`. Native `docker compose` interpolation applies;
a shell value takes precedence over `.env`. Use a literal reference: bootstrap refuses `$`,
`#` and whitespace in setting values. Local experiments are unvalidated and must provide
the Caddy and shell tools Edge uses.

`scripts/validate.sh`, `scripts/smoke.sh` and the backup drill always exercise the shipped
default, ignoring local overrides. Clearing the override returns to that default on the
next deployment. Checkpoints require a reference pinned by digest; see
[capture and restore](backup.md#capture-and-restore) before changing an installed image.

## Metrics and certificate expiry

The root `/metrics` is restricted by the socket peer's IP using `PE_METRICS_ALLOW`
(default `127.0.0.0/8 ::1`). Add only the scraper container's Platform Network address
as a `/32` (IPv6 `/128`), for example `PE_METRICS_ALLOW="127.0.0.0/8 ::1 172.30.0.130/32"`,
after reserving that address in the scraper's Compose configuration and verifying it.
Scrape `http://pe-edge:80/metrics` on the Platform Network in every access mode; set
`OB_SCRAPE_EDGE=true` in observability once the scraper is allowed, and leave it off when
Edge is absent.

Never allow the whole subnet or the Docker bridge gateway: published-port connections
relayed by Docker can all appear as that gateway, including remote clients under rootless
Docker or IPv6-to-IPv4 proxying, so allowing it can make metrics public. Edge does not
trust forwarded client IP headers, and in a cloud VPC "private" address space means every
tenant.

The response combines native Caddy metrics (`/metrics/caddy`, same restriction) with
`pe_certificate_not_after_seconds` and `pe_certificate_checked_seconds` from the
bootstrap-generated `/config/pe-certificate.prom` textfile. Run
`python3 scripts/bootstrap.py --probe-only` every five minutes to refresh the root leaf
observation after automatic renewal ([backup](backup.md) shows the cron line). Failure
leaves the last successful value; alert on expiry, stale or absent observations and scrape
failure. The root's expiry does not cover independently issued subdomain certificates:
external TLS probes must cover all seven names.

## Runtime logs

Caddy emits JSON access logs on stdout and runtime diagnostics on stderr. Docker uses
`journald` with `cache-disabled=true`: no application-managed or Docker JSON log files.
Journal persistence and retention are host policy; bootstrap never changes them. On a host
without journald, choose a supported Docker logging driver through an operator-owned
Compose override before starting the stack.

```sh
docker compose logs --tail=100 -f caddy
journalctl CONTAINER_NAME=platform-edge-caddy-1
```

The observability stack is optional. Its Alloy Docker discovery collects this container
through Docker's journal reader, labelled `compose_project=platform-edge` and
`service=caddy`. The Tailscale nodes log the same way (`CONTAINER_NAME=platform-edge-ts-<name>-1`).
Caddy removes request and response headers and query strings from access logs and
request-bearing error diagnostics; do not put credentials in URL paths.

## Status routes

The console reads each stack's public Status Document through same-origin routes that
`routes.d/00-stack-probes.caddy` owns: `/stack-status/gateway`, `/stack-status/backplane`
and `/stack-status/observability` proxy the owning gateway's `/status.json`;
`/stack-status/edge` (also `/status.json`) serves the document bootstrap writes after
readiness into the directory mounted at `/srv/state` (`data/console/` by default). These
routes accept GET and HEAD, strip Authorization and Cookie, apply a four-second deadline,
suppress upstream error and HTML bodies, and return uncached JSON. `/health` and
`/health/caddy` report Edge readiness only. When the status write fails, bootstrap exits 1
with `status_write_failed` although Caddy is running. The field rules, limits and fixtures
are in the [status contract](status-contract.md).

## Shared-host acceptance

After all sibling bootstraps pass, run:

```sh
SMOKE_INTEGRATION=1 SMOKE_DOMAIN=example.com PE_PLATFORM_NETWORK=platform scripts/smoke.sh
```

Use `localhost` for Local Mode. It requires running siblings with `lg-gateway`,
`ob-gateway` and `bp-server` on that network and prints `SKIP` with the missing aliases
when one is absent; a skip is not acceptance. The test starts its own edge on spare
loopback ports (`SMOKE_HTTP_PORT=18280`, `SMOKE_HTTPS_PORT=18643`, `SMOKE_PROJECT=platform-edge-smoke`
by default; override all three for another disposable instance), uses its project name as
its volume prefix and leaves sibling containers and volumes alone. `SMOKE_DOMAIN` must
equal the siblings' configured public domain. Six real application endpoints must return
200, first over HTTP and then over verified self-signed HTTPS:

| Host | Health endpoint |
| --- | --- |
| root | `/health/litellm` (real LiteLLM, not Edge liveness) |
| litellm | `/health/liveliness` |
| langfuse | `/api/public/health` |
| s3 | `/health/ready` |
| backplane | `/health/ready` |
| grafana | `/api/health` |

The ordinary smoke (without `SMOKE_INTEGRATION`) uses stubs on a disjoint `172.16.x.0/24`
network (`SMOKE_PLATFORM_SUBNET` moves it) to also exercise missing siblings, forwarding,
local HTTPS without HSTS on all seven hosts, console probes, metrics and container
hardening. Neither proves external DNS, port forwarding or public certificate issuance;
verify those from an external client as the final public check.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `port_conflict` | Another container publishes 80 or 443 (the message names it). Move that stack's gateway to a spare loopback port with its own bootstrap, then rerun. Bootstrap allows its own existing Caddy to hold the ports. |
| Compose error, exit 3 | A host process or a concurrent claim took the port after preflight, or Caddy did not become ready. Read `docker compose logs caddy`. |
| `platform_network_mismatch` | The `platform` network has another subnet or range. See [network cutover](#network-cutover). |
| `legacy_setting` naming `PE_TAILSCALE_EDGE_IP` | Remove the setting and any retired overlay from `COMPOSE_FILE`, then follow the network cutover. |
| `tls_files_unreadable` | Caddy cannot read `tls.key`, `tls.crt` or the CA file in `PE_TLS_DIR`. Check ownership, mode bits and symlink targets. |
| A sibling hostname answers 502 | That stack is not running or not on the Platform Network under its alias. Check `docker network inspect platform` for `lg-gateway`, `ob-gateway` or `bp-server`. |
| A route change has no effect | Caddy reads `routes.d/` at start. `docker compose restart caddy`; the admin API listens only on `localhost:2019` inside the container. |
| Sibling probes fail behind Edge | Each sibling probes its own loopback listener with the domain as Host: `curl -fsS -H 'Host: example.com' http://127.0.0.1:18080/health/litellm` and `http://127.0.0.1:18180/health/grafana`. |
| Edge cards show Unknown | The stack does not publish [status contract 2](status-contract.md) yet, or its producer is unreachable. Edge itself needs the version 1 timer retired once; see below. |

Exit codes: 0 ready, 1 refused, 2 usage, 3 not ready. Runtime failures are one JSON line on
stderr with `error` and `detail`.

### Network cutover

An installation whose `platform` network predates the fixed allocation (for example a
Docker-assigned `172.18.0.0/16`) is refused by every bootstrap with
`platform_network_mismatch`. The one-time fix recreates the network; certificate volumes,
data and env files are untouched:

1. Repair the Edge `.env` before anything stops: remove any `PE_TAILSCALE_EDGE_IP` value
   and any `COMPOSE_FILE` entry from the retired peer-pinning overlay. Set every sibling's
   `*_TRUSTED_PROXIES` to `172.30.0.2/32`.
2. Stop every stack on the network with its own `docker compose down` (containers only,
   never `-v`): siblings first, Edge last.
3. `docker network rm platform` (the configured `PE_PLATFORM_NETWORK`). Docker refuses
   overlapping subnets, so remove any other unused network that overlaps `172.30.0.0/24`
   as well (`docker network inspect <name> --format '{{len .Containers}}'` must print 0).
4. Run `python3 scripts/bootstrap.py` in platform-edge; it creates the network with the
   contract allocation and starts Edge at `172.30.0.2`. Then run each sibling's bootstrap.
5. Verify: `docker network inspect platform --format '{{json .IPAM.Config}}'` shows the
   subnet and ip-range, `docker inspect --format '{{.NetworkSettings.Networks.platform.IPAddress}}' "$(docker compose ps -q caddy)"`
   prints `172.30.0.2`, and every application hostname answers through Edge.

### Retiring the version 1 status timer

Edge reads only [status contract 2](status-contract.md) and runs no host observer. On an
installation that enabled the version 1 status timer, run once after updating the checkout:

```sh
scripts/retire-status-timer.sh
python3 scripts/bootstrap.py
```

The script disables and stops `platform-edge-status.timer` and its service, removes both
unit files from `~/.config/systemd/user/`, reloads the user manager, and deletes the old
`data/status/bootstrap.json` record and `data/console/.status.lock`. It prints each action
and is safe to rerun; when it cannot stop the timer it exits 1 before removing anything.
Bootstrap then replaces the version 1 `data/console/status.json` with the version 2 document.
