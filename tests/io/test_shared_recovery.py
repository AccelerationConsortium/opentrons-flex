"""Cross-controller emergency-stop and recovery-gate tests."""

import asyncio

import pytest
import pytest_asyncio
from opentrons.hardware_control import ot3_calibration
from opentrons.hardware_control.ot3api import OT3API
from opentrons.hardware_control.types import OT3Mount

from unitelabs.opentrons_flex.io import (
    FlexCalibrationController,
    FlexGripperController,
    FlexMotionController,
    NotHomedError,
)


@pytest_asyncio.fixture
async def shared_controllers() -> tuple[
    OT3API,
    FlexMotionController,
    FlexGripperController,
    FlexCalibrationController,
]:
    api = await OT3API.build_hardware_simulator(
        attached_instruments={
            OT3Mount.GRIPPER: {"model": "gripperV1.3", "id": "sim-gripper"},
        }
    )
    await api.home()
    lock = asyncio.Lock()
    yield (
        api,
        FlexMotionController.from_api(api, lock=lock),
        FlexGripperController.from_api(api, lock=lock),
        FlexCalibrationController.from_api(api, lock=lock),
    )
    await api.clean_up()


async def test_halt_gates_gripper_and_calibration_until_full_home(
    shared_controllers: tuple[OT3API, FlexMotionController, FlexGripperController, FlexCalibrationController],
) -> None:
    _, motion, gripper, calibration = shared_controllers
    await motion.stop()

    with pytest.raises(NotHomedError, match="Fully re-home"):
        await gripper.grip(force_newtons=15.0)
    with pytest.raises(NotHomedError, match="Fully re-home"):
        await calibration.calibrate_deck(OT3Mount.LEFT, pipette_id="sim-left")

    await motion.home()
    await gripper.grip(force_newtons=15.0)


async def test_halt_during_grip_requires_full_home_then_jaw_home(
    shared_controllers: tuple[OT3API, FlexMotionController, FlexGripperController, FlexCalibrationController],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, motion, gripper, _ = shared_controllers
    grip_started = asyncio.Event()

    async def blocked_grip(*args: object, **kwargs: object) -> None:
        grip_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(api, "grip", blocked_grip)
    grip_task = asyncio.create_task(gripper.grip(force_newtons=15.0))
    await grip_started.wait()
    await motion.stop()

    with pytest.raises(asyncio.CancelledError):
        await grip_task
    assert gripper._recovery_state.gripper_home_required

    await motion.home()
    with pytest.raises(NotHomedError, match="HomeJaw"):
        await gripper.grip(force_newtons=15.0)

    await gripper.home_jaw()
    assert gripper._recovery_state.operation_ready


async def test_halt_cancels_active_multi_step_calibration(
    shared_controllers: tuple[OT3API, FlexMotionController, FlexGripperController, FlexCalibrationController],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, motion, _, calibration = shared_controllers
    calibration_started = asyncio.Event()

    async def blocked_calibration(*args: object, **kwargs: object) -> None:
        calibration_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ot3_calibration, "calibrate_belts", blocked_calibration)
    calibration_task = asyncio.create_task(calibration.calibrate_deck(OT3Mount.LEFT, pipette_id="sim-left"))
    await calibration_started.wait()
    await motion.stop()

    with pytest.raises(asyncio.CancelledError):
        await calibration_task
    assert calibration._recovery_state.rehome_required
