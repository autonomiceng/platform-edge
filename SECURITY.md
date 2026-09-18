# Security policy

## Supported versions

Security fixes are applied to the default branch. The Caddy image is pinned as tag plus digest in `compose.yaml`; Renovate proposes updates for review after smoke passes.

## Reporting a vulnerability

Report privately through a GitHub Security Advisory for this repository. If advisories are unavailable, use the private contact route on the owner or maintainer GitHub profile. Keep exploit details out of public issues.

Include the impact, reproduction steps and affected revision or Caddy pin. We will aim to acknowledge receipt and coordinate a fix timeline.

## Hardening reminders

Local Mode binds only to loopback. Public Mode exposes every configured hostname; application authentication and authorization remain the stack's responsibility. The Edge adds no authentication or rate limiting. Keep the Platform Network restricted to trusted stacks: the outermost Edge does not trust forwarded client headers. The Edge overwrites the upstream Host and scheme headers on each application route.

The container has no Docker socket or admin API. Bootstrap generates no secrets, but `edge-data` holds TLS and internal CA private keys. Protect volume backups and distribute only the exported public root certificate. See [ingress operations](docs/operations/ingress.md) for DNS, port ownership and CA export.
