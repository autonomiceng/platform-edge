# Edge status observation

Edge serves the [public status contract](status-contract.md) at `/status.json` and
`/stack-status/edge`. A host process observes Caddy; the gateway reads only a public
JSON file. Caddy never receives the Docker socket. This metadata is public in every
access mode and contains no paths, references to private registries, credentials,
command output or diagnostic messages.

Run an observation explicitly from the selected checkout:

```sh
python3 scripts/status_observer.py --checkout "$PWD" --env-file "$PWD/.env"
```

The observer uses that checkout and env file, not shell `PE_*` or `COMPOSE_*`
overrides. Put persistent deployment settings, including any custom
`COMPOSE_PROJECT_NAME`, in the selected env file. Docker connection settings and
the current user's Docker permissions still apply. Different checkouts publish
separately; one checkout supports one selected installation. Configuration failure
produces unknown configuration, never an assertion that a component is disabled.

Successful bootstrap attempts to record its execution and the initial observation.
Recording failure produces a fixed warning and preserves bootstrap's real result.
Safe ownership and permissions are required for `data/` and its status directories;
group-writable existing directories can leave execution records unknown.
Before running Compose directly, create the public mount source as the deployment user
with `install -d -m 0755 data/console`. If Compose created it as root, stop the attempted
installation, verify that it contains only generated status files, then restore ownership
with `sudo chown -R "$(id -u):$(id -g)" data/console` before retrying.
`--probe-only` updates observations without claiming a new bootstrap execution.
For regular refreshes, explicitly install the user timer:

```sh
python3 scripts/install_status_timer.py --checkout "$PWD" --env-file "$PWD/.env" --install
systemctl --user status platform-edge-status.timer
```

The timer runs about every 30 seconds, requires an active user manager and ordinary
Docker access, and does not enable lingering. Existing units are refused. Installation
failure after activation starts retains both unit files because `systemctl enable --now`
can partially succeed. Run `systemctl --user disable --now platform-edge-status.timer`,
inspect and remove `platform-edge-status.service` and `.timer` from the user unit directory,
then run `systemctl --user daemon-reload` before retrying. Use the same disable-first
sequence to select another checkout. No stack restart is performed by the observer or
timer. Merely fetching status does not run an observation.

| Component | Evidence and limits |
| --- | --- |
| Caddy | Inspect the selected Compose project's single service container, then execute a bounded request to its internal `http://127.0.0.1/health`. This proves its own HTTP readiness, not sibling application readiness, public TLS or external routing. Docker health labels alone never establish health. |
| Caddy version | Execute `caddy version` independently of readiness. The configured version/digest and observed image ID are distinct fields. A custom image without these tools remains unknown. |
| Bootstrap | Inspect the private execution record for this env file. Its original execution timestamp is preserved; inspection does not claim a new execution. Older installations without a record remain unknown. |
| Telemetry | Unknown. Edge does not inspect another stack's Alloy or infer successful collection from its own logging configuration. |

Each command has a byte and time bound. Configuration and component observations
expire after 120 seconds. A stopped timer or frozen file therefore becomes stale
in the console even if it remains HTTP-accessible. A failed probe cannot renew an
old success. An empty project inventory reports unknown because the selected env
file may name a different project from a shell-only deployment override. An
inaccessible Docker daemon also reports unknown. Paused containers are unknown.

The observer atomically replaces `data/console/status.json` after checking file
ownership and rejecting symlinks/hard links. Caddy mounts this directory read-only.
Private bootstrap records live in `data/status/`, outside that mount. Missing
public files return an empty JSON 404; unsupported methods return 405 with `Allow`.
These generated observations are not certificate checkpoint state. After recovery,
run a fresh observation; do not restore old health as current evidence.
