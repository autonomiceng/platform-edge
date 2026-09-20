# Ingress and access modes

Edge owns host ports 80 and 443. Its default loopback binding makes them accessible only on that host. Stacks join the same external network and retain their private HTTP ingresses. Replace `example.com` below with one domain shared by all three stacks.

| Hostname | Upstream Alias |
| --- | --- |
| `example.com` | Edge console at `/`; other Gateway paths use `lg-gateway:80` |
| `litellm.example.com` | `lg-gateway:80` |
| `langfuse.example.com` | `lg-gateway:80` |
| `s3.example.com` | `lg-gateway:80` |
| `rustfs.example.com` | `lg-gateway:80` (admin console) |
| `backplane.example.com` | `bp-gateway:80` |
| `grafana.example.com` | `ob-gateway:80` |

## Selected installation

Repeat `bootstrap.py --stack NAME` to select `edge`, `gateway`, `backplane`, or
`observability`. Edge is implicit. No `--stack` retains standalone Edge bootstrap.
Omission never removes an installed stack, route, or Tailscale application.

```sh
python3 scripts/bootstrap.py --stack backplane --stack observability \
  --backplane-dir /srv/agent-backplane --observability-dir /srv/observability-stack \
  --backplane-backup-dir /mnt/backplane-backups \
  --capability-file /home/operator/private/backplane-enrollment --dry-run
```

Remove `--dry-run` to execute the selected owning bootstraps after all selected
preflight checks pass. Dry-run writes nothing and reports `executable`, conflicts,
ordered native Compose selection, listeners, origins, and owning recovery runbooks.
It renders Compose with fixed, in-memory interpolation sentinels for missing fresh
secrets. Only owning bootstraps generate and store actual secrets. A plan proves no
runtime readiness or enrollment. `--dry-run` alone plans Edge only; render/probe
flags cannot be combined with selected installation or dry-run.

Checkout defaults are sibling `llm-gateway-stack`, `agent-backplane`, and
`observability-stack` directories. Override them with `--gateway-dir`,
`--backplane-dir`, and `--observability-dir`. Only selected sibling configuration is
read. Configure Edge through its env or fresh `--template`. Unset selected stack,
managed secret, and Compose exports. Env files must be owned, private, single-link
regular files; their parent directories must be owned and not group/other writable.

Gateway needs `--gateway-backup-dir` and `--gateway-email` unless recorded as
`LG_BACKUP_DIR` and `LANGFUSE_INIT_USER_EMAIL`. Backups must use an existing writable
separate filesystem, without overlap with Postgres data, unless the owning
`LG_ALLOW_SAME_FILESYSTEM_BACKUP=true` development policy permits the same filesystem.
Backplane needs an existing writable backup directory and `--capability-file` with
an absolute path and a private writable parent. Capability contents are never read
by the installer. Existing backup/email values cannot be replaced by these inputs.
An unchanged rerun does **not** require a retained Checkpoint. Empty configured
backup directories are valid; live data, certificates, and credentials remain protected.

Fresh Backplane uses its owning full default with gateway ingress; pass
`--backplane-mode minimal` for filesystem Files without Functions. Recorded native
files, profiles, backend, project and volume prefix remain authoritative on rerun.
Conflicting modes require Backplane's upgrade/migration procedure. This requires the
full/minimal owning bootstrap interface, including capability readiness checks.

Fresh siblings use proxy mode on the shared Platform Network and loopback ports
18080 (Gateway), 18180 (Observability), and 3000 (Backplane). Local Backplane uses
HTTPS through Edge; install the public Edge CA root in client trust stores. Existing
public origins and already connected Tailnet application origins are preserved.
A newly added application uses direct Edge access unless `--tailscale` is selected.
Native console enable flags, login requirements, and client allowlists are preserved.

Preflight requires trusted local Docker, Compose 2.24.4+, selected configuration
files, known resource inventory, and free or qualified selected TCP listeners.
Existing containers, including stopped containers, must match rendered effective
image IDs, service selection, and named-volume/bind mounts. Unknown custody,
missing saved secrets, foreign publications, or an upgrade stop all execution before
env/container changes. Each action names the owning recovery runbook. Interrupted
preparation resumes with complete private saved secrets and native selection, even
after volume creation with no containers or only some services present. Every found
or referenced existing volume must carry the selected `com.docker.compose.project`
label, or be mounted only by containers that pass the image, service and mount checks.
Unlabelled volumes without qualified containers and foreign-labelled volumes are refused,
including unmounted prefix collisions. For an old unlabelled installation after teardown,
restore its original owning containers before using selected installation; inspect foreign
volume users independently. No relabelling or volume replacement is performed.
Image-declared anonymous volumes are accepted only
when Docker created them implicitly and all their users are qualified containers. Each present container must still qualify; the owning
bootstrap completes missing services and checks its database/storage binding.

