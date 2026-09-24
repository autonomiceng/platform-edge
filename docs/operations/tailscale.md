# Tailscale setup

Private access to every application from your own devices, without exposing anything to
the internet. Each routed hostname runs as its own Tailscale node inside the Edge project
and answers at `https://<name>.<tailnet>.ts.net` with a certificate issued by Tailscale.
Who can reach those names is decided by your tailnet policy, not by Edge. No host
`tailscale serve`, no sudo, no ports, no certificate installed on clients. One trade-off:
Tailscale certificates put the node names in public Certificate Transparency logs, so keep
the names generic (the defaults are) rather than descriptive of anything private. The
design and its consequences are in [ADR-0004](../adr/0004-tailscale-sidecars.md).

- [Prepare the tailnet](#1-prepare-the-tailnet-once-in-the-admin-console)
- [Configure and start](#2-configure-and-start)
- [Verify](#3-verify)
- [What bootstrap does](#what-bootstrap-does)
- [How a request flows](#how-a-request-flows)
- [Day two](#day-two)
- [RustFS consoles](#rustfs-consoles)
- [Troubleshooting](#troubleshooting)

You get seven names, one per application, under your tailnet domain:

| Name | Application |
| --- | --- |
| `platform` | Edge console (the landing page with links to everything) |
| `litellm` | LiteLLM |
| `langfuse` | Langfuse |
| `s3` | S3 API (RustFS) |
| `rustfs` | RustFS admin console |
| `backplane` | Agent Backplane |
| `grafana` | Grafana |

## 1. Prepare the tailnet (once, in the admin console)

1. **DNS** (`https://login.tailscale.com/admin/dns`): MagicDNS on, HTTPS certificates on.
2. **Access controls** (`https://login.tailscale.com/admin/acls/file`): add a tag for the
   platform nodes and a rule that lets your devices reach it on port 443. Pick any tag name;
   the examples use `tag:platform`.

   ```json
   "tagOwners": { "tag:platform": ["autogroup:admin"] },
   "grants": [
     { "src": ["autogroup:member"], "dst": ["tag:platform"], "ip": ["443"] }
   ]
   ```

   Read the rule as "from my user-owned devices, to the platform nodes, HTTPS only".
   `autogroup:member` covers devices logged in as a user. Devices that carry a tag
   themselves (a server, a phone enrolled with a tag) are not members: add their tags to
   `src`, for example `"src": ["autogroup:member", "tag:phones"]`. Every node shares the
   tag, so this rule grants every application at once; application login is still required
   behind it.
3. **Keys** (`https://login.tailscale.com/admin/settings/keys`): Generate auth key.
   Reusable on, Ephemeral off, Tags: the tag from step 2. Copy the key; it starts with
   `tskey-auth-`. Tagged nodes have no key expiry of their own; the key's expiry only
   limits new enrollments.

## 2. Configure and start

In the Edge checkout, set these lines in `.env` (mode 0600, never committed):

```sh
PE_ACCESS_MODE=local
PE_BIND_HOST=127.0.0.1
PE_TS_AUTHKEY=tskey-auth-...
PE_TS_TAG=tag:platform
PE_TS_APPS=console,litellm,langfuse,s3,rustfs,backplane,grafana
PE_ROOT_HOST=
PE_TAILNET_DOMAIN=
```

Remove from `PE_TS_APPS` the names of stacks you do not run. `PE_TS_TAG` must be one of
the key's tags; an empty value omits `--advertise-tags` so the key's own tags apply.
`PE_ROOT_HOST` names the console's node (`platform` when empty). Leave
`PE_TAILNET_DOMAIN` empty on the first run; bootstrap records it. `--tailscale` needs
`local` or `proxy` mode with the loopback bind, because the Tailnet hosts are plain-HTTP
sites on Edge's listener. Then:

```sh
python3 scripts/bootstrap.py --tailscale --dry-run
python3 scripts/bootstrap.py --tailscale --with gateway --with observability --with backplane \
  --capability-file /path/to/backplane-enrollment
```

Drop the `--with` entries for stacks you do not run. The first command prints the plan.
The second enrolls the nodes, records your tailnet domain in `PE_TAILNET_DOMAIN`, restarts
Edge with the Tailnet routes, and writes each application's new browser origin into the
sibling stacks before running their bootstraps. The result lists the origins.

## 3. Verify

From a device covered by the access rule, open `https://platform.<tailnet>.ts.net/` and
follow the application links. The first request to each name can take a few seconds while
the node fetches its certificate. In the admin console the selected machines (up to
seven) appear under your tag.

## What bootstrap does

With `--tailscale`, bootstrap refuses without the key (`tailscale_auth_key`), creates one
external volume per node (`${PE_VOLUME_PREFIX}_ts-<name>`, the node's identity), starts the
nodes and waits up to 120 s until every node reports `Running` under its expected MagicDNS
name. A name already taken on the tailnet enrolls as `<name>-1` and is refused
(`tailscale_name_taken`). It then records the tailnet domain in `PE_TAILNET_DOMAIN` and the
selection in `.env` (`COMPOSE_FILE` gains `compose.tailscale.yaml`, `COMPOSE_PROFILES` gains
one `ts-<name>` per selected node; other files and profiles are untouched; a failed
enrollment records nothing), starts Edge with the overlay so the Tailnet hosts are routed,
and probes `https://<name>.<tailnet>.ts.net/health` for every node from this host with the
system trust store. When MagicDNS on this host does not resolve the names to Tailscale
addresses, the probe is skipped with a message; verify from a tailnet member instead.
Without `--with`, the result lists the sibling settings to set by hand.

Because the selection is recorded, plain `docker compose up` and ordinary `bootstrap.py`
reruns keep the nodes and Edge's Tailnet routes; only `--tailscale` enrolls, re-selects
after a `PE_TS_APPS` change, and probes. Bootstrap refuses a recorded selection unless the
mode is `local` or `proxy` and `PE_BIND_HOST` is `127.0.0.1`; plain Compose enforces
nothing, so never change `PE_BIND_HOST` while the selection is recorded without running
bootstrap, which would otherwise publish the plain-HTTP Tailnet hosts on that interface. A
recorded selection without a recorded domain is refused (`tailnet_not_enrolled`) until
`--tailscale` completes; a recorded domain that differs from the enrolled one is refused
(`tailscale_domain_mismatch`). Reruns are idempotent: enrolled nodes stay enrolled
(`TS_AUTH_ONCE`).

The origins are the applications' browser URLs, and the bundle owns the settings that hold
them: `LG_CONSOLE_URL`, `LG_LITELLM_URL`, `LG_LANGFUSE_URL`, `LG_S3_URL`, `LG_RUSTFS_URL`,
`OB_GRAFANA_URL`, `OB_GATEWAY_URL`, `OB_BACKPLANE_URL` and `BP_PUBLIC_URL`. While the
selection is recorded, every `--with` run writes the Tailnet Origin of each selected node
into them; a key whose node is not selected, or any run after the selection is removed,
gets the public-domain origin or an empty value the stack derives itself. The public
hostnames keep working beside the Tailnet Origins.

## How a request flows

The node terminates TLS, keeps the original Host and proxies to `pe-edge:80` over the
Platform Network (`docker/tailscale/serve.json` is the one serve config every node uses).
Edge routes the Tailnet host to the same stack as the public hostname and sets
`X-Forwarded-Proto: https` because the request comes from the Platform Network's dynamic
range; the same Host from the loopback listener keeps its own scheme, and no forwarded
header from any client is trusted. Applications therefore see the node's Platform Network
address as the client, not the tailnet member; Tailscale's identity headers pass through
unverified and must not be trusted behind Edge. Every node can reach every Tailnet host
through Edge, so the nodes form one authorization domain: the tailnet policy grants the tag
as a whole.

With the selection recorded in `.env`, plain Compose commands see the nodes:

```sh
docker compose logs ts-litellm
```

## Day two

- **Add or remove an application:** edit `PE_TS_APPS`, rerun `bootstrap.py --tailscale`
  (it rewrites `COMPOSE_PROFILES`). Removing also needs
  `docker compose --profile ts-<name> rm -sf ts-<name>` and deleting the machine in the
  admin console; delete the volume `${PE_VOLUME_PREFIX}_ts-<name>` only if you want the
  identity gone. Restoring the name and rerunning reuses a kept volume's identity; a machine
  deleted in the admin console enrolls again, so `PE_TS_AUTHKEY` must still be set (every
  `--tailscale` run requires it).
- **Rotate the key:** create a new key, replace `PE_TS_AUTHKEY`, revoke the old one.
  Enrolled nodes keep working; the key is used only when a node has no identity yet.
- **Turn Tailscale off:** remove the nodes as above, remove `compose.tailscale.yaml` from
  `COMPOSE_FILE` and the `ts-` entries from `COMPOSE_PROFILES`, clear `PE_TAILNET_DOMAIN`,
  rerun `bootstrap.py --with ...` so the siblings return to their public-domain origins.

## RustFS consoles

The optional RustFS consoles of Backplane and Observability are not Tailnet Origins. For a
session, reach them over loopback through an SSH tunnel; never add RustFS to the Platform
Network.

Observability: the console exists only on an installation with the `s3` storage profile
(bootstrap refuses `rustfs_console_requires_s3` otherwise). Its client allowlist
`OB_RUSTFS_CONSOLE_ALLOW` defaults to loopback, and a connection to the published loopback
port reaches its Caddy from that project's Docker network gateway, so allow that one
address for the session. In the Observability checkout: set `OB_RUSTFS_CONSOLE=true`, add
the gateway printed by
`docker network inspect observability-stack_default --format '{{(index .IPAM.Config 0).Gateway}}'`
as a `/32` to `OB_RUSTFS_CONSOLE_ALLOW`, run its bootstrap, then
`ssh -L 18180:127.0.0.1:18180 <host>`, add a hosts entry mapping `rustfs.<domain>` (or the
configured `OB_RUSTFS_HOST`) to `127.0.0.1`, and open
`http://rustfs.<domain>:18180/rustfs/console/`; its gateway routes the console by that
hostname, so an IP URL does not reach it. Remove the address and rerun its bootstrap
afterwards. Details in Observability's ingress runbook.

Backplane: RustFS sits on its internal blob network only and publishes no port. Follow
"Native RustFS console" in Backplane's `docs/operations/ingress.md`: read the container's
address with `docker inspect`, then `ssh -L 9001:<address>:9001 <host>` and open
`http://localhost:9001/rustfs/console/`.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| A device cannot resolve or reach `*.ts.net` names, other devices can | The access rule's `src` does not cover that device. Tagged devices are not `autogroup:member`; add their tag. |
| `tailscale_name_taken` | Another machine already uses that name; rename or delete it in the admin console, delete the new `<name>-1` machine, rerun. |
| A node never reaches `Running` | The key expired or its tag is not owned by you. Check `docker compose logs ts-<name>`, mint a new key, rerun. |
| `tailnet_not_enrolled` | The selection is recorded but no domain is; run `bootstrap.py --tailscale` to finish enrollment, or remove the selection as in "Turn Tailscale off". |
| Probes skipped | This host does not resolve MagicDNS names to Tailscale addresses (for example it is itself a tagged device not covered by the rule). Verify from another device. |
| Certificate warning on first load | The node is still fetching its certificate; reload after a few seconds. |
