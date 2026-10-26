"""
``flexctl``: one command for configuring, deploying, and operating the Flex connector.

Run from a checkout of this repository on the operator workstation::

    flexctl setup                       # guided: find robot, SSH, wheels, deploy, verify
    flexctl sim                         # run a full simulator locally, no robot needed
    flexctl verify 10.0.0.42 --level motion
    flexctl up 10.0.0.42 --mode pe --operator alice
    flexctl status 10.0.0.42
    flexctl mode 10.0.0.42 opentrons

Every remote action delegates to the reviewed shell scripts in this repository
(``deploy.sh``, ``scripts/install_connector_service.sh``, ``scripts/switch_mode.sh``,
``scripts/rollback_connector.sh``), so ``flexctl`` adds orchestration and
pre-checks without a second implementation of the fail-closed deployment logic.
Use ``--dry-run`` to print the commands without running them.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from contextlib import asynccontextmanager as contextlib_asynccontextmanager

from . import setup_tools

STATE_DIR = "/var/lib/unitelabs-opentrons-flex"
ACTIVE_PATH = "/var/sila2_flex"
ENV_FILE = f"{STATE_DIR}/run-mutation.env"
MODES = {"pe": "connector-pe", "unit": "connector-unit", "opentrons": "opentrons"}
_ACTOR_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_HOST_PATTERN = re.compile(r"^[A-Za-z0-9.:_-]{1,253}$")


class FlexctlError(RuntimeError):
    """An operator-facing failure; the message says how to fix it."""


@dataclass
class Runner:
    """Run (or, in dry-run mode, print) external commands."""

    dry_run: bool = False
    cwd: Path | None = None
    log: Callable[[str], None] = field(default=lambda line: sys.stderr.write(line + "\n"))
    shell: str | None = None

    def script(self, path: str, *args: str) -> str:
        """Run one of the repository's POSIX shell scripts."""
        shell = self.shell or setup_tools.posix_shell()
        return self.run([shell, path, *args])

    def run(self, argv: Sequence[str], *, stdin: str | None = None, check: bool = True, capture: bool = False) -> str:
        """Run ``argv``; return captured stdout (empty unless ``capture``)."""
        shown = " ".join(shlex.quote(part) for part in argv)
        self.log(f"$ {shown}" + (" <<< (stdin redacted)" if stdin is not None else ""))
        if self.dry_run:
            return ""
        completed = subprocess.run(
            list(argv),
            input=stdin,
            text=True,
            cwd=self.cwd,
            capture_output=capture,
            check=False,
        )
        if check and completed.returncode != 0:
            detail = (completed.stderr or "").strip() if capture else ""
            message = f"Command failed with exit status {completed.returncode}: {shown}"
            raise FlexctlError(f"{message}\n{detail}".rstrip())
        return completed.stdout or "" if capture else ""

    def ssh(
        self, host: str, script: str, *, stdin: str | None = None, check: bool = True, capture: bool = False
    ) -> str:
        """Run a shell snippet on the robot as root."""
        return self.run(["ssh", f"root@{host}", script], stdin=stdin, check=check, capture=capture)


# --------------------------------------------------------------------------- config


def _logging_block() -> dict[str, object]:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
        "handlers": {
            "stderr": {"class": "logging.StreamHandler", "stream": "ext://sys.stderr", "formatter": "simple"},
        },
        "root": {"level": "INFO", "handlers": ["stderr"]},
    }


ALL_SIMULATED_MODULES = [
    "heater_shaker",
    "flex_stacker",
    "absorbance_reader",
    "temperature_module",
    "thermocycler",
    "gripper",
]

PROFILES = ("simulator", "simulator-full", "connector-pe", "connector-unit")

_MODULE_MODEL_TO_SIMULATED = {
    "heaterShakerModuleV1": "heater_shaker",
    "flexStackerModuleV1": "flex_stacker",
    "absorbanceReaderV1": "absorbance_reader",
    "temperatureModuleV2": "temperature_module",
    "thermocyclerModuleV2": "thermocycler",
}