Execution rechecks qualification under owning locks, publishes only selected public
settings atomically, and calls the owning bootstraps serially from their checkouts.
Edge starts first, then its exact peer is reserved with the shipped
`compose.tailscale.yaml` and `PE_TAILSCALE_EDGE_IP` setting. This reservation also works
without Tailscale. Its address is verified before sibling proxy trust is written;
network CIDRs are never trusted. Existing native overlays retain their order.

Backplane retains native image defaults and any explicit operator image overrides.
The installer does not write image overrides. Each present container's image ID must
match its rendered reference's current local image ID; default builds and workerd
recipe verification remain the owning bootstrap's behavior.

Edge labels newly created volumes with its selected Compose project. Existing
unlabelled Edge and Gateway volumes can be qualified through their matching containers,
without relabelling or replacing data. Gateway's owning bootstrap must label new volumes
so interruption before container creation also leaves verifiable ownership.
Observability custom overlays retain the recorded order for both configuration validation
and startup. The base must remain first, with the selected storage and proxy overlays
present and no duplicate files. No installer shadow selection is used.
A stopped, unpinned Edge needs its original peer recovered before reuse. Do not run
independent lifecycle commands concurrently.

Exit 0 means the requested owning bootstraps completed; exit 1 means preflight refused,
2 means usage, and 3 means execution stopped. The bounded JSON result lists
`completed` stacks and `stopped_at`; raw child output is discarded to protect secrets.
Infrastructure acceptance and enrollment remain unverified by this aggregate result.
Correct the owning failure and rerun the same selection. There is no global rollback,
volume deletion, aggregate env, or job database. Native owner status records remain
the interruption evidence. SIGKILL can leave `<backplane-dir>/.env.lock`, which is
preserved and blocks reruns. Following Backplane's documented stale-lock procedure,
confirm no installer, preparation or bootstrap process is running, remove only that
confirmed stale preparation lock, then rerun the same selection. Preserve env,
capability files and enrollment checkpoints. A dead-looking PID is not authorization
to unlink a lock; interruption recovery is not automatic across this boundary.

Add `--tailscale` to derive private HTTPS origins before publishing any selected
configuration or invoking an owning bootstrap. Preflight reads only selected sibling
envs/checkouts and checks their application ports plus Edge. Missing omitted siblings
are irrelevant. Existing omitted application entries, port settings, Route Files, and
Serve listeners remain intact. Selected RustFS consoles are included only when enabled
by the owning stack and its native storage selection. The saved Edge machine name
cannot change during a selected connection. Existing exact proxy trust and complete
Host forwarding remain required.

The connection reuses this installation plan, custody checks, native Compose selection,
and owning bootstraps. Existing Platform Network bridge details are checked before
execution; a fresh network's exact bridge peer is discovered after Edge creates it and
before any sibling starts. Edge retains loopback HTTP and HTTPS. The helper refuses
selected custom handlers, foreign host listeners, and Funnel before execution. Matching
Serve endpoints are read-only, including port 8450. A new endpoint can still need
administrator permission: exit 3 reports completed stacks, `stopped_at=tailscale`,
and the exact `sudo tailscale serve ...` command. Run only that command as administrator,
then repeat the original selection as the installation user. No Docker privilege
workaround or automatic rollback is used. The retry reads actual Serve state and checks
HTTPS origins, application reachability, and anonymous protected API denial. A console
blocked by its client allowlist is reported as `access_denied`; this does not establish
successful native login. Authenticated acceptance and enrollment remain operator checks.

