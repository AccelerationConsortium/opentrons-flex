"""Tests for system-wide defined errors: busy lock, halts, recovery gates, and the hardware fallback."""

import asyncio

import pytest
import pytest_asyncio
from opentrons.hardware_control.ot3api import OT3API
from opentrons.hardware_control.types import OT3Mount, TipStateType
from opentrons_shared_data.errors.codes import ErrorCodes
from opentrons.types import Point
from opentrons_shared_data.errors.exceptions import EnumeratedError, TipPickupFailedError

from unitelabs.opentrons_flex.io import (
    SYSTEM_ERRORS,
    FlexMotionController,
    HardwareBusyError,
    HardwareError,
    NotHomedError,
    OperationHaltedError,
    RecoveryRequiredError,
    TipPickupError,
    TipStateError,
)
from unitelabs.opentrons_flex.io._errors import (
    mark_task_halt_cancelled,
    translate_motion_errors,
    translate_system_errors,
    translate_tip_errors,
)
from unitelabs.opentrons_flex.io.hardware_proxy import _TimedLock
from unitelabs.opentrons_flex.io.recovery_state import HardwareRecoveryState


class _UnmappedHardwareFault(EnumeratedError):
    def __init__(self) -> None:
        super().__init__(ErrorCodes.GENERAL_ERROR, "motor driver fault", {"node": "gantry-x"})


@translate_motion_errors
async def _raise_unmapped() -> None:
    raise _UnmappedHardwareFault


@translate_tip_errors
@translate_motion_errors
async def _raise_tip_pickup_failure() -> None:
    raise TipPickupFailedError(message="tip not seated")


async def test_unmapped_opentrons_error_becomes_hardware_error_with_code() -> None:
    with pytest.raises(HardwareError) as excinfo:
        await _raise_unmapped()

    message = str(excinfo.value)
    assert "GENERAL_ERROR" in message
    assert "motor driver fault" in message
    assert "gantry-x" in message


async def test_inner_translator_does_not_preempt_a_more_specific_outer_mapping() -> None:
    with pytest.raises(TipPickupError, match="tip-rack seating"):
        await _raise_tip_pickup_failure()


async def test_halt_cancellation_becomes_operation_halted_error() -> None:
    started = asyncio.Event()

    @translate_system_errors
    async def long_action() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(long_action())
    await started.wait()
    mark_task_halt_cancelled(task)
    task.cancel()

    with pytest.raises(OperationHaltedError, match="emergency stop"):
        await task


async def test_halt_wrapped_by_the_hardware_layer_becomes_operation_halted_error() -> None:
    """On a real Flex, opentrons_hardware turns the halt's CancelledError into PythonException."""
    from opentrons_shared_data.errors.exceptions import PythonException

    started = asyncio.Event()

    @translate_motion_errors
    async def long_move() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            raise PythonException(exc) from exc

    task = asyncio.create_task(long_move())
    await started.wait()
    mark_task_halt_cancelled(task)
    task.cancel()

    with pytest.raises(OperationHaltedError):
        await task
    assert not task.cancelled()


async def test_hardware_error_without_a_halt_is_still_a_hardware_error() -> None:
    from opentrons_shared_data.errors.exceptions import PythonException

    @translate_motion_errors
    async def failing_move() -> None:
        raise PythonException(RuntimeError("stall"))

    with pytest.raises(HardwareError):
        await failing_move()


async def test_client_cancellation_still_propagates_as_cancellation() -> None:
    started = asyncio.Event()

    @translate_system_errors
    async def long_action() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(long_action())
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_timed_lock_timeout_is_a_defined_busy_error() -> None:
    lock = asyncio.Lock()
    await lock.acquire()
    timed = _TimedLock(lock, timeout_s=0.01)

    with pytest.raises(HardwareBusyError) as excinfo:
        async with timed:
            pass

    assert isinstance(excinfo.value, TimeoutError)
    assert timed.locked()
    lock.release()
    assert not timed.locked()


def test_system_errors_are_declared_together() -> None:
    assert set(SYSTEM_ERRORS) == {HardwareBusyError, OperationHaltedError, RecoveryRequiredError, HardwareError}
    assert issubclass(RecoveryRequiredError, NotHomedError)


async def test_recovery_guidance_names_the_reconcile_command_instead_of_a_restart() -> None:
    state = HardwareRecoveryState()
    state.mark_halted()
    state.require_tip_reconciliation("LEFT")
    state.require_gripper_home()

    guidance = state.operator_guidance
    assert guidance.index("re-home") < guidance.index("ReconcileTipState") < guidance.index("HomeJaw")
    assert "restart" not in guidance


@pytest_asyncio.fixture
async def controller() -> FlexMotionController:
    api = await OT3API.build_hardware_simulator(
        attached_instruments={OT3Mount.LEFT: {"model": "p1000_single_v3.0", "id": "sim-left"}}
    )
    await api.home()
    controller = FlexMotionController.from_api(api, lock=asyncio.Lock())
    yield controller
    await api.clean_up()


async def test_recovery_gate_raises_recovery_required_error(controller: FlexMotionController) -> None:
    controller.recovery_state.require_tip_reconciliation("LEFT")

    with pytest.raises(RecoveryRequiredError, match="ReconcileTipState"):
        await controller.move_rel(OT3Mount.LEFT, delta=Point(0, 0, 0))


async def test_reconcile_tip_state_lifts_quarantine_without_moving(controller: FlexMotionController) -> None:
    controller.recovery_state.require_tip_reconciliation("LEFT")
    before = await controller.gantry_position(OT3Mount.LEFT)

    state = await controller.reconcile_tip_state(OT3Mount.LEFT, tip_length=95.6)

    assert state is TipStateType.ABSENT
    assert controller.recovery_state.operation_ready
    assert await controller.gantry_position(OT3Mount.LEFT) == before


async def test_reconcile_tip_state_rejects_invalid_tip_length(controller: FlexMotionController) -> None:
    with pytest.raises(TipStateError, match="Tip length"):
        await controller.reconcile_tip_state(OT3Mount.LEFT, tip_length=float("nan"))


async def test_cached_position_is_available_while_the_hardware_is_busy(controller: FlexMotionController) -> None:
    expected = await controller.gantry_position(OT3Mount.LEFT)
    await controller._lock._lock.acquire()
    try:
        assert controller.hardware_busy
        cached = await asyncio.wait_for(controller.cached_gantry_position(OT3Mount.LEFT), timeout=0.5)
    finally:
        controller._lock._lock.release()
    assert cached == expected


async def test_cached_position_is_none_before_homing() -> None:
    api = await OT3API.build_hardware_simulator()
    controller = FlexMotionController.from_api(api, lock=asyncio.Lock())
    try:
        assert await controller.cached_gantry_position(OT3Mount.LEFT) is None
    finally:
        await api.clean_up()