def build_config(profile: str, *, simulated_modules: Sequence[str] | None = None) -> dict[str, object]:
    """Return a complete connector configuration for one deployment profile."""
    if profile not in PROFILES:
        message = f"Unknown profile {profile!r}; choose one of {', '.join(PROFILES)}."
        raise FlexctlError(message)
    simulator = profile.startswith("simulator")
    sila_server: dict[str, object] = {
        "hostname": "127.0.0.1" if profile in {"simulator", "simulator-full", "connector-unit"} else "0.0.0.0",
        "port": 50051,
        "tls": False,
        "require_client_auth": False,
        "uuid": str(uuid.uuid4()),
        "name": "Opentrons Flex Simulator" if simulator else "Opentrons Flex",
    }
    config: dict[str, object] = {
        "sila_server": sila_server,
        "cloud_server_endpoint": None,
        "discovery": None if simulator else {"ip_version": "ipv4", "network_interfaces": []},
        "logging": _logging_block(),
        "use_simulator": simulator,
        "simulated_modules": list(simulated_modules)
        if simulated_modules is not None
        else (ALL_SIMULATED_MODULES if profile == "simulator-full" else []),
        "lock_timeout_s": 30.0 if simulator else None,
        "labware_movement_config": None,
    }
    if profile == "simulator-full":
        config.update(
            {
                "with_robot_server": True,
                "robot_server_tcp_port": 31950,
                "run_mutation_ledger_path": "./.flex-sim/run-mutations.jsonl",
                "run_mutation_required": False,
            }
        )
    elif profile == "simulator":
        config.update({"with_robot_server": False, "run_mutation_ledger_path": None, "run_mutation_required": False})
    elif profile == "connector-pe":
        config.update(
            {
                "with_robot_server": True,
                "robot_server_uds": "/run/aiohttp.sock",
                "run_mutation_ledger_path": f"{STATE_DIR}/run-mutations.jsonl",
                "run_mutation_required": True,
                "recovery_state_path": f"{STATE_DIR}/recovery-state.json",
            }
        )
    else:
        config.update(
            {
                "with_robot_server": False,
                "run_mutation_ledger_path": None,
                "run_mutation_required": False,
                "labware_movement_config": f"{ACTIVE_PATH}/asms-unit-labware-movement.json",
                "recovery_state_path": f"{STATE_DIR}/recovery-state.json",
            }
        )
    config["run_mutation_token_env"] = "UNITELABS_RUN_MUTATION_TOKEN"
    config["run_mutation_actor_env"] = "UNITELABS_RUN_MUTATION_ACTOR"
    return config


