# Contributing

Read [AGENTS.md](AGENTS.md), [CONTEXT.md](CONTEXT.md) and the [design](docs/DESIGN.md) before changing anything. [ADRs](docs/adr/) hold the decisions and why. Shared conventions live in [docs/conventions.md](docs/conventions.md).

Toolchain is pinned in `mise.toml`; run `mise install` once. Keep changes focused, use Conventional Commits, and explain the problem and resulting behavior in your pull request.

Gates, from the repository root:

```sh
scripts/validate.sh          # env render, Compose, Caddy in three modes, shellcheck, py_compile
python3 -m unittest discover -s tests
scripts/smoke.sh             # disposable project and three stub upstreams; needs Docker
scripts/backup-drill.sh      # disposable CA restore and TLS verification
```

CI runs the first two on pushes and pull requests, and smoke and the backup drill on every pull request, weekly and on manual dispatch. An image or Compose change merges only after smoke passes. Smoke uses `SMOKE_PROJECT=platform-edge-smoke`, `SMOKE_HTTP_PORT=18280`, `SMOKE_HTTPS_PORT=18643` and its own `<project>-platform` network by default. Override the three `SMOKE_*` variables together for a different disposable instance. It refuses existing project state and removes only the resources it created.

The Python unit tests use fake runners and never call Docker. Report exact commands, counts and every unverified gate. A missing Docker daemon is an unverified container gate, not a pass.

Report bugs and proposals through the issue templates. Report vulnerabilities through the [security policy](SECURITY.md).

For file-specific validation and migration constraints, read the [change map](docs/agents/change-map.md).
