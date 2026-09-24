# Certificate state and Checkpoints

What the two Edge volumes hold, how to capture and restore them, and what the drill proves.

- [Prerequisites](#prerequisites)
- [Existing installations](#existing-installations)
- [Capture and restore](#capture-and-restore)
- [RPO and RTO](#rpo-and-rto)
- [Verification and troubleshooting](#verification-and-troubleshooting)

`edge-data` holds public certificate account keys, server certificate keys and the local certificate authority. `edge-config`
holds Caddy configuration state and the certificate metric. Both are external Docker
volumes named `${PE_VOLUME_PREFIX}_edge-data` and `${PE_VOLUME_PREFIX}_edge-config`.
The prefix defaults to `platform-edge`, independently of the Compose project name.
Bootstrap creates them. Changing the prefix selects another installation's state.

**Do not use `docker compose down -v` as a retirement procedure.** External volumes
survive it, but old Compose revisions still delete their managed volumes. Preserve the
matching checkout and configuration. Deliberate retirement uses `scripts/destroy.sh`:
it displays the project and exact volumes and requires typing the project name. It
stops that project and removes only those volumes, never the shared network. Run it
only after explicitly deciding to lose that installation's certificate state.

## Prerequisites

- Docker, Compose 2.24.4 or newer, Python 3.11 or newer, Git, and the pinned Caddy image
  pulled (`docker compose pull`).
- Run as the checkout owner: backup resolves the Git commit and dirty status first and
  refuses a checkout without Git metadata.
- `PE_BACKUP_DIR` on a mounted, protected filesystem with more free space than the two
  volumes; encryption at rest and off-host replication are yours to arrange.
- Exactly one Edge Caddy container for the project (use `stop`, not `down`, before an
  offline capture); the configured image reference pinned by digest.

## Existing installations

Earlier revisions let Compose manage `<project>_edge-data` and `<project>_edge-config`.
With the default project and prefix those are the same names, so bootstrap adopts the
existing volumes in place and the CA does not change. Verify: after `scripts/bootstrap.py`
the reported certificate fingerprint must match the one recorded before the upgrade.
If the project name differs from the prefix, copy the old volumes into the new names
before bootstrap, using the same fingerprint check:

```sh
docker compose stop caddy
image=$(docker compose config --images)
for suffix in edge-data edge-config; do
  docker volume inspect "<old-project>_$suffix" >/dev/null || exit 1
  docker volume create "<prefix>_$suffix" >/dev/null || exit 1
  docker run --rm --network none --entrypoint sh \
    --mount "type=volume,src=<old-project>_$suffix,dst=/old,readonly" \
    --mount "type=volume,src=<prefix>_$suffix,dst=/new" \
    "$image" -ec 'test -z "$(ls -A /new)"; cp -a /old/. /new/' || exit 1
done
python3 scripts/bootstrap.py
scripts/backup.sh
```

Retain the old volumes until a restore drill has passed; never run the old revision's
`down -v`.

## Capture and restore

`PE_BACKUP_DIR` selects the backup repository (relative paths resolve against this
checkout; default `./backups`; created if absent) and `PE_BACKUP_KEEP` (default `7`, minimum
`1`) how many complete Checkpoints a successful capture keeps. If the repository is a
mounted filesystem, verify the mount before each run: the script cannot distinguish a
missing mount from an ordinary directory. Encrypt at rest, replicate
off-host over encrypted transport, and verify the replica. The tools do not implement
encryption or replication. Checkpoints contain **private keys**, even though their
manifests contain no secrets. New directories/files use 0700/0600 permissions.

```sh
scripts/backup.sh
scripts/restore.sh /mnt/edge-backups/20260917T020000000000Z
```

Both accept `--env-file /path/to/.env`. They lock the env inode against bootstrap and
the backup repository against simultaneous captures/restores. Do not run other Compose
operations concurrently. Keep the env file and matching checkout separately in secure
configuration storage. Never restore archives from an untrusted source: hashes detect
corruption, not a malicious replacement of both artifacts and manifest.

Backup resolves the Git commit and dirty status before creating a capture directory or
stopping Caddy. Run it as the checkout owner with Git installed; a source-only tarball
without Git metadata is refused before any outage. For another backup account, arrange
Git ownership/trust explicitly for this checkout before scheduling it.

Checkpoint capture and restore require an effective image reference pinned by digest,
including when `PE_CADDY_IMAGE` overrides the default. Tag-only references and local
image IDs are refused before capture stops Caddy or restore writes volumes. To checkpoint
an experiment, publish the image to a registry and configure its complete `name@sha256:...`
reference first. Retain that image and the matching configuration for recovery; these
tools do not save image layers or resolve mutable tags automatically.

Backup compares the existing container's immutable image ID with the locally available
configured digest before stopping it. Helpers and the version-1 manifest use that
effective digest reference. Restore requires the same reference in the target configuration,
so existing digest-pinned Checkpoints remain compatible. An unavailable configured image
fails preflight without stopping Caddy.

Capture requires exactly one existing Caddy container whose image and mounts it checks;
use `stop`, not `down`, before an offline capture. Restore also requires the configured
image locally before creating volumes. To restore an older Checkpoint after an image
bump, set `PE_CADDY_IMAGE` to its `manifest.json` Caddy reference and pull that image first.

Backup stops Caddy, verifies neither volume has a running consumer, and streams a tar
of each volume. Before the outage it checks both volumes for restore markers and
refuses an incomplete restore or a failed state check. There is a brief ingress outage.
It resumes Caddy if it was running,
including on errors or catchable interruptions. SIGTERM, SIGHUP and SIGINT are deferred
until an active transfer and service resumption finish. For up to 300 seconds, resumption
retries start, HTTP or TLS readiness, and a final running check together. Readiness retry
windows and resumption CLI timeouts are capped at 20 seconds and the remaining budget.
Other Checkpoint commands have a 120-second child execution deadline, allowing the
normal 30-second Caddy stop grace period. Each archive capture or extraction has a
separate 1800-second (30-minute) deadline. On timeout the tool sends SIGKILL only to
the process group created for that child, then waits to reap the child before proceeding
to resumption. This includes Compose plugin children that may hold pipes open.
Timeouts fail the operation, preserve incomplete evidence, and store captured stderr
in the protected diagnostics directory.

To abort a backup, send SIGTERM, SIGHUP or SIGINT to its main process. The signal is
recorded while the current stop, pre-transfer checks or transfer completes or times
out; it prevents further archive transfers and does not interrupt resumption. Repeated
signals do not shorten those deadlines. An active transfer can therefore take up to
30 minutes to release the child, followed by the five-minute resumption retry budget.
These are child execution limits, not guarantees against kernel-level I/O stalls or
blocked filesystem sync. Killing a Docker CLI also does not cancel an already dispatched
daemon request or guarantee removal of its helper container. After a timeout, verify
Caddy and inspect any remaining helper containers before retrying; preserve partial
restore volumes and their markers.

Failure to resume fails capture and leaves its evidence incomplete. Hashing and
archive inspection run after resumption. A forced kill or host loss cannot run
cleanup; inspect incomplete directories and start Caddy manually. `manifest.json` is
written last, after successful resumption, and contains:

- UTC capture completion, Git commit, `git_dirty` boolean and full image pins;
- SHA-256 and size of both tar artifacts;
- `caddy_stopped: true` and the DER SHA-256 internal CA root fingerprint, or null.

CLI children run in new sessions so terminal signals reach the backup's deferred
handler. This does not protect them from a cgroup-wide kill. A systemd service running
the backup must use `KillMode=mixed`: the initial SIGTERM reaches the main process,
while the final forced kill still cleans up the entire cgroup. Set `TimeoutStopSec`
to at least `2200s` (about 37 minutes): the full 1800-second transfer deadline, the
40-second settle window, 300-second resumption budget and 60 seconds of cleanup margin. Increase it for measured slow
storage/cleanup; if execution deadlines change, increase this allowance accordingly.
A forced kill after that timeout
cannot guarantee resumption; alert and verify Caddy manually.

Git fields describe the checkout at preflight. They do not prove which file contents
the running Caddy loaded. Immutable image identity and volume mounts are checked against the container;
keep the matching configuration separately. Compose's service config hash does not
hash the contents of bind-mounted Route Files or Caddyfile.

The manifest excludes environment values, certificates and keys. A directory without
it is incomplete. Failed captures and diagnostics are never automatically deleted.
To clean up, disable the schedule, confirm no capture or restore is running, preserve
the failed set and diagnostics in protected storage, and verify Caddy readiness. Then
remove only the identified incomplete directory after deciding its evidence is no
longer needed. Re-enable the schedule and verify the next capture completes. Never
include Docker volumes or the source Checkpoint in this cleanup. Capture refuses when
the backup filesystem has less free space
than the estimated volume size. Disk-full or archive errors fail the command and resume
Caddy; alert on free space and the age of the last complete, replicated Checkpoint.

After a successful capture and service resumption, the configured retention keeps
the newest complete Checkpoint sets and prunes older complete sets in the backup repository. Incomplete sets, diagnostics and unrelated directories
are left untouched. Copy pre-upgrade Checkpoints outside this repository to retain
them independently. Command stderr is saved only in a 0600 file under
`PE_BACKUP_DIR/.diagnostics` (directory mode 0700); errors report the file path,
never command output. Treat diagnostics as secret-bearing data.

Restore verifies pins, hashes, archive entry safety and the recorded CA fingerprint
before writing. The target project and all consumers of its volumes must be stopped.
It accepts only uncompressed tar archives and bounds the public CA certificate at
64 KiB before parsing. It creates missing volumes, requires **both volumes empty**,
and refuses populated volumes; it never clears a target to make restoration succeed.
Before extraction it places `.pe-restore-incomplete` in both volumes, removing the
markers only after both extractions succeed. Bootstrap refuses to start if either
marker remains. Do not remove markers to bypass this refusal or start Compose directly.
`restore_incomplete` means the helper found a marker. `state_check_failed` means the
helper could not confirm the state, for example because Docker or the image was
unavailable. Diagnose that failure and retry without replacing healthy volumes.
It restores both archives and leaves Caddy stopped. On a new host, restore first, then run bootstrap to create the
network, start Caddy and verify TLS. A failed restore leaves partial data for diagnosis;
retry into another empty prefix. Keep the original Checkpoint until recovery is verified.
Compare bootstrap's `certificate.ca_sha256` with the manifest's `ca_sha256` for an
internal CA before returning clients to service. Markers detect an interrupted restore;
they cannot detect a deleted volume or a wrong prefix. Ordinary bootstrap still creates
missing volumes for new installations, so preserve and check the original CA identity.

## RPO and RTO

**RPO is the successful, replicated Checkpoint interval.** With daily captures, at most
24 hours of certificate state is lost while backups and replication are healthy. ACME
certificates are re-issuable, subject to DNS, issuer availability and rate limits. The
internal CA is not replaceable without changing client trust: losing its keys requires
redistributing a new root to every client. Take a first Checkpoint before distributing
trust and another after any intentional CA change.

**RTO is measured by the drill, not guaranteed.** The operational target for this small
state is five minutes after Docker, pinned images, configuration and a Checkpoint are
available. The drill prints seconds from destruction through restoration, startup and
a verified TLS handshake using the same CA fingerprint. It excludes off-host retrieval,
DNS changes, host provisioning and image pulls. No measured production RTO is claimed
until the drill passes on the target host.

```sh
scripts/backup-drill.sh
# Optional isolated name and spare ports:
SMOKE_PROJECT=platform-edge-drill-monthly SMOKE_HTTP_PORT=18380 SMOKE_HTTPS_PORT=18743 scripts/backup-drill.sh
```

The drill refuses existing resources, owns its project, network and prefix, boots
self-signed HTTPS, captures, destroys with the typed project name, restores into empty
volumes, and verifies the CA fingerprint and HTTPS readiness. It removes its disposable
resources. Run monthly and after backup, Compose or image-pin changes.

```cron
7 2 * * * cd /opt/platform-edge && scripts/backup.sh >> /var/log/platform-edge-backup.log 2>&1
*/5 * * * * cd /opt/platform-edge && python3 scripts/bootstrap.py --probe-only >> /var/log/platform-edge-readiness.log 2>&1
```

`--probe-only` refreshes the leaf expiry metric without restarting Caddy. Alert on probe
failure or stale observations. The schedules avoid simultaneous starts, but a long
capture or probe can still overlap. Locks fail immediately so overlapping maintenance
is visible; backup does not silently queue behind another operation. A
`bootstrap_already_running` probe result can be explained by a confirmed active capture.
Correlate it with backup completion and require the next probe to succeed; do not
blanket-ignore probe failures or stale observations. A backup lock failure requires a
retry after the competing operation finishes, and remains a failed capture for alerting.
Before upgrades, capture a Checkpoint, validate and
smoke the candidate, then change pins. Roll back with preserved volumes only when the
older Caddy supports that state; otherwise restore the matching Checkpoint and pins
into a fresh prefix. Check disk space in both Docker storage and the backup repository.

After a capture failure or interruption, resumption observes a 40-second settle
window before accepting readiness, followed by its 300-second recovery budget.
This covers a daemon stop request that completes after its CLI has already exited.

On a new host, pre-pull pinned images with `docker compose pull` before bootstrap or restore;
slow image downloads can exhaust a helper's command deadline safely before extraction.

## Verification and troubleshooting

A capture succeeded when backup exited 0, the Checkpoint directory holds `manifest.json`,
and Caddy answers `/health` again. A restore succeeded when restore exited 0, both volumes
carry no `.pe-restore-incomplete` marker, bootstrap starts Caddy, and bootstrap's
`certificate.ca_sha256` equals the manifest's `ca_sha256`. The drill proves both on a
disposable project.

| Symptom | Cause and fix |
| --- | --- |
| Backup refuses before stopping Caddy | Tag-only or local image, more than one Caddy container, a restore marker, or too little free space. The error names the check. |
| `bootstrap_already_running` during a probe | A capture holds the env lock. Correlate with backup completion and require the next probe to succeed. |
| `restore_incomplete` | A marker from an interrupted restore. Restore again into empty volumes; never delete markers by hand. |
| `state_check_failed` | The helper could not read the volumes (Docker or image unavailable). Fix that and retry without replacing healthy volumes. |
| Restore refuses populated volumes | Restore never clears a target. Use another `PE_VOLUME_PREFIX` or empty volumes deliberately. |
| Fingerprint mismatch after restore | The wrong Checkpoint or prefix. Do not distribute the new root; restore the matching Checkpoint. |
| Failed command details | `PE_BACKUP_DIR/.diagnostics/` holds stderr (0600); treat it as secret-bearing. |
