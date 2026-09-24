# platform-edge

One Caddy for ports 80 and 443 when several stacks share a host. It gets the certificates and sends each hostname to the right stack.

[![CI](https://github.com/autonomiceng/platform-edge/actions/workflows/ci.yml/badge.svg)](https://github.com/autonomiceng/platform-edge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Caddy 2.11](https://img.shields.io/badge/Caddy-2.11-1F88C0)](https://github.com/caddyserver/caddy)

- [What it is](#what-it-is)
- [Quick start](#quick-start)
- [Access modes](#access-modes)
- [What's inside](#whats-inside)
- [Upgrade](#upgrade)
- [Day two](#day-two)
- [The other stacks](#the-other-stacks)
- [Development](#development)
- [Security](#security)
- [License](#license)

## What it is

The LLM gateway, the agent backplane and the observability stack each ship their own Caddy and each want port 80. On a host that runs more than one of them, this project takes the ports instead. It handles HTTPS once and forwards each hostname over the shared `platform` Docker network to the stack that owns it, with the original Host and scheme. The root hostname serves a console with links, health and configured versions for every installed stack.

Every stack still works on its own without it. Add the edge when you add the second stack.

## Quick start

You need a Linux Docker host with journald, Compose 2.24.4 or newer, and Python 3.11 or newer. Clone the sibling stacks next to this checkout (`../llm-gateway-stack`, `../observability-stack`, `../agent-backplane`), or pass `--<stack>-dir`.

```sh
git clone https://github.com/autonomiceng/platform-edge.git && cd platform-edge
python3 scripts/bootstrap.py --with gateway --with observability --with backplane \
  --capability-file ~/private/backplane-enrollment
```

Drop the `--with` entries for stacks you do not run (`--capability-file` belongs to `--with backplane`). Bootstrap writes `.env` from `.env.example`, creates the shared network with the contract's allocation (or validates an existing one), creates the certificate volumes, checks port conflicts, starts Caddy and verifies both loopback listeners including HTTPS trust. Then, for each selected stack in turn, it writes the [bundle settings](docs/operations/ingress.md#bundle-settings-per-stack) into that stack's `.env` and runs that stack's own bootstrap. Add `--dry-run` first to see the plan without writing anything.

Set each stack's own required settings (`LG_BACKUP_DIR`, `LANGFUSE_INIT_USER_EMAIL`, `BP_BACKUP_DIR`, the Observability alert destination) in its `.env` before or after; the bundle never touches them. If an installed stack already owns port 80 or 443, Edge refuses to start (`port_conflict`): first move that stack's gateway to a spare loopback port with its own bootstrap, then rerun.

The defaults are Local Mode on `localhost`: open `http://localhost/` for the console, `http://litellm.localhost/`, `http://langfuse.localhost/`, `http://grafana.localhost/` and `http://backplane.localhost/` for the applications. HTTPS works too once you install Edge's public CA root ([trusting local certificates](docs/operations/ingress.md#trusting-local-https-certificates)).

## Access modes

`PE_ACCESS_MODE` in `.env` selects how the host is reached. Rerun the bundle command after changing it; the siblings receive the matching origins. Details in the [ingress runbook](docs/operations/ingress.md).

| You want | Settings | Read |
| --- | --- | --- |
| Localhost only (default) | `PE_ACCESS_MODE=local`; HTTP and self-signed HTTPS on loopback, no redirects | [Local Mode](docs/operations/ingress.md#local-mode-default) |
| Private access from your devices over Tailscale | `PE_TS_AUTHKEY` plus `python3 scripts/bootstrap.py --tailscale --with ...`; one Tailscale node per hostname, `https://<name>.<tailnet>.ts.net` | [Tailscale setup](docs/operations/tailscale.md) |
| Public hostnames with Let's Encrypt | `PE_ACCESS_MODE=public`, `PE_PUBLIC_DOMAIN`, `PE_BIND_HOST=0.0.0.0`, `PE_ACME_EMAIL` | [Public Mode](docs/operations/ingress.md#public-mode) |
| Corporate CA or certificate files | `PE_TLS_ISSUER=acme` with `PE_ACME_CA`, or `PE_TLS_ISSUER=files` with `PE_TLS_DIR` | [Corporate certificates](docs/operations/ingress.md#corporate-certificates-and-private-acme) |
| Behind another HTTPS gateway | `PE_ACCESS_MODE=proxy`; Edge publishes HTTP only | [Proxy Mode](docs/operations/ingress.md#proxy-mode) |

Runtime logs go to Linux journald; see [runtime logs](docs/operations/ingress.md#runtime-logs).

## What's inside

| Service | Job | Data |
| --- | --- | --- |
| Caddy | TLS, hostname routing, a health path, the project console | `edge-data` (certificates and CA keys) and `edge-config` volumes |
| Tailscale nodes (optional, `--tailscale`) | One HTTPS origin per hostname on your tailnet | one `ts-<name>` volume per node |

Routes live in `routes.d/`, one file per stack:

| Hostname | Upstream |
| --- | --- |
| `<domain>` | Edge console at `/`; other paths go to `lg-gateway:80` |
| `litellm.`, `langfuse.`, `s3.`, `rustfs.` | `lg-gateway:80` |
| `backplane.` | `bp-server:3000` |
| `grafana.` | `ob-gateway:80` |

The validated default image is pinned as `tag@sha256` in `compose.yaml`. Set `PE_CADDY_IMAGE` in `.env` to a complete image reference for a local experiment; see [image overrides](docs/operations/ingress.md#image-overrides).

## Upgrade

```sh
scripts/backup.sh          # Checkpoint of the certificate volumes
git pull
docker compose pull
python3 scripts/bootstrap.py --with gateway --with observability --with backplane \
  --capability-file ~/private/backplane-enrollment
```

Bootstrap recreates what changed and waits for readiness; the siblings' own upgrade steps are in their READMEs. Route changes in `routes.d/` apply on the next `docker compose restart caddy` when the container was not recreated.

## Day two

- [Ingress: modes, certificates, bundle settings, metrics, logs](docs/operations/ingress.md)
- [Tailscale setup](docs/operations/tailscale.md)
- [Backup, restore, CA preservation and RPO/RTO](docs/operations/backup.md)
- [Public stack status contract](docs/operations/status-contract.md)
- [Design](docs/DESIGN.md), [vocabulary](CONTEXT.md), [decisions](docs/adr/)

## The other stacks

- [llm-gateway-stack](https://github.com/autonomiceng/llm-gateway-stack): LiteLLM and Langfuse.
- [agent-backplane](https://github.com/autonomiceng/agent-backplane): shared state, queues and approvals for agents.
- [observability-stack](https://github.com/autonomiceng/observability-stack): Grafana, Loki, Tempo and Mimir.

All four deploy the same way. Shared conventions and the [platform contract](docs/conventions.md#platform-contract) (network, ingress, TLS, status and bootstrap interfaces) live in [docs/conventions.md](docs/conventions.md); it is canonical here and vendored into the siblings with `scripts/sync-conventions.sh`.

## Development

```sh
scripts/validate.sh                    # static checks, what CI runs on every push
python3 -m unittest discover -s tests  # unit tests, no Docker
node --test tests/status*.test.cjs     # status consumer tests
scripts/smoke.sh                       # disposable edge with stub upstreams: routing, HTTPS, hardening
scripts/backup-drill.sh                # prove CA and TLS survive restore; print RTO
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Report vulnerabilities through the [security policy](SECURITY.md). The edge does TLS and routing only; each application does its own login.

## License

[MIT](LICENSE).
