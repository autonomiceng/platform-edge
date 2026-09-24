# Change map

For Compose, Caddyfile or Route File changes, read the ingress contract in
`docs/operations/ingress.md`. Preserve Host and scheme forwarding, the
operator-route denial on the Backplane route, and the `pe-edge` alias. Service, network and
certificate-volume names require a migration when changed.

For bootstrap and environment changes, preserve existing secrets and unmanaged
environment lines. Unit tests use the fake runner and never call Docker.

For status producer, proxy or consumer changes, read `docs/operations/status-contract.md`.

For backup/restore changes, preserve external certificate volumes and verify the
existing restore drill in a disposable project before claiming recovery works.

Run the gates in `CONTRIBUTING.md`. `scripts/validate.sh` runs disposable image
validators; it does not start the installed stack. Image, config and bootstrap
changes also need `scripts/smoke.sh`. Shared-host route changes need
`scripts/integration_smoke.py` against prepared sibling deployments.
