# Tailscale setup

Private access to every application from your own devices, without exposing anything to
the internet. Each routed hostname runs as its own Tailscale node inside the Edge project
and answers at `https://<name>.<tailnet>.ts.net` with a certificate issued by Tailscale.
Who can reach those names is decided by your tailnet policy, not by Edge. No host
`tailscale serve`, no sudo, no ports, no certificate installed on clients. One trade-off:
Tailscale certificates put the node names in public Certificate Transparency logs, so keep
the names generic (the defaults are) rather than descriptive of anything private. The
full reference is the [ingress runbook](ingress.md#access-everything-through-tailscale);
this page is the short path.

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
   `src`, for example `"src": ["autogroup:member", "tag:phones"]`. Application login is
   still required behind this rule.
3. **Keys** (`https://login.tailscale.com/admin/settings/keys`): Generate auth key.
   Reusable on, Ephemeral off, Tags: the tag from step 2. Copy the key; it starts with
   `tskey-auth-`.

## 2. Configure and start

In the Edge checkout, set these lines in `.env` (mode 0600, never committed):

```sh
PE_ACCESS_MODE=local
PE_BIND_HOST=127.0.0.1
PE_TS_AUTHKEY=tskey-auth-...
PE_TS_TAG=tag:platform
PE_TS_APPS=console,litellm,langfuse,s3,rustfs,backplane,grafana
```

Remove from `PE_TS_APPS` the names of stacks you do not run. Then:

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

## Day two

- **Add or remove an application:** edit `PE_TS_APPS`, rerun `bootstrap.py --tailscale`.
  Removing also needs `docker compose --profile ts-<name> rm -sf ts-<name>` and deleting
  the machine in the admin console.
- **Rotate the key:** create a new key, replace `PE_TS_AUTHKEY`, revoke the old one.
  Enrolled nodes keep working; the key is only used for new enrollments.
- **Turn Tailscale off:** remove the nodes as above, remove `compose.tailscale.yaml` from
  `COMPOSE_FILE` and the `ts-` entries from `COMPOSE_PROFILES`, clear `PE_TAILNET_DOMAIN`,
  rerun `bootstrap.py --with ...` so the siblings return to their public-domain origins.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| A device cannot resolve or reach `*.ts.net` names, other devices can | The access rule's `src` does not cover that device. Tagged devices are not `autogroup:member`; add their tag. |
| `tailscale_name_taken` | Another machine already uses that name; rename or delete it in the admin console, delete the new `<name>-1` machine, rerun. |
| A node never reaches `Running` | The key expired or its tag is not owned by you. Check `docker compose logs ts-<name>`, mint a new key, rerun. |
| Probes skipped | This host does not resolve MagicDNS names to Tailscale addresses (for example it is itself a tagged device not covered by the rule). Verify from another device. |
| Certificate warning on first load | The node is still fetching its certificate; reload after a few seconds. |