def detect_robot_hardware(
    host: str,
    *,
    port: int = 31950,
    timeout: float = 8.0,
    fetch: Callable[[str], object] | None = None,
) -> dict[str, list[str]]:
    """
    Read attached instruments and modules from a running Flex robot-server (stock or connector).

    Returns ``{"instruments": [...], "modules": [...], "simulated_modules": [...]}``
    where ``simulated_modules`` mirrors the detected hardware for a simulator profile.
    """
    _validate_host(host)

    def default_fetch(url: str) -> object:
        request = urllib.request.Request(url, headers={"Opentrons-Version": "*"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    read = fetch or default_fetch
    base = f"http://{host}:{port}"
    try:
        instruments = read(f"{base}/instruments")
        modules = read(f"{base}/modules")
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        message = f"Cannot read hardware from {base}: {exc}. Is the robot on and reachable?"
        raise FlexctlError(message) from exc
    instrument_names = sorted(
        f"{item.get('mount')}: {item.get('instrumentModel')}"
        for item in _data(instruments)
        if isinstance(item, dict) and item.get("ok", True)
    )
    module_models = sorted(str(item.get("moduleModel")) for item in _data(modules) if isinstance(item, dict))
    simulated = sorted({_MODULE_MODEL_TO_SIMULATED[m] for m in module_models if m in _MODULE_MODEL_TO_SIMULATED})
    if any(name.startswith("extension:") for name in instrument_names):
        simulated.append("gripper")
    return {"instruments": instrument_names, "modules": module_models, "simulated_modules": simulated}


def _data(payload: object) -> list[object]:
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return list(payload["data"])
    return []


# ---------------------------------------------------------------------- repository


def find_repository(start: Path | None = None) -> Path:
    """Locate the checkout that contains ``deploy.sh`` and ``pyproject.toml``."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "deploy.sh").is_file() and (candidate / "pyproject.toml").is_file():
            return candidate
    message = "Run flexctl inside a checkout of the opentrons-flex repository (deploy.sh not found)."
    raise FlexctlError(message)


def checkout_version(repository: Path) -> str:
    """Read ``[project].version`` from the checkout's pyproject.toml."""
    match = re.search(r'^version = "([^"]+)"', (repository / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    if match is None:
        message = "Cannot read the connector version from pyproject.toml."
        raise FlexctlError(message)
    return match.group(1)


@dataclass(frozen=True)
class Check:
    """One doctor result."""

    name: str
    ok: bool
    detail: str


def doctor_checks(repository: Path, wheels: Path) -> list[Check]:
    """Validate the workstation and wheel bundle before touching the robot."""
    checks = [
        Check("ssh client", shutil.which("ssh") is not None, "ssh must be on PATH"),
        Check("scp client", shutil.which("scp") is not None, "scp must be on PATH"),
    ]
    version = checkout_version(repository)
    manifest_path = wheels / "runtime-manifest.json"
    if not wheels.is_dir():
        checks.append(Check("wheel bundle", False, f"{wheels} does not exist; build or download flex-arm-wheels"))
    elif not manifest_path.is_file():
        checks.append(
            Check("wheel bundle", False, f"{wheels} has no runtime-manifest.json; it is an old build, rebuild it")
        )
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            checks.append(Check("wheel bundle", False, f"unreadable runtime-manifest.json: {exc}"))
        else:
            built = manifest.get("connectorVersion")
            checks.append(
                Check(
                    "wheel bundle",
                    built == version,
                    f"built for {built}, checkout is {version}" + ("" if built == version else "; rebuild the wheels"),
                )
            )
    return checks


def _validate_host(host: str) -> None:
    if not _HOST_PATTERN.fullmatch(host) or host.startswith("-"):
        message = f"Invalid robot host {host!r}."
        raise FlexctlError(message)


# ----------------------------------------------------------------------- commands


def ensure_mutation_credential(runner: Runner, host: str, operator: str | None) -> str | None:
    """Create the controlled-mutation credential on the robot if it is missing; return a new token."""
    present = runner.ssh(
        host,
        f"grep -q '^UNITELABS_RUN_MUTATION_TOKEN=.\\{{32\\}}' {ENV_FILE} && "
        f"grep -q '^UNITELABS_RUN_MUTATION_ACTOR=..*' {ENV_FILE} && echo present || echo missing",
        capture=True,
        check=False,
    ).strip()
    if present == "present":
        return None
    if operator is None:
        message = "The robot has no mutation credential yet; rerun with --operator NAME to create one."
        raise FlexctlError(message)
    if not _ACTOR_PATTERN.fullmatch(operator):
        message = "--operator may contain only letters, digits, dot, underscore, and hyphen (max 64)."
        raise FlexctlError(message)
    token = secrets.token_hex(32)
    payload = f"UNITELABS_RUN_MUTATION_TOKEN={token}\nUNITELABS_RUN_MUTATION_ACTOR={operator}\n"
    # The token travels on stdin so it never appears in a process listing.
    runner.ssh(host, f"umask 077 && mount -o remount,rw / && mkdir -p {STATE_DIR} && cat > {ENV_FILE}", stdin=payload)
    return token


def connector_active(runner: Runner, host: str) -> bool:
    """Return whether the connector service currently owns the hardware."""
    state = runner.ssh(
        host, "systemctl is-active sila2-connector 2>/dev/null || true", capture=True, check=False
    ).strip()
    return state == "active"


def command_up(args: argparse.Namespace, runner: Runner, repository: Path) -> int:
    """Verify, deploy, install, switch, and preflight in one idempotent sequence."""
    _validate_host(args.host)
    wheels = (repository / args.wheels).resolve()
    failures = [check for check in doctor_checks(repository, wheels) if not check.ok]
    if any(check.name == "wheel bundle" for check in failures) and getattr(args, "build", True) and not runner.dry_run:
        runner.log("The wheel bundle is missing or stale; obtaining a fresh one first.")
        method = setup_tools.obtain_wheels(
            repository, wheels, checkout_version(repository), source="auto", log=runner.log
        )
        runner.log(f"Wheel bundle {method}.")
        failures = [check for check in doctor_checks(repository, wheels) if not check.ok]
    if failures and not runner.dry_run:
        lines = "\n".join(f"  - {check.name}: {check.detail}" for check in failures)
        message = f"Local checks failed:\n{lines}"
        raise FlexctlError(message)

    if runner.dry_run:
        runner.log("(dry run) would create the controlled-mutation credential if the robot has none.")
        token = None
    else:
        token = ensure_mutation_credential(runner, args.host, args.operator)
    if token is not None:
        # Print it now: a later step can fail, and the token is never shown again.
        sys.stdout.write(
            "\nA new controlled-mutation credential was created on the robot.\n"
            f"Token (shown once, store it in your password manager): {token}\n\n"
        )
        sys.stdout.flush()
    if connector_active(runner, args.host):
        runner.log("Connector is running; switching to stock Opentrons mode before deployment.")
        runner.script("scripts/switch_mode.sh", args.host, "opentrons")
    runner.script("deploy.sh", args.host, str(wheels))
    runner.script("scripts/install_connector_service.sh", args.host)
    if args.mode == "unit":
        runner.script("scripts/switch_mode.sh", args.host, MODES["unit"])
    preflight = [sys.executable, "scripts/preflight_flex.py", args.host]
    if args.mode == "unit":
        preflight.append("--runtime-only")
    runner.run(preflight)
    sys.stdout.write(f"\nFlex connector is up on {args.host} in {MODES[args.mode]} mode.\n")
    return 0


def command_status(args: argparse.Namespace, runner: Runner) -> int:
    """Print service state, active release, and the running connector identity."""
    _validate_host(args.host)
    script = (
        "printf 'service: '; systemctl is-active sila2-connector 2>/dev/null || true; "
        "printf 'stock robot-server: '; systemctl is-active opentrons-robot-server 2>/dev/null || true; "
        f"printf 'active release: '; readlink -f {ACTIVE_PATH} 2>/dev/null || echo none; "
        f"printf 'config: '; readlink {STATE_DIR}/connector-config.json 2>/dev/null || echo none; "
        "printf 'runtime: '; curl --silent --max-time 5 http://127.0.0.1:31950/unitelabs/runtime || echo unavailable; "
        f"printf '\\nrecovery: '; cat {STATE_DIR}/recovery-state.json 2>/dev/null || echo none"
    )
    output = runner.ssh(args.host, script, capture=True, check=False)
    sys.stdout.write(output)
    return 0


def command_mode(args: argparse.Namespace, runner: Runner) -> int:
    """Switch hardware ownership between the connector modes and stock Opentrons."""
    _validate_host(args.host)
    runner.script("scripts/switch_mode.sh", args.host, MODES[args.mode])
    return 0


def command_rollback(args: argparse.Namespace, runner: Runner) -> int:
    """Restore the previously activated release (stock mode stays active)."""
    _validate_host(args.host)
    runner.script("scripts/rollback_connector.sh", args.host)
    return 0


def command_logs(args: argparse.Namespace, runner: Runner) -> int:
    """Show connector service logs."""
    _validate_host(args.host)
    follow = " -f" if args.follow else ""
    runner.ssh(args.host, f"journalctl -u sila2-connector -n {int(args.lines)} --no-pager{follow}")
    return 0


def command_deck(args: argparse.Namespace, runner: Runner) -> int:
    """Inspect or reconcile the durable deck ledger on the robot (connector must be stopped)."""
    _validate_host(args.host)
    movement_config = shlex.quote(args.movement_config)
    base = f"{ACTIVE_PATH}/bin/python -m unitelabs.opentrons_flex.deck_ledger"
    if args.action == "show":
        sys.stdout.write(runner.ssh(args.host, f"{base} show --movement-config {movement_config}", capture=True))
        return 0
    if connector_active(runner, args.host):
        message = "Stop the connector first: flexctl mode HOST opentrons (the ledger must not change underneath it)."
        raise FlexctlError(message)
    if not args.confirm:
        message = "Inspect the physical deck, then rerun with --confirm."
        raise FlexctlError(message)
    occupancy: list[str]
    if args.empty:
        occupancy = ["--empty"]
    elif args.initial:
        occupancy = ["--initial"]
    else:
        occupancy = [part for item in args.set or [] for part in ("--set", item)]
        if not occupancy:
            message = "Give --set LOCATION=LABWARE (repeatable), --empty, or --initial."
            raise FlexctlError(message)
    command = " ".join(
        [
            "mount -o remount,rw / &&",
            base,
            "reconcile --movement-config",
            movement_config,
            "--operator",
            shlex.quote(args.operator),
            "--reason",
            shlex.quote(args.reason),
            *(shlex.quote(part) for part in occupancy),
            "--confirm",
        ]
    )
    sys.stdout.write(runner.ssh(args.host, command, capture=True))
    return 0


def command_config_init(args: argparse.Namespace) -> int:
    """Write a ready-to-use configuration file for one profile."""
    simulated = None
    if args.detect is not None:
        detected = detect_robot_hardware(args.detect)
        sys.stderr.write(
            f"Detected instruments: {', '.join(detected['instruments']) or 'none'}\n"
            f"Detected modules: {', '.join(detected['modules']) or 'none'}\n"
        )
        if args.profile.startswith("simulator"):
            simulated = detected["simulated_modules"]
    config = build_config(args.profile, simulated_modules=simulated)
    text = json.dumps(config, indent=2) + "\n"
    if args.output == "-":
        sys.stdout.write(text)
        return 0
    output = Path(args.output)
    if output.exists() and not args.force:
        message = f"{output} exists; pass --force to overwrite it."
        raise FlexctlError(message)
    output.write_text(text, encoding="utf-8")
    sys.stderr.write(f"Wrote {args.profile} configuration to {output}\n")
    if args.profile.startswith("simulator"):
        sys.stderr.write(
            f"Start it with: connector start --app unitelabs.opentrons_flex:create_app --config-path {output}\n"
        )
    return 0


def command_doctor(args: argparse.Namespace, repository: Path) -> int:
    """Print local readiness checks; exit 1 if any fail."""
    checks = doctor_checks(repository, (repository / args.wheels).resolve())
    for check in checks:
        sys.stdout.write(f"[{'ok' if check.ok else 'FAIL'}] {check.name}: {check.detail}\n")
    return 0 if all(check.ok for check in checks) else 1


# ------------------------------------------------------------- setup helpers


def command_discover(_args: argparse.Namespace) -> int:
    """List Opentrons Flex robots found on the local network."""
    robots = setup_tools.discover_robots()
    if not robots:
        sys.stdout.write(
            "No Flex found via mDNS. Read the IP on the robot touchscreen (Settings > Network) "
            "and pass it directly, e.g. flexctl setup --host 10.0.0.42\n"
        )
        return 1
    for robot in robots:
        sys.stdout.write(f"{robot.name}\t{robot.host}\n")
    return 0


def _usb_prompt(drive: str | None, ask: Callable[[str], str]) -> Callable[[Path], bool]:
    def ready(public_key: Path) -> bool:
        target_dir = (
            drive
            or ask(
                "This network path cannot install SSH keys directly. Insert an otherwise EMPTY USB drive into "
                "THIS computer (the Flex trusts every .pub file on it as a root login key) and type its mount "
                "path (for example /Volumes/USB or E:\\), or press Enter to cancel: "
            ).strip()
        )
        if not target_dir:
            return False
        others = sorted(path.name for path in Path(target_dir).glob("*.pub") if path.name != public_key.name)
        if others:
            message = (
                f"The USB drive already holds other public keys ({', '.join(others)}). The Flex would trust "
                "all of them for root login. Remove them or use an empty drive, then retry."
            )
            raise FlexctlError(message)
        target = Path(target_dir) / public_key.name
        shutil.copyfile(public_key, target)
        ask(f"Copied {public_key.name} to {target_dir}. Eject it, plug it into a Flex USB port, then press Enter.")
        return True

    return ready


def command_ssh_setup(args: argparse.Namespace, ask: Callable[[str], str] = input) -> int:
    """Make key-based ``ssh root@HOST`` work for every other flexctl command."""
    _validate_host(args.host)
    setup_tools.check_flex(args.host)
    result = setup_tools.ensure_ssh_access(args.host, usb_ready=_usb_prompt(args.usb_drive, ask))
    sys.stdout.write(f"SSH ready: {result}\n")
    return 0


def command_build(args: argparse.Namespace, repository: Path) -> int:
    """Download or build the ARM wheel bundle that matches this checkout."""
    wheels = (repository / args.wheels).resolve()
    method = setup_tools.obtain_wheels(repository, wheels, checkout_version(repository), source=args.source)
    sys.stdout.write(f"Wheel bundle in {wheels}: {method}.\n")
    return 0


def command_clean(args: argparse.Namespace, repository: Path) -> int:
    """Remove caches and wheel bundles that do not match this checkout."""
    paths = setup_tools.stale_paths(repository, checkout_version(repository))
    if not paths:
        sys.stdout.write("Nothing to clean.\n")
        return 0
    for path in paths:
        sys.stdout.write(f"{'removing' if args.yes else 'would remove'} {path.relative_to(repository)}\n")
    if args.yes:
        setup_tools.remove_paths(paths)
    else:
        sys.stdout.write("Rerun with --yes to delete these.\n")
    return 0


def command_sim(args: argparse.Namespace, repository: Path, runner: Runner) -> int:
    """Run a complete local simulator (SiLA on 50051, HTTP on 31950) in the foreground."""
    profile = "simulator-full" if args.with_http else args.profile
    if profile == "simulator-full" and not setup_tools.robot_server_installed():
        setup_tools.install_robot_server(log=runner.log)
    config = Path(args.config) if args.config else repository / ".flex-sim" / f"{profile}.json"
    if not config.exists():
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps(build_config(profile), indent=2) + "\n", encoding="utf-8")
        runner.log(f"Wrote {profile} configuration to {config}")
    environment_hint = "In another terminal: flexctl verify 127.0.0.1 --allow-simulator --level motion --deck-clear"
    runner.log(environment_hint)
    runner.run(
        [
            sys.executable,
            "-c",
            "from unitelabs.cdk.cli import connector; connector()",
            "start",
            "--app",
            "unitelabs.opentrons_flex:create_app",
            "--config-path",
            str(config),
        ],
        check=False,
    )
    return 0


@contextlib_asynccontextmanager
async def _connect_with_tunnel(host: str, port: int, runner: Runner, *, use_tunnel: bool | None):  # noqa: ANN202
    """Connect directly, or through an SSH tunnel when SiLA listens only on the robot's loopback."""
    import asyncio
    import contextlib

    from .client import FlexClient

    async with contextlib.AsyncExitStack() as stack:
        client = None
        if use_tunnel is not True:
            try:
                client = await stack.enter_async_context(FlexClient.connect(f"{host}:{port}", timeout_s=4.0))
            except ConnectionError:
                if use_tunnel is False:
                    raise
        if client is None:
            local_port = setup_tools.free_local_port()
            runner.log(f"SiLA is not reachable directly; opening an SSH tunnel ({host} -> 127.0.0.1:{local_port})")
            tunnel = await asyncio.create_subprocess_exec(
                "ssh",
                "-N",
                "-o",
                "ExitOnForwardFailure=yes",
                "-L",
                f"127.0.0.1:{local_port}:127.0.0.1:{port}",
                f"root@{host}",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            async def close_tunnel() -> None:
                with contextlib.suppress(ProcessLookupError):
                    tunnel.terminate()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(tunnel.wait(), timeout=5)

            stack.push_async_callback(close_tunnel)
            client = await stack.enter_async_context(FlexClient.connect(f"127.0.0.1:{local_port}", timeout_s=15.0))
        yield client


def command_verify(args: argparse.Namespace, runner: Runner) -> int:
    """Run the guided verification levels and write a JSON report."""
    import asyncio

    from . import verify

    host, _, port_text = args.host.partition(":")
    _validate_host(host)
    port = int(port_text or args.port)
    levels = verify.LEVELS if args.level == "all" else verify.LEVELS[: verify.LEVELS.index(args.level) + 1]
    local = host in {"127.0.0.1", "localhost"}

    def http_get(path: str) -> object:
        return setup_tools.robot_http(host, path)

    try:
        setup_tools.robot_http(host, "/health")
        http = http_get
    except (OSError, ValueError):
        http = None

    def ssh(command: str) -> str:
        return runner.ssh(host, command, capture=True)

    ssh_available = not local and setup_tools.ssh_works(host)
    operator = verify.UnattendedOperator(deck_clear=args.deck_clear) if args.unattended else verify.ConsoleOperator()
    if args.deck_clear and not args.unattended:
        operator = _PreConfirmedOperator(operator)
    verifier = verify.Verifier(
        lambda: _connect_with_tunnel(host, port, runner, use_tunnel=False if local or not ssh_available else None),
        operator=operator,
        levels=tuple(levels),
        http_get=http,
        ssh=ssh if ssh_available else None,
        acknowledge_estop=(
            (lambda: setup_tools.robot_http(host, "/robot/control/acknowledgeEstopDisengage", method="PUT"))
            if http is not None
            else None
        ),
        allow_simulator=args.allow_simulator,
        move_speed=args.move_speed,
    )
    results = asyncio.run(verifier.run())
    summary, passed = verify.summarize(results)
    report = verify.write_report(results, Path(args.report_dir), args.host)
    sys.stdout.write(f"\n{summary}\nReport: {report}\n")
    return 0 if passed else 1


class _PreConfirmedOperator:
    """Console operator whose deck-clear question was answered on the command line."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def ask(self, prompt: str) -> bool:
        if prompt.startswith("Confirm the deck is clear"):
            self.say("[--deck-clear] deck confirmed clear on the command line")
            return True
        return self._inner.ask(prompt)

    def say(self, message: str) -> None:
        self._inner.say(message)


def _choose_robot(say: Callable[[str], None], ask: Callable[[str], str]) -> str:
    """Let the operator pick (and always confirm) which robot to set up."""
    say("Looking for Flex robots on the network...")
    robots = setup_tools.discover_robots()
    if not robots:
        target = ask("No robot found automatically. Robot IP (touchscreen: Settings > Network): ").strip()
        if not target:
            message = "No robot selected; pass --host IP."
            raise FlexctlError(message)
        return target
    for index, robot in enumerate(robots, start=1):
        say(f"  {index}. {robot.name} ({robot.host})")
    prompt = (
        f"Set up {robots[0].name} ({robots[0].host})? Type 1 to confirm, or enter another IP: "
        if len(robots) == 1
        else f"Which robot? Type 1-{len(robots)} or an IP: "
    )
    answer = ask(prompt).strip()
    if answer.isdigit():
        number = int(answer)
        if 1 <= number <= len(robots):
            return robots[number - 1].host
        message = f"{answer} is not in the list (1-{len(robots)}). Rerun setup, or pass --host IP."
        raise FlexctlError(message)
    if not answer:
        message = "No robot selected; pass --host IP."
        raise FlexctlError(message)
    return answer


def command_setup(args: argparse.Namespace, runner: Runner, repository: Path, ask: Callable[[str], str] = input) -> int:
    """Guided first-time setup: simulator, or find a Flex, SSH, wheels, deploy, and verify."""
    say = runner.log
    target = args.host
    if not target and not args.non_interactive:
        choice = ask("Set up a real Flex (r) or try the local simulator (s)? [r/s]: ").strip().lower()
        if choice.startswith("s"):
            return command_sim(
                argparse.Namespace(config=None, profile="simulator", with_http=False), repository, runner
            )
    if not target:
        if args.non_interactive:
            message = "Pass --host IP with --non-interactive; setup never picks a robot on its own."
            raise FlexctlError(message)
        target = _choose_robot(say, ask)
    _validate_host(target)
    robot = setup_tools.check_flex(target)
    say(f"[1/5] {robot.name} at {target} is an Opentrons Flex.")
    result = setup_tools.ensure_ssh_access(
        target, usb_ready=_usb_prompt(args.usb_drive, ask) if not args.non_interactive else None
    )
    say(f"[2/5] SSH: {result}.")
    wheels = (repository / args.wheels).resolve()
    say(f"[3/5] Wheels: {setup_tools.obtain_wheels(repository, wheels, checkout_version(repository))}.")
    operator_name = args.operator
    if not operator_name and not args.non_interactive:
        operator_name = ask("Your operator name (letters, digits, . _ -): ").strip() or None
    say("[4/5] Deploying and starting the connector...")
    command_up(
        argparse.Namespace(host=target, mode=args.mode, wheels=args.wheels, operator=operator_name, build=True),
        runner,
        repository,
    )
    say("[5/5] Smoke test...")
    code = command_verify(
        argparse.Namespace(
            host=target,
            port=50051,
            level="smoke",
            unattended=True,
            deck_clear=False,
            allow_simulator=False,
            move_speed=10.0,
            report_dir=args.report_dir,
        ),
        runner,
    )
    sys.stdout.write(
        "\nNext: run the hardware checks with the robot in view:\n"
        f"  flexctl verify {target} --level motion      # ~2 min, homes and tests the emergency stop\n"
        f"  flexctl verify {target} --level all         # adds tip, restart, and module plug tests\n"
    )
    return code


# --------------------------------------------------------------------------- main


def _move_speed(text: str) -> float:
    """Parse --move-speed; zero or negative would mean the fast hardware default."""
    try:
        value = float(text)
    except ValueError as exc:
        message = f"{text!r} is not a number"
        raise argparse.ArgumentTypeError(message) from exc
    if not 1.0 <= value <= 50.0:
        message = "must be between 1 and 50 mm/s"
        raise argparse.ArgumentTypeError(message)
    return value


def build_parser() -> argparse.ArgumentParser:
    """Return the ``flexctl`` argument parser."""
    parser = argparse.ArgumentParser(prog="flexctl", description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("--dry-run", action="store_true", help="Print commands instead of running them.")
    sub = parser.add_subparsers(dest="command", required=True)

    config = sub.add_parser("config", help="Generate connector configuration.")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    init = config_sub.add_parser("init", help="Write a configuration for one profile.")
    init.add_argument("--profile", choices=PROFILES, required=True)
    init.add_argument("--output", default="config.json", help="Output path, or - for stdout.")
    init.add_argument("--detect", metavar="HOST", help="Read attached hardware from a running Flex.")
    init.add_argument("--force", action="store_true", help="Overwrite an existing file.")

    doctor = sub.add_parser("doctor", help="Check the workstation and wheel bundle.")
    doctor.add_argument("--wheels", default="dist_arm")

    up = sub.add_parser("up", help="Deploy and start the connector in one step.")
    up.add_argument("host")
    up.add_argument("--mode", choices=("pe", "unit"), default="pe")
    up.add_argument("--wheels", default="dist_arm")
    up.add_argument("--operator", help="Operator identity for a newly created mutation credential.")

    up.add_argument(
        "--no-build", dest="build", action="store_false", help="Fail instead of fetching a missing wheel bundle."
    )

    setup = sub.add_parser("setup", help="Guided first-time setup (simulator or real Flex).")
    setup.add_argument("--host", help="Robot IP; found automatically when omitted.")
    setup.add_argument("--mode", choices=("pe", "unit"), default="pe")
    setup.add_argument("--operator", help="Operator identity for the mutation credential.")
    setup.add_argument("--wheels", default="dist_arm")
    setup.add_argument("--usb-drive", help="Mounted USB drive to carry the SSH key when no cable is available.")
    setup.add_argument("--report-dir", default=".")
    setup.add_argument("--non-interactive", action="store_true", help="Never prompt; fail when input is needed.")

    sub.add_parser("discover", help="Find Opentrons Flex robots on the local network.")

    ssh_setup = sub.add_parser("ssh-setup", help="Install an SSH key on the robot (cable or USB drive).")
    ssh_setup.add_argument("host")
    ssh_setup.add_argument("--usb-drive", help="Mounted USB drive path to copy the public key to.")

    build = sub.add_parser("build", help="Download (GitHub Actions) or build (Docker) the ARM wheel bundle.")
    build.add_argument("--wheels", default="dist_arm")
    build.add_argument("--source", choices=("auto", "github", "docker"), default="auto")

    clean = sub.add_parser("clean", help="Remove caches and wheel bundles that do not match this checkout.")
    clean.add_argument("--yes", action="store_true")

    sim = sub.add_parser("sim", help="Run a full local simulator (no robot needed).")
    sim.add_argument("--config", help="Configuration file (default .flex-sim/config.json).")
    sim.add_argument("--profile", choices=("simulator", "simulator-full"), default="simulator")
    sim.add_argument(
        "--with-http",
        action="store_true",
        help="Also run the Opentrons HTTP API (installs the pinned robot-server once).",
    )

    verify = sub.add_parser("verify", help="Guided hardware verification with a pass/fail report.")
    verify.add_argument("host", help="Robot IP (or 127.0.0.1 for the local simulator), optionally HOST:PORT.")
    verify.add_argument(
        "--level",
        choices=("smoke", "motion", "recovery", "modules", "all"),
        default="smoke",
        help="Levels are cumulative: motion includes smoke, recovery adds motion, modules (= all) adds recovery.",
    )
    verify.add_argument("--port", type=int, default=50051)
    verify.add_argument("--deck-clear", action="store_true", help="Pre-confirm the deck is clear for motion tests.")
    verify.add_argument("--unattended", action="store_true", help="Never prompt; skip steps that need hands.")
    verify.add_argument("--allow-simulator", action="store_true", help="Accept a simulator (development only).")
    verify.add_argument(
        "--move-speed",
        type=_move_speed,
        default=10.0,
        help="Speed (mm/s, 1-50) of the move interrupted by the emergency-stop test.",
    )
    verify.add_argument("--report-dir", default=".")

    status = sub.add_parser("status", help="Show the connector state on the robot.")
    status.add_argument("host")

    mode = sub.add_parser("mode", help="Switch hardware ownership.")
    mode.add_argument("host")
    mode.add_argument("mode", choices=tuple(MODES))

    rollback = sub.add_parser("rollback", help="Restore the previously activated release.")
    rollback.add_argument("host")

    logs = sub.add_parser("logs", help="Show connector logs.")
    logs.add_argument("host")
    logs.add_argument("--lines", type=int, default=200)
    logs.add_argument("--follow", action="store_true")

    deck = sub.add_parser("deck", help="Inspect or reconcile the durable deck ledger.")
    deck.add_argument("host")
    deck.add_argument("action", choices=("show", "reconcile"))
    deck.add_argument("--movement-config", default=f"{ACTIVE_PATH}/asms-unit-labware-movement.json")
    deck.add_argument("--set", action="append", metavar="LOCATION=LABWARE")
    deck.add_argument("--empty", action="store_true")
    deck.add_argument("--initial", action="store_true")
    deck.add_argument("--operator", default="")
    deck.add_argument("--reason", default="")
    deck.add_argument("--confirm", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None, *, runner: Runner | None = None) -> int:
    """Entry point for the ``flexctl`` console script."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "config":
            return command_config_init(args)
        if args.command == "discover":
            return command_discover(args)
        if args.command == "ssh-setup":
            return command_ssh_setup(args)
        repository = find_repository()
        runner = runner or Runner(dry_run=args.dry_run, cwd=repository)
        handlers: dict[str, Callable[[], int]] = {
            "doctor": lambda: command_doctor(args, repository),
            "up": lambda: command_up(args, runner, repository),
            "status": lambda: command_status(args, runner),
            "mode": lambda: command_mode(args, runner),
            "rollback": lambda: command_rollback(args, runner),
            "logs": lambda: command_logs(args, runner),
            "deck": lambda: command_deck(args, runner),
            "setup": lambda: command_setup(args, runner, repository),
            "build": lambda: command_build(args, repository),
            "clean": lambda: command_clean(args, repository),
            "sim": lambda: command_sim(args, repository, runner),
            "verify": lambda: command_verify(args, runner),
        }
        if args.command == "deck" and args.action == "reconcile" and (not args.operator or not args.reason):
            message = "deck reconcile requires --operator and --reason."
            raise FlexctlError(message)
        return handlers[args.command]()
    except (FlexctlError, setup_tools.SetupError) as exc:
        sys.stderr.write(f"flexctl: {exc}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("flexctl: interrupted\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
