# platform-edge

A shared ingress for a host running several platform stacks: one Caddy, one Compose project, static routes and certificate state. Each stack retains its standalone gateway.

Read before changing anything: `CONTEXT.md` (vocabulary), `docs/DESIGN.md` (the map), `docs/adr/` (decisions and why). A change that contradicts an ADR is declared, never made quietly. For route or port changes, read `docs/operations/ingress.md` for the sibling configuration contract.

## Ways to hurt yourself

1. **Killing by pattern.** Never `pkill -f`, `pgrep | kill`, or kill a PID found by matching a name, path or worktree string. Kill only a PID captured at spawn, or the owner of your port from `ss -H -ltnp` after confirming `/proc/<pid>/cwd` is your worktree.
2. **Touching certificate state.** Never delete installation volumes unless the user asked for data loss by name. Replacing `edge-data` loses certificate keys and internal CA trust. Export only the public CA certificate to clients.
3. **Mutating the running stack to inspect it.** Read files and `docker compose config` to inspect. `up`, `down`, `restart` and `pull` need the user's authorization. Smoke owns only its fresh disposable project and network.
4. **Breaking a sibling's ingress.** Root health and fallback belong to this repo; stack application routes belong to the relevant Route File. Keep Host and scheme forwarding intact. Keep datastores off the Platform Network.

## Communication

Short, direct, precise, industry standard language. State the result, then the evidence. No em-dashes.

## Commits

- Conventional Commits: `<type>(scope): <description>`.
- Use `Co-Authored-By: Various Models`. Do not claim work performed by other models.
- Never commit `.env`, certificate exports, private keys or generated state. Never print secrets.

## Documentation

Update `CONTEXT.md` when a term changes meaning. Add an ADR only for a hard-to-reverse decision with a real trade-off. Keep per-stack env settings in `docs/operations/ingress.md`.

## Plans and scratch

Never commit plans, research notes or agent scratch. `.scratch/`, `.agents/`, `.devloop/` and `.plans/` are gitignored.

## Delegation

For delegated work, read `docs/agents/model-routing.md` for model choices and briefs. At most two concurrent workers share this host's Docker daemon.

## Where things live

- `compose.yaml`: the only service and image pin, networks and certificate volumes.
- `.env.example`: every operator setting, prefixed `PE_`, one comment per assignment, no secrets.
- `Caddyfile`: global policy, issuer snippets and route imports. Its root location is the Edge mount contract.
- `routes.d/`: one Route File per stack. `docker/console/index.html`: one fallback page.
- `scripts/bootstrap.py`: env locking, port checks, network, readiness and the Edge Status Document in `data/console/status.json`.
- `scripts/installation.py`: read-only prerequisite planning for selected stacks.
- `scripts/retire-status-timer.sh`: one-time upgrade step removing the version 1 status timer.
- `scripts/validate.sh`, `scripts/smoke.sh`: static gates and the Smoke Contract.
- `tests/`: Python unittest with a fake runner; no Docker calls.
- `docs/operations/`: runbooks. `docs/conventions.md`: the canonical shared conventions and Platform Contract; `scripts/sync-conventions.sh` vendors it into siblings.

Only Caddy publishes ports and joins the external Platform Network, as `pe-edge`. There is no default network. List the service environment explicitly. Service names, volume names and Upstream Aliases are interfaces: renaming one needs a migration.

## Taste

Compose is the product. Keep logic in upstream apps and their config. Python standard library and POSIX shell, no build step. Make the smallest coherent change. Comments explain constraints. The fallback page uses system fonts and the gateway palette; checks update without continuous animation.

## Finish

Run `scripts/validate.sh`, `python3 -m unittest discover -s tests`, and `scripts/smoke.sh` for image, config or bootstrap changes. Report exact commands and counts, limitations, operator actions and every spec deviation. A failed or unverified gate is never a pass.
