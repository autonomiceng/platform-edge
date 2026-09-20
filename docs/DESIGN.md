# platform-edge: systems design

One host, one Compose project, one shared Caddy for every installed platform stack. Vocabulary lives in [CONTEXT.md](../CONTEXT.md); the decision lives in [ADR-0001](adr/0001-one-edge-per-host.md).

## Why it exists

Multiple standalone stacks each ship an ingress, but only one ingress can own a given host address and port. The Edge centralizes public port ownership and certificate renewal while preserving each stack's standalone deployment.

## Guarantees, stated exactly

- Caddy is the only service and publisher in this project. Defaults bind HTTP and HTTPS ports to loopback.
- All six hostnames are configured even when some stacks are absent. An unavailable alias produces a request failure, independent of other aliases.
- `/health` returns 200 independently of upstream readiness. It is available on the root site and over HTTP in every access mode.
- Every application route sets `Host` to the requested hostname and `X-Forwarded-Proto` to the scheme received by Edge, or the configured external scheme when another gateway handles HTTPS.
- The root gateway failure serves a small static fallback page with status 502. Its same-origin probes show which applications answer. Application health remains a stack concern.
- Bootstrap renders no secrets, locks the env inode, refuses container port conflicts, ensures the network and external volumes, runs Compose with `--wait`, and prints route-derived hostnames. HTTPS readiness verifies the local TLS handshake with domain SNI and publishes the root leaf expiry. Exit codes match the sibling bootstrap contract: 0 ready, 1 refused, 2 usage, 3 not ready.

No high availability, dynamic service discovery, authentication, rate limiting or edge dashboards are promised.

## Shape

```mermaid
flowchart TD
    client[Browser or API client] -->|TCP 80 and 443| edge[Edge: Caddy]
    edge -->|root, litellm, langfuse, s3| gateway[lg-gateway:80]
    edge -->|backplane| backplane[bp-server:3000]
    edge -->|grafana| observability[ob-gateway:80]
```

Caddy joins only the external Platform Network, under alias `pe-edge`. There is no project-default network and no dependency on a stack's container lifecycle. The external volumes `${PE_VOLUME_PREFIX}_edge-data` and `${PE_VOLUME_PREFIX}_edge-config` keep TLS state and Caddy configuration state; routine Compose teardown preserves them. There is no Docker socket mount.

The root `Caddyfile` holds the shared global block and issuer snippets. `routes.d/gateway.caddy`, `backplane.caddy` and `observability.caddy` own the site blocks. The fallback page is the single `docker/console/index.html` file, mounted read-only. It checks at load, on request, and every 30 seconds while visible, without continuous animation.

## Access modes

Local mode serves HTTP and self-signed HTTPS together, without redirects or browser policies that force HTTPS. Public mode obtains trusted certificates for your domain and redirects HTTP to HTTPS. Behind another gateway (`proxy`), Edge receives HTTP and the other gateway handles HTTPS. In public mode, the explicit root HTTP health route remains reachable before the redirect. [Caddy documents this routing order](https://caddyserver.com/docs/automatic-https).

The certificate contact email is used only for public certificates. Its empty value uses Caddy's default. Site addresses follow the access mode; the canonical application scheme remains a separate setting. Stack Caddys use a separate HTTP listen scheme behind the Edge, while their applications retain public HTTPS origins. Backplane ignores forwarded headers and requires an explicit public URL.

## Operations

The ingress runbook contains exact per-stack settings and rollout order. Only move ports or restart services as an authorized installation action. Port conflict checks include overlapping TCP bindings and port ranges, and ignore this project's existing Caddy for idempotent reruns. Host-process conflicts and races after preflight remain Compose startup errors.

Validation uses the pinned image and mounts every Route File in local, public and proxy modes. Smoke owns a fresh project and network, starts three independent stubs using the same image, then tests routing, TLS scheme forwarding, restart with an absent alias, and fallback behavior. Smoke refuses existing project containers or volumes and never adopts an existing network.

Adding a hostname requires updating its Route File and the fallback page and extending smoke coverage. Bootstrap derives its JSON hostnames directly from the simple site-address lines in Route Files. Images are pinned only in Compose. The shared conventions file is copied verbatim; this repo has no application secrets, datastore checkpoint tooling or separate stack gateway. Certificate Checkpoints and restore drills are described in [backup](operations/backup.md).

Access logs are JSON on stdout; runtime diagnostics are on stderr. Docker sends both to
journald without a Docker log cache. The host owns journal retention, and Alloy collection
is optional. Local console aliases do not change application origins or grant metrics access.
The optional Tailscale setup keeps Edge in local mode with both HTTP and self-signed
HTTPS listeners, and forwards every Tailscale HTTPS endpoint to Edge's loopback HTTP
listener. It configures explicit application URLs and routes by hostname
and port, preserving the complete Host for signed requests. See [ADR-0002](adr/0002-tailscale-application-ports.md).
