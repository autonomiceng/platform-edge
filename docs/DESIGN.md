# platform-edge: systems design

One host, one Compose project, one shared Caddy for every installed platform stack. Vocabulary lives in [CONTEXT.md](../CONTEXT.md); the decision lives in [ADR-0001](adr/0001-one-edge-per-host.md).

## Why it exists

Multiple standalone stacks each ship an ingress, but only one ingress can own a given host address and port. The Edge centralizes public port ownership and certificate renewal while preserving each stack's standalone deployment.

## Guarantees, stated exactly

- `/status.json` and `/stack-status/edge` are unauthenticated in every access mode, including public internet access, and expose allowlisted version and image digest metadata.
- Caddy is the only service and publisher in this project. Defaults bind HTTP and HTTPS ports to loopback.
- All seven hostnames are configured even when some stacks are absent. An unavailable alias produces a request failure, independent of other aliases.
- `/health` returns 200 independently of upstream readiness. It is available on the root site and over HTTP in every access mode.
- Every application route sets `Host` to the requested hostname and `X-Forwarded-Proto` to the scheme received by Edge, or the configured external scheme when another gateway handles HTTPS.
- The root serves the project console with status 200 independently of sibling stacks. Same-origin probes report application failures; application health remains a stack concern.
- Bootstrap renders no secrets, locks the env inode, refuses container port conflicts, ensures the network and external volumes, runs Compose with `--wait`, and prints route-derived hostnames. HTTPS readiness verifies the local TLS handshake with domain SNI and publishes the root leaf expiry. Exit codes match the sibling bootstrap contract: 0 ready, 1 refused, 2 usage, 3 not ready.

No high availability, dynamic service discovery, authentication, rate limiting or edge dashboards are promised.

Selected host installation enters through bootstrap's `--stack`/`--dry-run` flags.
`scripts/installation.py` preflights only selected sibling configuration;
`scripts/installation_execution.py` qualifies existing image/mount custody, preserves
owning env state atomically, and invokes each owning bootstrap after all selected
checks pass. Edge's exact peer is pinned before sibling trust is written. No lifecycle
logic or secret generation moves out of the owning stacks. The
[installation contract](operations/ingress.md#selected-installation) describes recovery,
owner-interface limits, selected private Tailscale connection, and owning status-timer opt-in. Aggregate completion does
not claim host acceptance or enrollment.

## Shape

```mermaid
flowchart TD
    client[Browser or API client] -->|TCP 80 and 443| edge[Edge: Caddy]
    edge -->|Gateway paths, litellm, langfuse, s3, rustfs| gateway[lg-gateway:80]
    edge -->|backplane| backplane[bp-gateway:80]
    edge -->|grafana| observability[ob-gateway:80]
```

Caddy joins only the external Platform Network, under alias `pe-edge`. There is no project-default network and no dependency on a stack's container lifecycle. The external volumes `${PE_VOLUME_PREFIX}_edge-data` and `${PE_VOLUME_PREFIX}_edge-config` keep TLS state and Caddy configuration state; routine Compose teardown preserves them. There is no Docker socket mount.

The root `Caddyfile` holds the shared global block and issuer snippets. `routes.d/gateway.caddy`, `backplane.caddy` and `observability.caddy` own the site blocks. The root `/` serves the console even without Gateway; other Gateway paths retain their proxy routes. The console uses static HTML, CSS and JavaScript in `docker/console/`, mounted read-only, with no build step. Project cards, search, service details and an architecture map share one catalog. It refreshes access settings and health at load, on request, and every 30 seconds while visible, without continuous animation. Cards link their titles to application interfaces and provide copyable endpoints. HTTP probes report reachability only. Component health requires fresh, component-specific public status evidence. Optional components and map connections describe the stack architecture, not detected installation state or live traffic. The bounded status consumer reads independent `/stack-status/{stack}` documents with three concurrent requests, four-second deadlines and 64 KiB/32-component limits. Optional `/stack-versions/gateway` metadata supplies explicitly configured versions when current status configuration is unavailable. Missing producers leave health unknown and preserve trusted console navigation. `routes.d/00-stack-probes.caddy` owns these credential-free metadata routes; its prefix makes shared snippets available before application Route Files are expanded. Project icons are bundled locally. The uncached `/edge-config.json` endpoint keeps already-open pages current after access setup changes.

## Access modes

Local mode serves HTTP and self-signed HTTPS together, without redirects or browser policies that force HTTPS. Public mode obtains trusted certificates for your domain and redirects HTTP to HTTPS. Behind another gateway (`proxy`), Edge receives HTTP and the other gateway handles HTTPS. In public mode, the explicit root HTTP health route remains reachable before the redirect. [Caddy documents this routing order](https://caddyserver.com/docs/automatic-https).

The certificate contact email is used only for public certificates. Its empty value uses Caddy's default. Site addresses follow the access mode; the canonical application scheme remains a separate setting. Stack Caddys use a separate HTTP listen scheme behind the Edge, while their applications retain public HTTPS origins. Backplane ignores forwarded headers and requires an explicit public URL.

## Operations

The [public stack status contract](operations/status-contract.md) defines a versioned
interface for independent status producers and console consumers. Its fixtures describe
compatibility and freshness requirements; they do not attest deployed producer support.

The [Edge host observer](operations/status-observer.md) publishes bounded Caddy
readiness and version observations through a read-only public file mount. Bootstrap
attempts an initial observation; periodic publication is an explicit user-timer opt-in.

The ingress runbook contains exact per-stack settings and rollout order. Only move ports or restart services as an authorized installation action. Port conflict checks include overlapping TCP bindings and port ranges, and ignore this project's existing Caddy for idempotent reruns. Host-process conflicts and races after preflight remain Compose startup errors.

Validation and smoke use the shipped validated default image regardless of local image overrides. Validation mounts every Route File in local, public and proxy modes. Smoke owns a fresh project and network, starts three independent stubs using the same image, then tests routing, TLS scheme forwarding, restart with an absent alias, and independent console availability. Smoke refuses existing project containers or volumes and never adopts an existing network.

Adding a hostname requires updating its Route File and the console and extending smoke coverage. Bootstrap derives its JSON hostnames directly from the simple site-address lines in Route Files. Validated image defaults are pinned only in Compose; `PE_CADDY_IMAGE` accepts a complete reference for unvalidated local experiments. The shared conventions differ only in the approved image-policy bullet; this repo has no application secrets, datastore checkpoint tooling or separate stack gateway. Certificate Checkpoints and restore drills are described in [backup](operations/backup.md).

Access logs are JSON on stdout; runtime diagnostics are on stderr. Docker sends both to
journald without a Docker log cache. The host owns journal retention, and Alloy collection
is optional. Local console aliases do not change application origins or grant metrics access.
The optional Tailscale setup keeps Edge in local mode with both HTTP and self-signed
HTTPS listeners, and forwards each Tailscale HTTPS endpoint created by this setup to Edge's loopback HTTP
listener. It configures explicit application URLs and routes by hostname
and port, preserving the complete Host for signed requests. See [ADR-0002](adr/0002-tailscale-application-ports.md).
