"""Install or reconfigure the selected sibling stacks behind Edge.

For each stack named with `--with`, write the Platform Contract's bundle settings into the
sibling's `.env` (every other line kept byte for byte) and run the sibling's own bootstrap
from its checkout. No secret is read for its value, generated or printed.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import bootstrap

ORDER = ("gateway", "observability", "backplane")
# Setting prefix, sibling repository directory, bootstrap entrypoint inside that checkout.
STACKS = {
    "gateway": ("LG", "llm-gateway-stack", "scripts/bootstrap.py"),
    "observability": ("OB", "observability-stack", "scripts/bootstrap.py"),
    "backplane": ("BP", "agent-backplane", "infra/bootstrap/prepare.ts"),
}
# Loopback HTTP ports behind Edge (Platform Contract, "Bundle ports").
PORTS = {"gateway": "18080", "observability": "18180", "backplane": "3000"}
# A sibling bootstrap pulls images and waits up to 300 s for Compose readiness on top.
TIMEOUT = 1800


def add_arguments(parser) -> None:
    parser.add_argument("--with", dest="stacks", action="append", choices=ORDER, default=[], metavar="STACK",
                        help="install or reconfigure this sibling stack behind Edge (gateway, observability, backplane); repeatable")
    for stack in ORDER:
        parser.add_argument(f"--{stack}-dir", type=Path, help=f"{STACKS[stack][1]} checkout; default ../{STACKS[stack][1]}")
    parser.add_argument("--capability-file", type=Path, help="Backplane enrollment capability file, handed to its bootstrap")


def check_usage(parser, args) -> None:
    for stack in ORDER:
        if getattr(args, stack + "_dir") and stack not in args.stacks:
            parser.error(f"--{stack}-dir requires --with {stack}")
    if args.capability_file and "backplane" not in args.stacks:
        parser.error("--capability-file requires --with backplane")


def settings(stack: str, edge: dict[str, str]) -> dict[str, str]:
    """The bundle keys one stack receives, derived from the Edge settings."""
    prefix = STACKS[stack][0]
    domain, scheme = edge["PE_PUBLIC_DOMAIN"], edge["PE_SCHEME"]
    values = {"ACCESS_MODE": "proxy"}
    if stack == "backplane":
        values["PUBLIC_URL"] = f"{scheme}://backplane.{domain}"
    else:
        values.update(PUBLIC_DOMAIN=domain, SCHEME=scheme)
    values["BIND_HOST"] = "127.0.0.1"
    if stack == "backplane":
        values["PORT"] = PORTS[stack]
    else:
        values.update(HTTP_PORT=PORTS[stack], PUBLIC_PORT_SUFFIX="")
    values["PLATFORM_NETWORK"] = edge["PE_PLATFORM_NETWORK"]
    # Always written, so a sibling never keeps an allocation Edge has since reverted.
    for key in ("PLATFORM_SUBNET", "PLATFORM_IP_RANGE"):
        values[key] = edge["PE_" + key]
    values["TRUSTED_PROXIES"] = edge["PE_EDGE_IP"] + "/32"
    if stack == "observability":
        values.update(GATEWAY_HEALTH_HOST=domain, GATEWAY_URL=f"{scheme}://{domain}",
                      BACKPLANE_URL=f"{scheme}://backplane.{domain}")
    return {prefix + "_" + key: value for key, value in values.items()}


def unquoted(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def amended(source: str, changes: dict[str, str]) -> tuple[str, dict[str, str]]:
    """The env text with the bundle keys applied, and the keys whose value changes."""
    pending = dict(changes)
    written = {}
    lines = []
    for line in source.splitlines(keepends=True):
        match = bootstrap.ENV_LINE.match(line.rstrip("\r\n"))
        key = match.group("key") if match else None
        # A quoted value that continues on later lines would make those lines look like assignments.
        if match and match.group("value").strip()[:1] in ("'", '"') and unquoted(match.group("value")) == match.group("value").strip():
            raise bootstrap.Refused("bundle_env_repair_required", f"{key} holds a multiline or unterminated quoted value; not supported")
        if key in changes:
            if key not in pending:
                raise bootstrap.Refused("bundle_env_repair_required", f"{key} is set twice; keep one assignment")
            value = pending.pop(key)
            if unquoted(match.group("value")) != value:
                line = line[:match.start("value")] + value + line[match.end("value"):]
                written[key] = value
        lines.append(line)
    text = "".join(lines)
    if pending:
        text += ("" if not text or text.endswith("\n") else "\n") + "".join(f"{key}={value}\n" for key, value in pending.items())
        written.update(pending)
    return text, written


def source_text(env: Path) -> str:
    """The sibling env, or its template when the env does not exist yet."""
    with open(env if env.exists() else env.with_name(".env.example"), encoding="utf-8", newline="") as handle:
        return handle.read()


def command(stack: str, args, values: dict[str, str], source: str) -> list[str]:
    if stack != "backplane":
        return [sys.executable, "scripts/bootstrap.py"]
    argv = ["bun", "infra/bootstrap/prepare.ts", "--capability-file", str(args.capability_file.resolve()),
            "--access-mode", "proxy", "--public-url", values["BP_PUBLIC_URL"]]
    # A recorded selection is authoritative for prepare.ts; repeating --profile conflicts with it.
    recorded = any(match and match.group("key") == "COMPOSE_PROFILES"
                   for match in map(bootstrap.ENV_LINE.match, source.splitlines()))
    return argv if recorded else argv + ["--profile", "gateway"]


def plan(args, root: Path, edge: dict[str, str]) -> list[dict]:
    """Preflight every selected stack; nothing is written until every checkout qualifies."""
    if not args.stacks:
        return []
    prefixes = tuple(STACKS[stack][0] + "_" for stack in args.stacks) + ("COMPOSE_",)
    exported = sorted(key for key in os.environ if key.startswith(prefixes))
    if exported:
        raise bootstrap.Refused("bundle_shell_settings", "unset " + ", ".join(exported) + "; siblings are configured through their .env files")
    if "backplane" in args.stacks:
        if not args.capability_file:
            raise bootstrap.Refused("bundle_capability_file", "--capability-file is required with --with backplane; its bootstrap takes it on every run")
        if shutil.which("bun") is None:
            raise bootstrap.Refused("bundle_tool_missing", "install bun; Backplane's bootstrap runs with it")
    plans = []
    for stack in ORDER:
        if stack not in args.stacks:
            continue
        prefix, repository, entrypoint = STACKS[stack]
        directory = (getattr(args, stack + "_dir") or root.parent / repository).resolve()
        env = directory / ".env"
        if not (directory / entrypoint).is_file() or not env.with_name(".env.example").is_file():
            raise bootstrap.Refused("bundle_checkout_missing", f"{directory} is not a {repository} checkout with {entrypoint} and .env.example; pass --{stack}-dir")
        if env.is_symlink():
            raise bootstrap.Refused("bundle_env_symlink", f"{env} must be a regular file")
        values = settings(stack, edge)
        source = source_text(env)
        _, writes = amended(source, values)
        plans.append({"stack": stack, "directory": directory, "env": env, "settings": values, "writes": writes,
                      "command": command(stack, args, values, source)})
    return plans


def describe(item: dict) -> dict:
    return {"stack": item["stack"], "checkout": str(item["directory"]), "env": str(item["env"]),
            "writes": item["writes"], "command": item["command"]}


@contextlib.contextmanager
def locked(stack: str, env: Path):
    """Hold the sibling's own env lock for the rewrite; it is released before its bootstrap runs."""
    lock = env.with_name(env.name + ".lock")
    if stack == "backplane":
        # prepare.ts creates this file exclusively for its run and removes it on exit.
        try:
            os.close(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
        except FileExistsError:
            raise bootstrap.Refused("bundle_env_locked", f"{lock} exists: a Backplane preparation is running, or a stale lock needs its documented recovery") from None
        try:
            yield
        finally:
            lock.unlink(missing_ok=True)
        return
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise bootstrap.Refused("bundle_env_locked", f"{lock}: a {stack} bootstrap is running") from None
        yield
    finally:
        os.close(fd)


def replace(env: Path, text: str) -> None:
    """Private atomic replacement beside the env; a failed write leaves no temporary behind."""
    mode = stat.S_IMODE(env.stat().st_mode) if env.exists() else 0o600
    fd, temporary = tempfile.mkstemp(prefix=".env.bundle-", dir=env.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            os.fchmod(fd, mode)
            handle.write(text)
            handle.flush()
            os.fsync(fd)
        os.replace(temporary, env)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    directory = os.open(env.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def run_bootstrap(argv: list[str], directory: Path) -> int:
    """Run a sibling bootstrap in its checkout with this terminal as its stdout and stderr."""
    return bootstrap.run_detached(argv, timeout=TIMEOUT, stdout=None, stderr=None, cwd=str(directory)).returncode


def install(plans: list[dict], argv: list[str]) -> None:
    for item in plans:
        stack, env, writes = item["stack"], item["env"], {}
        # Edge is already running here, so every failure from this point is exit 3 with the rerun.
        try:
            with locked(stack, env):
                text, writes = amended(source_text(env), item["settings"])
                if writes or not env.exists():
                    replace(env, text)
            outcome = "exited " + str(run_bootstrap(item["command"], item["directory"]))
        except subprocess.TimeoutExpired:
            outcome = f"ran longer than {TIMEOUT} s and was stopped"
        except bootstrap.Refused as refused:
            outcome = "was not started: " + refused.detail
        except OSError as error:
            outcome = "was not started: " + str(error)
        if outcome != "exited 0":
            raise bootstrap.Refused("sibling_bootstrap_failed",
                                    f"{stack} bootstrap {outcome} in {item['directory']}; earlier stacks are ready and later "
                                    f"stacks are untouched. Fix the reported problem, then rerun "
                                    f"`python3 scripts/bootstrap.py {shlex.join(argv)}` here or `{shlex.join(item['command'])}` in that checkout")
        print(json.dumps({"stack": stack, "env": str(env), "written": writes, "command": item["command"]}))
