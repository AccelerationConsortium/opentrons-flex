"""Tests for module reconnect rebinding, new-module restart, and the observable_action decorator."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from opentrons.hardware_control.modules.types import ModuleType

from unitelabs.opentrons_flex import _RestartRequest, _RobotServerWatchdog
from unitelabs.opentrons_flex.features._progress import OperationPhase, observable_action
from unitelabs.opentrons_flex.io import FlexStackerController, ModuleNotRespondingError, ModuleOperationError
from unitelabs.opentrons_flex.io._module_base import ModuleControllerBase
from unitelabs.opentrons_flex.io.hot_plug import ModuleHotPlugMonitor


def _module(serial: str, module_type: ModuleType = ModuleType.HEATER_SHAKER) -> SimpleNamespace:
    return SimpleNamespace(MODULE_TYPE=module_type, device_info={"serial": serial, "model": "m", "version": "1"})


class _Controller(ModuleControllerBase):
    async def get_serial(self) -> str:
        return self._module.device_info["serial"]


# ------------------------------------------------------------------ rebinding


async def test_controller_follows_a_replugged_module_by_serial() -> None:
    original = _module("HS-1")
    attached = [original]
    controller = _Controller.from_module(original, attached_modules=lambda: attached)

    attached[:] = []
    assert not controller.module_connected
    with pytest.raises(ModuleNotRespondingError, match="Reconnect its USB cable"):
        await controller.get_serial()

    replugged = _module("HS-1")
    attached[:] = [_module("TM-1", ModuleType.TEMPERATURE), replugged]
    assert controller.module_connected
    assert await controller.get_serial() == "HS-1"
    assert controller._module is replugged


async def test_controller_never_adopts_a_different_unit() -> None:
    attached = [_module("HS-1")]
    controller = _Controller.from_module(attached[0], attached_modules=lambda: attached)

    attached[:] = [_module("HS-2")]

    assert not controller.module_connected
    with pytest.raises(ModuleNotRespondingError, match="does not switch to another unit"):
        await controller.get_serial()


async def test_module_without_serial_adopts_the_single_unclaimed_unit() -> None:
    anonymous = SimpleNamespace(MODULE_TYPE=ModuleType.HEATER_SHAKER, device_info={})
    attached = [anonymous]
    controller = _Controller.from_module(anonymous, attached_modules=lambda: attached)

    attached[:] = [_module("HS-2")]

    assert await controller.get_serial() == "HS-2"


async def test_a_module_owned_by_another_controller_is_never_taken() -> None:
    first = SimpleNamespace(MODULE_TYPE=ModuleType.HEATER_SHAKER, device_info={})
    second = _module("HS-2")
    attached = [first, second]
    controller = _Controller.from_module(first, attached_modules=lambda: attached)
    owner = _Controller.from_module(second, attached_modules=lambda: attached)

    attached[:] = [second]

    with pytest.raises(ModuleNotRespondingError):
        await controller.get_serial()
    assert await owner.get_serial() == "HS-2"


async def test_status_reads_never_rebind() -> None:
    original = _module("HS-1")
    attached = [original]
    controller = _Controller.from_module(original, attached_modules=lambda: attached)
    replugged = _module("HS-1")
    attached[:] = [replugged]

    assert controller.module_connected
    assert controller._bound_module is original
    await controller.get_serial()
    assert controller._bound_module is replugged


async def test_ambiguous_replacement_is_a_defined_error() -> None:
    anonymous = SimpleNamespace(MODULE_TYPE=ModuleType.HEATER_SHAKER, device_info={})
    attached = [anonymous]
    controller = _Controller.from_module(anonymous, attached_modules=lambda: attached)

    attached[:] = [_module("HS-2"), _module("HS-3")]

    with pytest.raises(ModuleOperationError, match="exactly one remains"):
        await controller.get_serial()
    assert not controller.module_connected


async def test_replugged_stacker_requires_a_full_home() -> None:
    original = _module("FS-1", ModuleType.FLEX_STACKER)
    attached = [original]
    controller = FlexStackerController.from_module(original, attached_modules=lambda: attached)
    replugged = _module("FS-1", ModuleType.FLEX_STACKER)
    attached[:] = [replugged]

    assert await controller.is_connected()
    assert controller._recovery_state is not None
    assert controller._recovery_state.full_home_required


# ---------------------------------------------------------------- hot plug


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _monitor(attached: list[tuple[str, str]], *, idle: bool = True, restart=None, clock=None):
    requests: list[str] = []
    monitor = ModuleHotPlugMonitor(
        registered_types={"HEATER_SHAKER"},
        attached=lambda: attached,
        idle=lambda: idle,
        restart=restart if restart is not None else requests.append,
        stable_s=5.0,
        clock=clock or _Clock(),
    )
    return monitor, requests


def test_new_module_type_restarts_after_it_is_stable_and_idle() -> None:
    clock = _Clock()
    attached = [("HEATER_SHAKER", "HS-1"), ("THERMOCYCLER", "TC-1")]
    monitor, requests = _monitor(attached, clock=clock)

    assert monitor.check() is None
    assert monitor.pending_types == ("THERMOCYCLER",)
    clock.now = 5.0
    reason = monitor.check()

    assert reason is not None and "THERMOCYCLER" in reason
    assert requests == [reason]
    assert monitor.check() == reason
    assert len(requests) == 1


def test_replugging_a_registered_type_never_restarts() -> None:
    clock = _Clock()
    monitor, requests = _monitor([("HEATER_SHAKER", "HS-2")], clock=clock)
    clock.now = 60.0

    assert monitor.check() is None
    assert requests == []


def test_restart_waits_for_idle() -> None:
    clock = _Clock()
    attached = [("THERMOCYCLER", "TC-1")]
    busy = {"idle": False}
    requests: list[str] = []
    monitor = ModuleHotPlugMonitor(
        registered_types=set(),
        attached=lambda: attached,
        idle=lambda: busy["idle"],
        restart=requests.append,
        stable_s=1.0,
        clock=clock,
    )
    monitor.check()
    clock.now = 2.0
    assert monitor.check() is None
    busy["idle"] = True
    assert monitor.check() is not None


def test_duplicates_and_report_mode_never_restart() -> None:
    clock = _Clock()
    duplicate, requests = _monitor([("THERMOCYCLER", "TC-1"), ("THERMOCYCLER", "TC-2")], clock=clock)
    duplicate.check()
    clock.now = 10.0
    assert duplicate.check() is None
    assert requests == []

    report = ModuleHotPlugMonitor(
        registered_types=set(),
        attached=lambda: [("THERMOCYCLER", "TC-1")],
        idle=lambda: True,
        restart=None,
        stable_s=0.0,
    )
    assert report.check() is None


def test_unplugged_before_stable_does_not_restart() -> None:
    clock = _Clock()
    attached = [("THERMOCYCLER", "TC-1")]
    monitor, requests = _monitor(attached, clock=clock)
    monitor.check()
    attached.clear()
    clock.now = 3.0
    monitor.check()
    attached.append(("THERMOCYCLER", "TC-1"))
    clock.now = 6.0
    assert monitor.check() is None, "the stability timer restarts after a disconnect"
    assert requests == []


def test_restart_request_is_one_shot(monkeypatch: pytest.MonkeyPatch) -> None:
    import unitelabs.opentrons_flex as connector

    signals: list[str] = []
    monkeypatch.setattr(connector, "_request_restart", lambda: signals.append("sigterm"))
    restart = _RestartRequest()
    restart.request("register THERMOCYCLER")
    restart.request("again")

    assert restart.reason == "register THERMOCYCLER"
    assert signals == ["sigterm"]


async def test_watchdog_uses_the_shared_restart_request(monkeypatch: pytest.MonkeyPatch) -> None:
    import unitelabs.opentrons_flex as connector

    signals: list[str] = []
    monkeypatch.setattr(connector, "_request_restart", lambda: signals.append("sigterm"))
    restart = _RestartRequest()
    watchdog = _RobotServerWatchdog(MagicMock(should_exit=False), grace_s=0.01, restart=restart)

    task = asyncio.create_task(asyncio.sleep(0))
    watchdog.watch(task)
    await task
    await asyncio.sleep(0.05)

    assert restart.reason is not None and "robot-server" in restart.reason
    assert signals == ["sigterm"]


# ------------------------------------------------------- observable_action


class _Feature:
    @observable_action("Homing {mount}.", "{mount} homed.", "{mount} home cancelled.", running="Moving {mount}.")
    async def home(self, mount: str, *, status: object, intermediate: object) -> str:
        return f"done {mount}"

    @observable_action("Start.", "Done.", "Cancelled.")
    async def fail(self, *, status: object, intermediate: object) -> None:
        raise RuntimeError("stall")


async def test_observable_action_formats_messages_from_arguments() -> None:
    intermediate = MagicMock()

    result = await _Feature().home("LEFT", status=MagicMock(), intermediate=intermediate)

    assert result == "done LEFT"
    sent = [call.args[0] for call in intermediate.send.call_args_list]
    assert [(update.phase, update.message) for update in sent] == [
        (OperationPhase.STARTING, "Homing LEFT."),
        (OperationPhase.RUNNING, "Moving LEFT."),
        (OperationPhase.COMPLETED, "LEFT homed."),
    ]


async def test_observable_action_reports_failure_and_reraises() -> None:
    intermediate = MagicMock()

    with pytest.raises(RuntimeError, match="stall"):
        await _Feature().fail(status=MagicMock(), intermediate=intermediate)

    assert intermediate.send.call_args_list[-1].args[0].phase is OperationPhase.FAILED


def test_observable_action_keeps_the_sila_signature() -> None:
    import inspect

    parameters = inspect.signature(_Feature.home).parameters
    assert {"mount", "status", "intermediate"} <= set(parameters)


# ------------------------------------------------------- restart idle check


def _status(state: str = "READY", run_status: str = "idle", direct: bool = True) -> SimpleNamespace:
    from unitelabs.opentrons_flex.io.robot_status import RobotState

    snapshot = SimpleNamespace(
        state=RobotState[state], run=SimpleNamespace(run_id="", status=run_status, direct_control_allowed=direct)
    )
    return SimpleNamespace(snapshot=lambda: snapshot)


async def test_restart_requires_a_fully_idle_robot() -> None:
    from unitelabs.opentrons_flex import _safe_to_restart
    from unitelabs.opentrons_flex.features import _progress

    lock = asyncio.Lock()
    hardware = SimpleNamespace(lock=lock)
    assert _safe_to_restart(_status(), hardware)

    assert not _safe_to_restart(_status("RECOVERY_REQUIRED"), hardware), "a restart could drop a recovery gate"
    assert not _safe_to_restart(_status(run_status="paused", direct=False), hardware)
    assert not _safe_to_restart(_status(run_status="awaiting-recovery"), hardware)
    assert _safe_to_restart(_status(run_status="succeeded"), hardware)

    with _progress._counted():
        assert not _safe_to_restart(_status(), hardware), "an observable command is running"

    module = _module("TC-1", ModuleType.THERMOCYCLER)
    controller = _Controller.from_module(module, lock=lock, attached_modules=lambda: [module])
    controller._profile_active = True
    assert not _safe_to_restart(_status(), hardware), "a thermocycler profile is running"
    controller._profile_active = False
    controller._block_control_lock = asyncio.Lock()
    await controller._block_control_lock.acquire()
    assert not _safe_to_restart(_status(), hardware), "a temperature wait holds its control lock"
    controller._block_control_lock.release()
    assert _safe_to_restart(_status(), hardware)

    async with lock:
        assert not _safe_to_restart(_status(), hardware)
