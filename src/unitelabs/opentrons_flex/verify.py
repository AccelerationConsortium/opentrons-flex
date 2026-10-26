"""
Guided, mostly automatic verification of a deployed Flex connector (``flexctl verify``).

Levels build on each other and each step reports PASS, WARN, FAIL, or SKIP:

``smoke`` (about 30 s, nothing moves)
    Connects, reads RobotStatusProvider, the HTTP API, pipettes, gripper and every
    registered module.
``motion`` (about 2 min, asks once that the deck is clear)
    Homes, proves an emergency stop interrupts a slow move with
    OperationHaltedError, that the recovery gate blocks motion until homing, and
    optionally that the physical E-stop is reported.
``recovery`` (needs a pipette and SSH)
    Reconciles the tip state on each pipette and proves recovery gates survive a
    connector restart.
``modules`` (guided, needs hands at the robot)
    Unplug/replug of a registered module (no restart needed) and hot-plug of a
    new module type (automatic restart and registration).

Every step that needs a person asks first; pressing ``s`` skips it.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Protocol

from .client import FlexClient, SiLACallError, enum_name, field

LEVELS = ("smoke", "motion", "recovery", "modules")

MODULE_FEATURES = {
    "HEATER_SHAKER": "HeaterShakerController",
    "FLEX_STACKER": "FlexStackerController",
    "ABSORBANCE_READER": "AbsorbanceReaderController",
    "TEMPERATURE": "TemperatureController",
    "THERMOCYCLER": "ThermocyclerController",
}


@dataclasses.dataclass
class StepResult:
    """Outcome of one verification step."""

    level: str
    name: str
    outcome: str
    detail: str
    seconds: float


class Operator(Protocol):
    """How the verifier talks to the person at the robot."""

    def ask(self, prompt: str) -> bool:
        """Return True to proceed, False to skip."""
        ...

    def say(self, message: str) -> None:
        """Show progress."""
        ...


class ConsoleOperator:
    """Interactive operator on the terminal (Enter = continue, s = skip)."""

    def ask(self, prompt: str) -> bool:  # noqa: D102
        answer = input(f"\n>>> {prompt}\n    [Enter] continue, [s] skip: ").strip().lower()
        return answer not in {"s", "skip", "n", "no"}

    def say(self, message: str) -> None:  # noqa: D102
        print(message, flush=True)  # noqa: T201


class UnattendedOperator:
    """Non-interactive operator: answers prompts from pre-given consent, skips the rest."""

    def __init__(self, *, deck_clear: bool = False, say: Callable[[str], None] | None = None) -> None:
        self._deck_clear = deck_clear
        self._say = say or (lambda message: print(message, flush=True))  # noqa: T201

    def ask(self, prompt: str) -> bool:  # noqa: D102
        consent = self._deck_clear and prompt.startswith("Confirm the deck is clear")
        self.say(f"[unattended] {'yes' if consent else 'skip'}: {prompt.splitlines()[0]}")
        return consent

    def say(self, message: str) -> None:  # noqa: D102
        self._say(message)


class _Skip(Exception):
    pass


class _Warn(Exception):
    pass


Connect = Callable[[], contextlib.AbstractAsyncContextManager[FlexClient]]


class Verifier:
    """Run verification levels against one connector."""

    def __init__(
        self,
        connect: Connect,
        *,
        operator: Operator,
        levels: tuple[str, ...] = ("smoke",),
        http_get: Callable[[str], object] | None = None,
        ssh: Callable[[str], str] | None = None,
        acknowledge_estop: Callable[[], object] | None = None,
        allow_simulator: bool = False,
        move_speed: float = 10.0,
        move_distance: float = 100.0,
        timeouts: dict[str, float] | None = None,
    ) -> None:
        """
        Configure a verification run.

        Args:
            connect: Returns an async context manager yielding a connected ``FlexClient``.
            operator: Prompts and progress output.
            levels: Levels to run, in order.
            http_get: Returns decoded JSON for an HTTP path on the robot, or None without HTTP.
            ssh: Runs a shell command on the robot as root, or None without SSH.
            acknowledge_estop: Acknowledges a released E-stop (HTTP), or None to ask the operator.
            allow_simulator: Do not fail when the connector reports the simulator.
            move_speed: Gantry speed (mm/s) for the interrupted test move.
            move_distance: Length (mm) of the interrupted test move.
            timeouts: Overrides for ``restart`` and ``module`` waits in seconds.
        """
        self._connect = connect
        self._operator = operator
        self._levels = levels
        self._http_get = http_get
        self._ssh = ssh
        self._acknowledge_estop = acknowledge_estop
        self._allow_simulator = allow_simulator
        self._move_speed = move_speed
        self._move_distance = move_distance
        self._timeouts = {"restart": 120.0, "module": 30.0, **(timeouts or {})}
        self.results: list[StepResult] = []
        self._client: FlexClient | None = None
        self._deck_clear = False
        self._pipette_mounts: list[str] = []
        self._gripper = False

    # ---------------------------------------------------------------- running

    async def run(self) -> list[StepResult]:
        """Run every selected level; never raises for a failed step."""
        try:
            async with self._session():
                for level in LEVELS:
                    if level not in self._levels:
                        continue
                    self._operator.say(f"\n=== {level} ===")
                    for name, step in self._steps(level):
                        await self._run_step(level, name, step)
        except (ConnectionError, OSError) as exc:
            self.results.append(StepResult("smoke", "Connect to the SiLA server", "FAIL", str(exc), 0.0))
        return self.results

    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[None]:
        stack = contextlib.AsyncExitStack()
        try:
            self._client = await stack.enter_async_context(self._connect())
            self._stack = stack
            yield
        finally:
            await stack.aclose()

    async def _reconnect(self, *, timeout_s: float) -> None:
        await self._stack.aclose()
        self._stack = contextlib.AsyncExitStack()
        deadline = time.monotonic() + timeout_s
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self._client = await self._stack.enter_async_context(self._connect())
                await self._status()
            except (ConnectionError, OSError, SiLACallError) as exc:
                last = exc
                await self._stack.aclose()
                self._stack = contextlib.AsyncExitStack()
                await asyncio.sleep(2.0)
            else:
                return
        message = f"The connector did not come back within {timeout_s:g} s ({last})."
        raise RuntimeError(message)

    async def _run_step(self, level: str, name: str, step: Callable[[], Awaitable[str]]) -> None:
        started = time.monotonic()
        try:
            detail = await step()
            outcome = "PASS"
        except _Skip as skip:
            outcome, detail = "SKIP", str(skip)
        except _Warn as warning:
            outcome, detail = "WARN", str(warning)
        except Exception as exc:  # noqa: BLE001 - a failed step is reported, the run continues
            outcome, detail = "FAIL", f"{type(exc).__name__}: {exc}"
        result = StepResult(level, name, outcome, detail, round(time.monotonic() - started, 1))
        self.results.append(result)
        self._operator.say(f"[{outcome:4}] {name}" + (f" - {detail}" if detail else ""))

    def _steps(self, level: str) -> list[tuple[str, Callable[[], Awaitable[str]]]]:
        steps = {
            "smoke": [
                ("Connector status", self._smoke_status),
                ("HTTP API and runtime identity", self._smoke_http),
                ("Pipettes", self._smoke_pipettes),
                ("Gripper", self._smoke_gripper),
                ("Module status", self._smoke_modules),
            ],
            "motion": [
                ("Deck clear confirmation", self._motion_confirm),
                ("Home all axes", self._motion_home),
                ("Emergency stop interrupts a move", self._motion_halt),
                ("Physical E-stop is reported", self._motion_physical_estop),
            ],
            "recovery": [
                ("Reconcile tip state", self._recovery_tips),
                ("Recovery gates survive a restart", self._recovery_restart),
            ],
            "modules": [
                ("Module unplug and replug", self._modules_replug),
                ("New module type is registered automatically", self._modules_hot_plug),
            ],
        }
        return steps[level]

    # ---------------------------------------------------------------- helpers

    @property
    def client(self) -> FlexClient:
        """The connected client."""
        if self._client is None:
            message = "Not connected."
            raise RuntimeError(message)
        return self._client

    async def _status(self) -> object:
        return await self.client.subscribe_first("RobotStatusProvider", "Status")

    async def _state(self) -> str:
        return enum_name(field(await self._status(), "state"))

    async def _wait_for(
        self, predicate: Callable[[object], bool], *, timeout_s: float, interval_s: float = 0.5
    ) -> object:
        deadline = time.monotonic() + timeout_s
        status = await self._status()
        while not predicate(status):
            if time.monotonic() >= deadline:
                message = f"timed out after {timeout_s:g} s; last state {enum_name(field(status, 'state'))}"
                raise TimeoutError(message)
            await asyncio.sleep(interval_s)
            status = await self._status()
        return status

    def _require_deck_clear(self) -> None:
        if not self._deck_clear:
            raise _Skip("the deck-clear confirmation was skipped")

    def _motion_mount(self) -> str:
        if self._pipette_mounts:
            return self._pipette_mounts[0]
        return "GRIPPER" if self._gripper else "LEFT"

    # ------------------------------------------------------------------ smoke

    async def _smoke_status(self) -> str:
        status = await self._status()
        state = enum_name(field(status, "state"))
        details = [f"state {state}", f"E-stop {enum_name(field(status, 'emergency_stop'))}"]
        if field(status, "simulating") and not self._allow_simulator:
            message = "the connector is running the SIMULATOR, not the robot hardware"
            raise RuntimeError(message)
        guidance = field(status, "recovery_guidance")
        if guidance:
            details.append(f"recovery: {guidance}")
        if state in {"HARDWARE_ERROR", "INITIALIZING"}:
            raise _Warn("; ".join(details))
        return "; ".join(details)

    async def _smoke_http(self) -> str:
        if self._http_get is None:
            raise _Skip("no HTTP API in this mode")
        health = self._http_get("/health")
        model = field(health, "robot_model") if isinstance(health, dict) else None
        if model != "OT-3 Standard":
            message = f"/health reported robot_model {model!r}"
            raise RuntimeError(message)
        runtime = self._http_get("/unitelabs/runtime")
        contract = field(runtime, "runtimeContractId") if isinstance(runtime, dict) else "?"
        return f"Flex HTTP API healthy; runtime contract {contract}"

    async def _smoke_pipettes(self) -> str:
        response = await self.client.run("PipetteController", "GetAttachedPipettes")
        pipettes = next(iter(response.values()))
        attached = [pipette for pipette in pipettes if field(pipette, "attached")]
        self._pipette_mounts = [enum_name(field(pipette, "mount")) for pipette in attached]
        if not attached:
            raise _Warn("no pipette attached")
        return ", ".join(f"{enum_name(field(p, 'mount'))}: {field(p, 'model')}" for p in attached)

    async def _smoke_gripper(self) -> str:
        status = await self.client.read("GripperController", "Status")
        self._gripper = bool(field(status, "attached"))
        if not self._gripper:
            raise _Skip("no gripper attached")
        return f"{field(status, 'model')} ({field(status, 'gripper_id')})"

    async def _smoke_modules(self) -> str:
        status = await self._status()
        modules = list(field(status, "modules"))
        lines = []
        for module in modules:
            module_type = str(field(module, "module_type"))
            feature = MODULE_FEATURES.get(module_type)
            if not field(module, "connected"):
                message = f"{module_type} {field(module, 'serial_number')} is registered but disconnected"
                raise RuntimeError(message)
            if feature is not None:
                await self.client.subscribe_first(feature, "Status")
            lines.append(f"{module_type} {field(module, 'serial_number')}")
        unregistered = list(field(status, "unregistered_modules"))
        if unregistered:
            raise _Warn(f"attached after startup, not yet registered: {', '.join(unregistered)}")
        return ", ".join(lines) if lines else "no modules attached"

    # ----------------------------------------------------------------- motion

    async def _motion_confirm(self) -> str:
        if not self._operator.ask(
            "Confirm the deck is clear of loose items, tips are not near labware, the door is closed, and you can "
            "reach the E-stop. The gantry will home and make one slow 10 cm move at the top of its travel."
        ):
            raise _Skip("operator skipped motion tests")
        self._deck_clear = True
        return "confirmed by operator"

    async def _motion_home(self) -> str:
        self._require_deck_clear()
        await self.client.run("MotionController", "Home", timeout_s=180.0)
        await self._wait_for(lambda status: enum_name(field(status, "state")) == "READY", timeout_s=10.0)
        return "homed; state READY"

    async def _motion_halt(self) -> str:
        self._require_deck_clear()
        from .features.motion_control import Mount

        mount = Mount[self._motion_mount()]
        params = {"mount": mount, "delta_x": -abs(self._move_distance), "delta_y": 0.0, "delta_z": 0.0}
        move = asyncio.create_task(
            self.client.run("MotionController", "MoveRelative", {**params, "speed": self._move_speed})
        )
        await asyncio.sleep(1.5)
        if move.done():
            await move
            await self.client.run("MotionController", "Home", timeout_s=180.0)
            raise _Warn("the move finished before the stop arrived; lower --move-speed to retest")
        await self.client.run("MotionController", "EmergencyStop")
        try:
            return await self._check_halt(move, params)
        except Exception:
            await self._best_effort_home()
            raise

    async def _check_halt(self, move: asyncio.Task[dict[str, object]], params: dict[str, object]) -> str:
        try:
            await move
        except SiLACallError as exc:
            if exc.identifier != "OperationHaltedError":
                raise
        else:
            message = "the interrupted move reported success"
            raise RuntimeError(message)
        status = await self._wait_for(
            lambda status: enum_name(field(status, "state")) == "RECOVERY_REQUIRED", timeout_s=5.0
        )
        try:
            await self.client.run("MotionController", "MoveRelative", {**params, "delta_x": 0.0, "speed": 0.0})
        except SiLACallError as exc:
            if exc.identifier != "RecoveryRequiredError":
                raise
        else:
            message = "motion was accepted while the recovery gate was set"
            raise RuntimeError(message)
        await self.client.run("MotionController", "Home", timeout_s=180.0)
        await self._wait_for(lambda status: enum_name(field(status, "state")) == "READY", timeout_s=10.0)
        guidance = field(status, "recovery_guidance")
        return f"move halted with OperationHaltedError; gate blocked motion ({guidance}); Home cleared it"

    async def _best_effort_home(self) -> None:
        """After a failed motion step, try to leave the robot homed and READY for the next step."""
        with contextlib.suppress(Exception):
            if await self._state() == "RECOVERY_REQUIRED":
                self._operator.say("    (homing to clear the recovery gate before the next step)")
                await self.client.run("MotionController", "Home", timeout_s=180.0)

    async def _motion_physical_estop(self) -> str:
        self._require_deck_clear()
        if not self._operator.ask("Press and HOLD the physical E-stop down, then press Enter."):
            raise _Skip("operator skipped the physical E-stop test")
        seen: list[str] = []

        def estop(status: object) -> str:
            value = enum_name(field(status, "emergency_stop"))
            if not seen or seen[-1] != value:
                seen.append(value)
            return value

        try:
            status = await self._wait_for(
                lambda status: estop(status) in {"PHYSICALLY_ENGAGED", "LOGICALLY_ENGAGED"}, timeout_s=15.0
            )
        except TimeoutError:
            message = (
                f"the connector never reported the E-stop as engaged (EmergencyStop values seen: {', '.join(seen)}). "
                "Make sure the pendant is plugged in and stays pressed when you press Enter; if the touchscreen "
                "showed an E-stop alert, the connector is not reading the E-stop state."
            )
            raise RuntimeError(message) from None
        if estop(status) == "PHYSICALLY_ENGAGED" and enum_name(field(status, "state")) != "HARDWARE_ERROR":
            message = f"state was {enum_name(field(status, 'state'))} with the E-stop engaged"
            raise RuntimeError(message)
        if not self._operator.ask("Release (twist) the E-stop, then press Enter."):
            raise _Skip("operator stopped after engaging the E-stop")
        status = await self._wait_for(
            lambda status: estop(status) in {"LOGICALLY_ENGAGED", "DISENGAGED"}, timeout_s=15.0
        )
        if estop(status) == "LOGICALLY_ENGAGED":
            # A released E-stop stays latched until acknowledged (touchscreen "Resume" or HTTP).
            if self._acknowledge_estop is not None:
                self._acknowledge_estop()
            elif not self._operator.ask("Acknowledge the E-stop on the touchscreen, then press Enter."):
                raise _Skip("E-stop left latched")
            await self._wait_for(lambda status: estop(status) == "DISENGAGED", timeout_s=15.0)
        await self.client.run("MotionController", "Home", timeout_s=180.0)
        await self._wait_for(lambda status: enum_name(field(status, "state")) == "READY", timeout_s=10.0)
        return f"E-stop states {' -> '.join(seen)}; reported as HARDWARE_ERROR; recovered after release and Home"

    # --------------------------------------------------------------- recovery

    async def _recovery_tips(self) -> str:
        from .features._pipette_types import PipetteMount

        if not self._pipette_mounts:
            raise _Skip("no pipette attached")
        results = []
        for mount_name in self._pipette_mounts:
            mount = PipetteMount[mount_name]
            sensor = next(iter((await self.client.call("TipController", "GetTipPresence", {"mount": mount})).values()))
            reconciled = await self.client.call(
                "TipController", "ReconcileTipState", {"mount": mount, "tip_length": 95.6}
            )
            presence = next(iter(reconciled.values()))
            if enum_name(presence) != enum_name(sensor):
                message = f"{mount_name}: sensor {enum_name(sensor)} but reconciled {enum_name(presence)}"
                raise RuntimeError(message)
            results.append(f"{mount_name} {enum_name(presence)}")
        status = await self._status()
        if list(field(status, "tip_reconciliation_required")):
            message = "tip quarantine still reported after reconciliation"
            raise RuntimeError(message)
        return "; ".join(results)

    async def _recovery_restart(self) -> str:
        self._require_deck_clear()
        if self._ssh is None:
            raise _Skip("needs SSH to restart the connector service")
        await self.client.run("MotionController", "EmergencyStop")
        self._ssh("systemctl restart sila2-connector")
        await self._reconnect(timeout_s=self._timeouts["restart"])
        status = await self._status()
        if not field(status, "rehome_required"):
            raise _Warn("the re-home gate was not restored; set recovery_state_path in the connector config")
        await self.client.run("MotionController", "Home", timeout_s=180.0)
        await self._wait_for(lambda status: enum_name(field(status, "state")) == "READY", timeout_s=10.0)
        return "gate persisted across a service restart and cleared after Home"

    # ---------------------------------------------------------------- modules

    async def _modules_replug(self) -> str:
        status = await self._status()
        modules = list(field(status, "modules"))
        if not modules:
            raise _Skip("no module registered")
        module_type = str(field(modules[0], "module_type"))
        serial = str(field(modules[0], "serial_number"))

        def connected(value: object) -> bool:
            return any(
                str(field(module, "module_type")) == module_type and field(module, "connected")
                for module in field(value, "modules")
            )

        if not self._operator.ask(f"Unplug the USB cable of the {module_type} ({serial}), then press Enter."):
            raise _Skip("operator skipped")
        await self._wait_for(lambda value: not connected(value), timeout_s=self._timeouts["module"])
        if not self._operator.ask(f"Plug the {module_type} back in, then press Enter."):
            raise _Skip("operator stopped with the module unplugged")
        await self._wait_for(connected, timeout_s=self._timeouts["module"])
        feature = MODULE_FEATURES.get(module_type)
        if feature is not None:
            await self.client.subscribe_first(feature, "Status")
        return f"{module_type} reported disconnected, then usable again without a restart"

    async def _modules_hot_plug(self) -> str:
        if not self._operator.ask(
            "Plug in a module of a type that is NOT registered yet (or press s to skip), then press Enter."
        ):
            raise _Skip("operator skipped")
        before = set(await self.client.implemented_features())
        status = await self._wait_for(
            lambda value: bool(field(value, "restart_pending")) or bool(list(field(value, "unregistered_modules"))),
            timeout_s=self._timeouts["module"],
        )
        if not field(status, "restart_pending"):
            await self._wait_for(lambda value: bool(field(value, "restart_pending")), timeout_s=30.0)
        await asyncio.sleep(3.0)
        await self._reconnect(timeout_s=self._timeouts["restart"])
        added = sorted(set(await self.client.implemented_features()) - before)
        if not added:
            message = "the connector restarted but no new feature was registered"
            raise RuntimeError(message)
        return f"registered after automatic restart: {', '.join(added)}"


# ----------------------------------------------------------------- reporting


def summarize(results: list[StepResult]) -> tuple[str, bool]:
    """Return a printable summary and whether the run passed (no FAIL)."""
    counts = {
        outcome: sum(1 for result in results if result.outcome == outcome)
        for outcome in ("PASS", "WARN", "FAIL", "SKIP")
    }
    lines = [f"{result.outcome:4}  {result.level:8}  {result.name}" for result in results]
    lines.append("")
    lines.append(", ".join(f"{count} {outcome}" for outcome, count in counts.items()))
    return "\n".join(lines), counts["FAIL"] == 0


def write_report(results: list[StepResult], directory: Path, host: str) -> Path:
    """Write a JSON report next to the operator and return its path."""
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"flex-verify-{host.replace(':', '_')}-{stamp}.json"
    payload = {
        "host": host,
        "finished_at": stamp,
        "passed": summarize(results)[1],
        "results": [dataclasses.asdict(result) for result in results],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


__all__ = [
    "LEVELS",
    "ConsoleOperator",
    "StepResult",
    "UnattendedOperator",
    "Verifier",
    "summarize",
    "write_report",
]
