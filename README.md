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

Choose the local or public Edge settings below, then run `python3 scripts/bootstrap.py` here; add `--with` for each sibling stack to install behind it (below). If an existing stack already owns ports 80 or 443, first move its gateway to spare loopback ports.
Bootstrap creates the shared network with the platform contract's allocation (or validates an existing one), creates external certificate volumes, checks port
conflicts, starts Caddy and verifies both local listeners, including HTTPS certificate trust. No sibling stack is required for Edge readiness.

## Local integration

Local Mode serves HTTP on 80 and self-signed HTTPS on 443, without redirecting HTTP or telling browsers to require HTTPS. Sibling ingresses use HTTP internally. A missing stack returns 502 on its hostnames while other stacks continue working. The root always serves the Edge project console, including when Gateway is absent.

The shared setup below uses HTTPS application URLs for browser login. Install Edge’s
public root certificate on clients once; HTTP remains available for health checks and
transport access. A standalone stack can still start with its own HTTP defaults.

Edge `.env`:

```sh
PE_ACCESS_MODE=local
PE_PUBLIC_DOMAIN=localhost
PE_SCHEME=https
PE_BIND_HOST=127.0.0.1
PE_HTTP_PORT=80
PE_HTTPS_PORT=443
PE_PLATFORM_NETWORK=platform
PE_ACME_EMAIL=
PE_VOLUME_PREFIX=platform-edge
PE_BACKUP_DIR=./backups
```

Then install the stacks behind it with one command. It writes the
[per-stack settings](docs/operations/ingress.md#per-stack-settings-behind-the-edge) into each
sibling `.env` (`../llm-gateway-stack`, `../observability-stack` and `../agent-backplane` by
default) and runs each stack's own bootstrap; add `--dry-run` to see the plan first:

```sh
python3 scripts/bootstrap.py --with gateway --with observability --with backplane \
  --capability-file ~/private/backplane-enrollment
```

Set each stack's own required settings (`LG_BACKUP_DIR`, `LANGFUSE_INIT_USER_EMAIL`,
`BP_BACKUP_DIR`, the Observability alert destination) in its `.env` first; the bundle never
touches them. See [Install the bundle](docs/operations/ingress.md#install-the-bundle).

The console also opens at `http://127.0.0.1`; verified direct HTTPS requires installing
Edge's public CA root. For private access from other computers through Tailscale, put a
reusable tagged auth key in `.env` as `PE_TS_AUTHKEY` and run:

```sh
python3 scripts/bootstrap.py --tailscale --with gateway --with observability --with backplane \
  --capability-file ~/private/backplane-enrollment
```

This starts one Tailscale node per hostname inside the Edge project and gives each
application its own `https://<name>.<tailnet>.ts.net` origin with a Tailscale-issued
certificate: no host `tailscale serve`, no sudo, no port table, no client CA install.
Localhost HTTP and self-signed HTTPS remain available. See the
[Tailscale setup](docs/operations/tailscale.md) for the short path, and the
[ingress guide](docs/operations/ingress.md#access-everything-through-tailscale) for the full reference.

Runtime logs go to stdout/stderr and Docker journald, without Docker log files or cache.
Alloy collection is optional; `docker compose logs -f caddy` works without observability.
Host journal persistence remains the operator's choice. Fresh installs provide HTTP and self-signed HTTPS. Choose public mode for your own domain, or proxy mode when another gateway handles HTTPS.

## Public integration

Point DNS for the root and six subdomains at the host and open TCP 80/443. Certificate issuance requires all seven names to be reachable. For private DNS, select `PE_ACCESS_MODE=local` with the private domain and distribute its public CA root. A private ACME CA or certificate files from your own PKI: set `PE_TLS_ISSUER` as described in [corporate certificates and private ACME](docs/operations/ingress.md#corporate-certificates-and-private-acme).

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

Then run the same bundle command as in the local setup. It derives `example.com` and the
HTTPS origins from the Edge `.env`, writes the
[per-stack settings](docs/operations/ingress.md#per-stack-settings-behind-the-edge) and runs
each stack's bootstrap. Edge reaches the Backplane server directly at `bp-server:3000`; its
`edge` profile stays off and its `gateway` profile is needed only by Backplane checkouts that
still ship it.
The seven routed hosts are root, `litellm.`, `langfuse.`, `s3.`, `rustfs.`, `backplane.`
and `grafana.` under the configured domain. Run shared-host acceptance after siblings
are ready: `SMOKE_INTEGRATION=1 SMOKE_DOMAIN=example.com scripts/smoke.sh` (use
`localhost` for Local Mode). A missing-sibling skip is not a pass.

## What's inside

| Service | Job | Data |
| --- | --- | --- |
| Caddy | TLS, hostname routing, a health path, a project console | `edge-data` (certificates and CA keys) and `edge-config` volumes |

Routes live in `routes.d/`, one file per stack. The validated default image is pinned as `tag@sha256` in `compose.yaml`.
Set `PE_CADDY_IMAGE` in `.env` to a complete image reference for an unvalidated local
experiment; empty or unset keeps that default, including with bare `docker compose`.
See [image overrides](docs/operations/ingress.md#image-overrides) for validation and Checkpoint limits.

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
The [platform contract](docs/conventions.md#platform-contract) in that file fixes the network, ingress, TLS, status and bootstrap interfaces the four stacks share; it is canonical here and vendored into the siblings with `scripts/sync-conventions.sh`.

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
