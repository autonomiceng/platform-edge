# platform-edge

One Caddy for ports 80 and 443 when several stacks share a host. It gets the certificates and sends each hostname to the right stack.

[![CI](https://github.com/autonomiceng/platform-edge/actions/workflows/ci.yml/badge.svg)](https://github.com/autonomiceng/platform-edge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Caddy 2.11](https://img.shields.io/badge/Caddy-2.11-1F88C0)](https://github.com/caddyserver/caddy)

## What it is

The LLM gateway, the agent backplane and the observability stack each ship their own Caddy and each want port 80. On a host that runs more than one of them, this project takes the ports instead. It handles HTTPS once and forwards each hostname over the shared `platform` Docker network to the stack that owns it, with the original Host and scheme.

Every stack still works on its own without it. Add the edge when you add the second stack.

## Quick start

You need a Linux Docker host with journald, Compose 2.24.4 or newer, and Python 3.11 or newer.

```sh
git clone https://github.com/autonomiceng/platform-edge.git
cd platform-edge
python3 scripts/bootstrap.py --render-only
```

Choose the local or public Edge settings below, then run `python3 scripts/bootstrap.py` here. Add sibling stacks afterward using the [ingress guide](docs/operations/ingress.md). If an existing stack already owns ports 80 or 443, first move its gateway to spare loopback ports.
Bootstrap creates the shared network and external certificate volumes, checks port
conflicts, starts Caddy and verifies both local listeners, including HTTPS certificate trust. No sibling stack is required for Edge readiness.

## Local integration

Local Mode serves HTTP on 80 and self-signed HTTPS on 443, without redirecting HTTP or telling browsers to require HTTPS. Sibling ingresses use HTTP internally. A missing stack returns 502 on its hostnames while other stacks continue working. The root serves a fallback console when the gateway is absent.

Edge `.env`:

```sh
PE_ACCESS_MODE=local
PE_PUBLIC_DOMAIN=localhost
PE_SCHEME=http
PE_BIND_HOST=127.0.0.1
PE_HTTP_PORT=80
PE_HTTPS_PORT=443
PE_PLATFORM_NETWORK=platform
PE_ACME_EMAIL=
PE_VOLUME_PREFIX=platform-edge
PE_BACKUP_DIR=./backups
```

Gateway `.env`:

```sh
LG_ACCESS_MODE=proxy
LG_PUBLIC_DOMAIN=localhost
LG_SCHEME=http
LG_BIND_HOST=127.0.0.1
LG_HTTP_PORT=18080
LG_PUBLIC_PORT_SUFFIX=
LG_PLATFORM_NETWORK=platform
LG_TRUSTED_PROXIES=192.0.2.2/32
```

Observability `.env`:

```sh
OB_ACCESS_MODE=proxy
OB_PUBLIC_DOMAIN=localhost
OB_SCHEME=http
OB_BIND_HOST=127.0.0.1
OB_HTTP_PORT=18180
OB_PUBLIC_PORT_SUFFIX=
OB_PLATFORM_NETWORK=platform
OB_TRUSTED_PROXIES=192.0.2.2/32
OB_GATEWAY_HEALTH_HOST=localhost
OB_GATEWAY_URL=http://localhost
OB_BACKPLANE_URL=http://backplane.localhost
```

Replace `192.0.2.2/32` above with Edge’s reserved address on the shared Docker network, as described in the [ingress guide](docs/operations/ingress.md).

Backplane `.env` (start core services without its optional `edge` profile):

```sh
BP_PUBLIC_URL=http://backplane.localhost
BP_BIND_HOST=127.0.0.1
BP_PORT=3000
```

The console also opens at `http://127.0.0.1`; verified direct HTTPS requires installing
Edge's public CA root. Application links retain their configured hostnames. To share the
console through Tailscale's trusted HTTPS, run the optional helper after startup:

```sh
sudo python3 scripts/tailscale_serve.py --https-port 8443
```

It prints the URL and does not modify unrelated Serve endpoints. See the
[ingress guide](docs/operations/ingress.md) for prerequisites, collision handling,
trusting self-signed certificates, and configuring names for application access.

Runtime logs go to stdout/stderr and Docker journald, without Docker log files or cache.
Alloy collection is optional; `docker compose logs -f caddy` works without observability.
Host journal persistence remains the operator's choice. Fresh installs provide HTTP and self-signed HTTPS. Choose public mode for your own domain, or proxy mode when another gateway handles HTTPS.

## Public integration

Point DNS for the root and five subdomains at the host and open TCP 80/443. Certificate issuance requires all six names to be reachable. For private DNS, select `PE_ACCESS_MODE=local` with the private domain and distribute its public CA root.

Edge `.env`:

```sh
PE_ACCESS_MODE=public
PE_PUBLIC_DOMAIN=example.com
PE_SCHEME=https
PE_BIND_HOST=0.0.0.0
PE_HTTP_PORT=80
PE_HTTPS_PORT=443
PE_PLATFORM_NETWORK=platform
PE_ACME_EMAIL=ops@example.com
PE_VOLUME_PREFIX=platform-edge
PE_BACKUP_DIR=./backups
```

Gateway `.env`:

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

Observability `.env`:

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

Replace `192.0.2.2/32` above with Edge’s reserved address on the shared Docker network, as described in the [ingress guide](docs/operations/ingress.md).

Backplane `.env` (start core services without its optional `edge` profile):

```sh
BP_PUBLIC_URL=https://backplane.example.com
BP_BIND_HOST=127.0.0.1
BP_PORT=3000
```

The Edge reaches the backplane at `bp-server:3000`; keep the backplane `edge` profile off.
The six routed hosts are root, `litellm.`, `langfuse.`, `s3.`, `backplane.`
and `grafana.` under the configured domain. Run shared-host acceptance after siblings
are ready: `SMOKE_INTEGRATION=1 SMOKE_DOMAIN=example.com scripts/smoke.sh` (use
`localhost` for Local Mode). A missing-sibling skip is not a pass.

## What's inside

| Service | Job | Data |
| --- | --- | --- |
| Caddy | TLS, hostname routing, a health path, a fallback page | `edge-data` (certificates and CA keys) and `edge-config` volumes |

Routes live in `routes.d/`, one file per stack. The image is pinned as `tag@sha256` in `compose.yaml`.

## Built on

| Project | Stars | What we use it for |
| --- | --- | --- |
| [Caddy](https://github.com/caddyserver/caddy) | ![stars](https://img.shields.io/github/stars/caddyserver/caddy?style=flat) | Routing and automatic HTTPS |
| [Docker Compose](https://github.com/docker/compose) | ![stars](https://img.shields.io/github/stars/docker/compose?style=flat) | Running it |

## The other stacks

- [llm-gateway-stack](https://github.com/autonomiceng/llm-gateway-stack): LiteLLM and Langfuse.
- [agent-backplane](https://github.com/autonomiceng/agent-backplane): shared state, queues and approvals for agents.
- [observability-stack](https://github.com/autonomiceng/observability-stack): Grafana, Loki, Tempo and Mimir.

All four deploy the same way. Shared conventions are in [docs/conventions.md](docs/conventions.md).

## Day two

- [Ingress: per-stack settings, DNS, certificates, CA export](docs/operations/ingress.md)
- [Backup, restore, CA preservation and RPO/RTO](docs/operations/backup.md)
- [Design](docs/DESIGN.md), [vocabulary](CONTEXT.md), [decisions](docs/adr/)

Run `scripts/backup.sh` for both Edge state volumes (`edge-data` and `edge-config`) and replicate the encrypted Checkpoint off-host. Read the migration instructions before upgrading from managed volumes.

## Development

```sh
scripts/validate.sh                    # static checks, what CI runs on every push
python3 -m unittest discover -s tests  # unit tests, no Docker
scripts/smoke.sh                       # disposable edge: HTTP and verified self-signed HTTPS
scripts/backup-drill.sh                # prove CA and TLS survive restore; print RTO
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Report vulnerabilities through the [security policy](SECURITY.md). The edge does TLS and routing only; each application does its own login.

## License

[MIT](LICENSE).
