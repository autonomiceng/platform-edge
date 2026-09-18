# platform-edge

One Caddy for ports 80 and 443 when several stacks share a host. It gets the certificates and sends each hostname to the right stack.

[![CI](https://github.com/autonomiceng/platform-edge/actions/workflows/ci.yml/badge.svg)](https://github.com/autonomiceng/platform-edge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Caddy 2.11](https://img.shields.io/badge/Caddy-2.11-1F88C0)](https://github.com/caddyserver/caddy)

## What it is

The LLM gateway, the agent backplane and the observability stack each ship their own Caddy and each want port 80. On a host that runs more than one of them, this project takes the ports instead. It terminates TLS once and forwards each hostname over the shared `platform` Docker network to the stack that owns it, with the original Host and scheme.

Every stack still works on its own without it. Add the edge when you add the second stack.

## Quick start

You need Docker with the Compose plugin and Python 3.11 or newer.

```sh
git clone https://github.com/autonomiceng/platform-edge.git
cd platform-edge
python3 scripts/bootstrap.py --render-only
```

Configure and start the sibling stacks using the Local or Public settings below and the [ingress guide](docs/operations/ingress.md), then run `python3 scripts/bootstrap.py` here.
Bootstrap creates the shared network and external certificate volumes, checks port
conflicts, starts Caddy and verifies the selected HTTP or HTTPS listener.

## Local integration

Local Mode uses HTTP throughout. A missing stack returns 502 on its hostnames while other stacks continue working. The root serves a fallback console when the gateway is absent.

Edge `.env`:

```sh
PE_PUBLIC_DOMAIN=localhost
PE_SCHEME=http
PE_TLS_ISSUER=none
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
LG_PUBLIC_DOMAIN=localhost
LG_SCHEME=http
LG_LISTEN_SCHEME=http
LG_TLS_ISSUER=none
LG_BIND_HOST=127.0.0.1
LG_HTTP_PORT=18080
LG_HTTPS_PORT=18443
LG_PUBLIC_PORT_SUFFIX=
LG_PLATFORM_NETWORK=platform
```

Observability `.env`:

```sh
OB_PUBLIC_DOMAIN=localhost
OB_SCHEME=http
OB_LISTEN_SCHEME=http
OB_TLS_ISSUER=none
OB_BIND_HOST=127.0.0.1
OB_HTTP_PORT=18180
OB_HTTPS_PORT=18543
OB_PUBLIC_PORT_SUFFIX=
OB_PLATFORM_NETWORK=platform
OB_GATEWAY_HEALTH_HOST=localhost
OB_GATEWAY_URL=http://localhost
OB_BACKPLANE_URL=http://backplane.localhost
```

Backplane `.env` (start core services without its optional `edge` profile):

```sh
BP_PUBLIC_URL=http://backplane.localhost
BP_BIND_HOST=127.0.0.1
BP_PORT=3000
```

## Public integration

Point DNS for the root and five subdomains at the host and open TCP 80/443. ACME requires all six names reachable. For private DNS, change the Edge issuer to `internal` and distribute its public CA root.

Edge `.env`:

```sh
PE_PUBLIC_DOMAIN=example.com
PE_SCHEME=https
PE_TLS_ISSUER=acme
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

Observability `.env`:

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
scripts/smoke.sh                       # disposable edge: HTTP and verified internal TLS
scripts/backup-drill.sh                # prove CA and TLS survive restore; print RTO
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Report vulnerabilities through the [security policy](SECURITY.md). The edge does TLS and routing only; each application does its own login.

## License

[MIT](LICENSE).