Add `--status-timers` to check each selected owning timer before installation and invoke
its installer after the requested bootstraps and connection succeed. Edge is implicit.
The owner must implement the read-only `--check` and exact-pair recovery contract in
[status observation](status-observer.md#selected-timer-owner-followups). Current sibling
helpers lack that contract, so selecting their timers refuses before any mutation.
No timer for an omitted stack is inspected or changed. Root must run fresh/rerun host
acceptance. Merge remains gated by H-PROOF, BDEFAULT, and HSELECT.

## Image overrides

`PE_CADDY_IMAGE` accepts a complete image reference, for example `local/edge:experiment`
or `registry.example/team/caddy:test`, in `.env`. Empty or unset selects the shipped
validated tag and digest in `compose.yaml`. Native `docker compose` interpolation applies;
a shell value takes precedence over `.env`. Bootstrap preserves the setting and env lock.
Use a literal reference: bootstrap refuses `$`, `#` and whitespace in setting values,
including nested environment expressions that bare Compose would expand.
Local experiments are unvalidated and must provide the Caddy and shell tools used by Edge.

`scripts/validate.sh`, `scripts/smoke.sh` and the backup drill always exercise the shipped
default, ignoring local image overrides. Clearing the override returns to that default
on the next authorized deployment. Checkpoints require a digest-qualified reference;
see [capture and restore](backup.md#capture-and-restore) before changing an installed image.

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
The configured root hostname serves the Edge console independently of Gateway. Other
Gateway paths still proxy to the Gateway and report upstream failures. Bootstrap does
not require any sibling stack.

For local sibling stacks behind Edge, choose their proxy access mode, keep internal HTTP,
and configure the configured browser URL for the address customers will use. Applications
with authentication or generated links still need one configured application URL even though Edge
accepts both HTTP and HTTPS. Give sibling Caddys spare loopback HTTP ports. Use Backplane’s internal gateway overlay and set its explicit `BP_PUBLIC_URL`.

## Access everything through Tailscale

Start Edge and the stacks you want to use, then run from the Edge checkout:

```sh
python3 scripts/tailscale_serve.py --dry-run
python3 scripts/tailscale_serve.py
```

Log in to Tailscale and enable its HTTPS certificates first. Run the helper as the
installation user. If a Serve change needs administrator permission, it prints the
exact `sudo tailscale serve ...` command. Run that command, then rerun the helper to
verify the actual HTTPS endpoints. Matching Serve endpoints require no rewrite. The helper connects existing installations; it does not install
missing stacks or create credentials. By default it looks for `.env` in the sibling
`llm-gateway-stack`, `observability-stack`, and `agent-backplane` checkouts. Use
`--gateway-dir`, `--observability-dir`, or `--backplane-dir` for other locations.
Use `--env-file` for a different Edge environment file. This standalone helper keeps
its original **all installed siblings** behavior: every sibling with an env is considered,
and missing envs are skipped. Use bootstrap's `--stack ... --tailscale` for selected-only
installation and connection; the standalone helper does not accept a stack selection.

You get private HTTPS links on one machine name, without editing DNS or installing a
certificate on your computer:

| Application | Default HTTPS port |
| --- | --- |
| Platform Edge | 443 |
| LiteLLM | 8443 |
| Langfuse | 8444 |
| S3 object storage | 8445 |
| LLM Gateway overview | 8446 |
| Grafana | 8447 |
| Agent Backplane | 8448 |
| RustFS admin console | 8449 |

Open the printed Platform Edge link to launch applications. S3 is an API endpoint;
use an S3 client and your existing credentials. RustFS has a separate browser admin console, enabled by default in the gateway. Set `LG_RUSTFS_CONSOLE=off` to disable it; the helper only connects it when enabled. Each application keeps its own login.
The LiteLLM operator pages become reachable through the private Edge connection;
application authentication remains required. Your tailnet access rules must permit the
chosen ports. Use `--https-port` for the landing page and `--port-base` to move the nine
consecutive application ports together. Selected installation uses the recorded
`PE_TAILSCALE_*_PORT` settings instead of resetting ports.

Each Tailscale listener created by this setup forwards to the configured Edge loopback
HTTP endpoint at `http://127.0.0.1:<PE_HTTP_PORT>` (port 80 by default);
Edge chooses the application by hostname and port. Services are not exposed directly.
Edge retains localhost HTTP and self-signed HTTPS on its configured ports (80 and 443
by default), using its existing certificate state. Tailscale adds trusted HTTPS for remote
clients. Application login and generated links use the selected Tailscale URLs; keeping
local listeners does not give an application two separate canonical login URLs.

The helper updates application URLs, keeps Edge in local mode, configures sibling
gateways for HTTP behind Edge, pins Edge's current network address for proxy trust, and recreates the
services that need those settings. It preserves credentials, storage volumes and unrelated
Compose overlays. It checks every Compose configuration before writing settings. It never
enables Funnel and preserves unrelated Serve endpoints. Conflicting root handlers require
`--replace`; non-HTTPS listeners and Funnel endpoints are always refused. Ports with custom path handlers are also refused, since those paths could bypass the
selected application's root handler.

If setup fails partway, correct the reported error and rerun. Inspect `tailscale serve
status` and the affected service's logs; partial setup is not reported as success. Rerun
after installing another stack to connect it. Keep the generated `COMPOSE_FILE` setting
when recreating Edge so its pinned address stays consistent with sibling proxy trust.

Serve endpoints created by this setup forward to Edge's **HTTP** port, usually `http://127.0.0.1:80`.
Do not forward HTTP to local port 443, and do not use an HTTPS listener on port 80 when
expecting ordinary HTTP clients. Tailscale access is optional; each stack retains its
standalone local and public-domain setup.

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

Set A and, where IPv6 is configured, AAAA records for all seven names to the host. The root needs its own record even if a wildcard record covers subdomains. Every configured name must resolve correctly and reach Caddy to obtain trusted certificates, whether or not its application stack is running. Forward TCP 80 and 443 through the host firewall and any NAT. An AAAA record must not point to an unreachable IPv6 listener.

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

Start Backplane with its internal gateway overlay (Compose 2.24.4+):

```sh
docker compose -f compose.yaml -f compose.gateway.yaml --profile gateway up -d --wait
```

This runs Caddy as `bp-gateway:80` without publishing host ports. Keep the standalone
`edge` profile off. The gateway joins Backplane’s private network and the shared
Platform Network. Backplane ignores forwarded headers by design; `BP_PUBLIC_URL`
remains the explicit browser origin. Existing direct `bp-server` access remains
available for internal telemetry; Edge routes application requests through `bp-gateway`.
For an existing installation, start and verify the gateway before updating Edge’s routes.

Do not attach a datastore to the Platform Network. All members of this network are trusted infrastructure.

## Rollout and diagnosis

When upgrading an existing Backplane installation from direct server routing, start
and verify `bp-gateway` before applying the new Edge routes. Use the Backplane
command above; verify `docker compose -f compose.yaml -f compose.gateway.yaml
--profile gateway exec edge wget -qO- http://127.0.0.1/health/ready` reports ready.
Then reload or bootstrap Edge. Fresh installs may start Edge first to create its
network; Backplane remains unavailable until its selected stack starts. Edge itself
must remain independently startable when Backplane is not installed.


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

This requires running Compose siblings with `lg-gateway`, `ob-gateway` and `bp-gateway`
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
seven hosts, empty uncached console probes, metrics and container hardening.
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
external TLS probes must cover all seven names. HSTS is `max-age=31536000` in public mode, with no preload or includeSubDomains. Local mode deliberately has no HSTS. Console probes expose status and an empty
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

## Project console

The Edge console groups applications and supporting services by project. Search filters
service names and descriptions. Project overview links narrow the same interface; Map
shows application and log-collection paths with independent toggles. Component details
link to the component’s upstream repository; project headers link to the stack repository.

Refresh and automatic checks every 30 seconds update application addresses, HTTP
reachability and independent stack status. Network polling pauses while hidden; evidence
expires at its original deadline even between refreshes. Three concurrent requests share
four-second per-request deadlines; status documents are limited to 64 KiB and 32 components.
HTTP 200 alone never makes a component healthy. Missing, malformed, incompatible or failed
producers leave other cards usable. Failed refreshes retain previous observations only as
explicitly stale. Component links come exclusively from validated Edge access configuration
and remain available when component health or producer support is unknown.

`/stack-status/gateway`, `/stack-status/backplane` and `/stack-status/observability`
proxy their owning gateway's `/status.json`. `/stack-versions/gateway` remains a legacy
configured-version fallback, dated by valid `configuredAt`/`pinnedAt` or labelled undated.
The Edge card reads `/stack-status/edge`, published by the installed host observer.
If that producer is unavailable, its own image version remains a configured, undated
fallback and component health stays unknown.
A configured image version is never displayed as an observed runtime version. Fresh status
configuration takes precedence. Tasks display their execution start separately from the
freshness of the record inspection. Optional architecture entries with no evidence are unknown.

Metadata routes accept GET/HEAD, remove Authorization and Cookie, suppress upstream error
and HTML fallback bodies, and return uncached JSON. **Each producer must enforce the contract's
closed public field allowlist.** Edge checks transport and the browser validates schema;
Caddy does not sanitize fields inside successful JSON. No backend administration route,
Docker socket or observer credential is exposed. Gateway, Backplane and Observability status
producers can roll out independently; there are no new operator environment settings.
Deploy the reviewed console and routes using the existing rollout procedure. Missing
sibling producers need no workaround. Before the Edge observer publishes its first
record, `/stack-status/edge` returns empty JSON 404 and Edge component health is unknown.
`/health` reports HTTP reachability only.

Consumer checks use `node --test tests/status*.test.cjs` and the existing
`tests/console-browser.cjs` Playwright acceptance runner. `scripts/smoke.sh` owns disposable
Docker resources and invokes `tests/status_proxy.py` to check methods, credentials,
404/HTML/error suppression, forwarding and partial producer failure. Fixture tests do not
attest deployed sibling producers.

The status consumer enforces a four-second total request deadline, including body
reads, and a 64 KiB body limit. It aborts and cancels a slow or oversized response.
The Caddy status proxy separately limits connection setup, response headers and
idle reads; its read timeout is not a total response-body deadline. Direct clients
of these routes must apply their own total deadline and size limit. Producers are
trusted stack services publishing bounded public metadata.

### Optional private storage consoles

Backplane and Observability can expose their already enabled native RustFS consoles
at Tailscale ports 8450 and 8451. The Edge console adds links only for connected
endpoints. Each service keeps its native login, and agents continue to use Backplane's
Files API. The connector does not enable a storage backend or migrate any data.

First deploy the reviewed sibling console feature and explicitly enable
`BP_RUSTFS_CONSOLE=true` or `OB_RUSTFS_CONSOLE=true` in that installation. Backplane
must persist its existing `compose.gateway.yaml` and `compose.blobs.yaml` in
`COMPOSE_FILE` and their `gateway,blobs` profiles in `COMPOSE_PROFILES` (keep any
other selected files and profiles). Observability requires its existing `s3`
profile and `compose.s3.yaml`. Do not add these settings to a filesystem installation
as a substitute for its migration procedure.

Run `scripts/tailscale_serve.py --dry-run` to inspect the proposed links. Its optional
`--console-allow '100.64.0.0/10 fd7a:115c:a1e0::/48'` explicitly permits Tailnet clients
at enabled Backplane and Observability consoles. Supply narrower client IPs/CIDRs
when required. Without this option, each existing `BP_RUSTFS_CONSOLE_ALLOW` or `OB_RUSTFS_CONSOLE_ALLOW` list is preserved. General monitoring allowlists are unchanged.
Tailnet access policy and native application login still apply. No Funnel endpoint
is adopted; Funnel must remain disabled on these listeners. These client ranges are never trusted proxy ranges.

`PE_TRUSTED_PROXIES` contains only exact ingress peer IPs. The connector records the
Platform Network's IPv4 host gateway because Tailscale Serve reaches Edge through
its loopback host port. Edge accepts forwarded client information only from that
peer and sends one validated client address to the console gateways. Each sibling
trusts only Edge's pinned Platform Network address. Arbitrary clients cannot supply
their own forwarded identity. The host and Docker administrators remain trusted;
local host processes can reach the same loopback ingress. A different host networking
layout needs explicit validation of its ingress peer before deployment.

The whole console origin is proxied, including login, administrative requests and
S3 requests. Host, port and HTTPS scheme are retained for native authentication and
request signatures. RustFS has no published host port and stays off the Platform
Network. The connector checks current images and persistent mounts before changing
settings and recreates only the selected services. Keep a verified checkpoint before
running it; a partial failure can be corrected and rerun without deleting volumes.

With trusted peers configured, any additional host proxy must replace untrusted
forwarded-client headers and must not forward Tailnet console authorities. Prefer
per-operator `/32` or `/128` client entries. The broader Tailnet ranges also admit
shared-in nodes allowed by Tailscale policy. Agents must not receive host networking
or Docker administration authority if they are outside this trusted host boundary.

The setup result reports console access from the setup host separately. A 401,
403 or 404 from that exact HTTPS origin means access was denied or the console
was unavailable to this host; it does not prove a working login. Complete the
native login check from an explicitly allowed client. Existing application
routes retain their previous peer-address forwarding; only the new console
routes forward the validated client address. Docker configurations with
`userland-proxy: false` require explicit ingress-peer validation before use.

The selected-stack preflight recognizes the Gateway template’s native `compose.${LG_ACCESS_MODE:-local}.yaml` selection. Other interpolated Compose paths require explicit recorded file paths before planning.
