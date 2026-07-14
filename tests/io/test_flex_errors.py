"""Tests for the Flex defined-error translation.

Confirms the io layer maps the real OT3API exception types to the connector's
defined SiLA errors, and that the gripper guard fires when no gripper is attached.
Offline: depends only on opentrons.
"""

import asyncio

import pytest
import pytest_asyncio
from opentrons.hardware_control.errors import OutOfBoundsMove
from opentrons.hardware_control.ot3api import OT3API
from opentrons.hardware_control.types import FailedTipStateCheck, TipStateType
from opentrons.types import PipetteNotAttachedError as OpentronsPipetteNotAttachedError
from opentrons_shared_data.errors.exceptions import (
    PositionEstimationInvalidError,
    PositionUnknownError,
    StallOrCollisionDetectedError,
    TipDropFailedError,
    TipPickupFailedError,
)

from unitelabs.opentrons_flex.io import FlexGripperController, GripperNotAttachedError
from unitelabs.opentrons_flex.io._errors import (
    CalibrationFailedError,
    CalibrationProbeNotAttachedError,
    MovementOutOfBoundsError,
    NotHomedError,
    PipetteNotAttachedError,
    StallDetectedError,
    TipDropError,
    TipPickupError,
    TipStateError,
    translate_motion_errors,
    translate_tip_errors,
)
from unitelabs.opentrons_flex.io.calibration import FlexCalibrationController


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (PositionUnknownError(), NotHomedError),
        (PositionEstimationInvalidError(), NotHomedError),
        (OutOfBoundsMove(message="m", detail={}), MovementOutOfBoundsError),
        (StallOrCollisionDetectedError(), StallDetectedError),
    ],
)
async def test_translate_motion_errors(raised: Exception, expected: type[Exception]):
    @translate_motion_errors
    async def boom():
        raise raised

    with pytest.raises(expected):
        await boom()


async def test_translate_motion_errors_passes_through_unrelated():
    @translate_motion_errors
    async def boom():
        raise ValueError("unrelated")

    with pytest.raises(ValueError, match="unrelated"):
        await boom()


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (OpentronsPipetteNotAttachedError("missing"), PipetteNotAttachedError),
        (TipPickupFailedError("pickup failed"), TipPickupError),
        (TipDropFailedError("drop failed"), TipDropError),
        (FailedTipStateCheck(TipStateType.PRESENT, TipStateType.ABSENT), TipStateError),
    ],
)
async def test_translate_tip_errors(raised: Exception, expected: type[Exception]):
    @translate_tip_errors
    async def boom():
        raise raised

    with pytest.raises(expected):
        await boom()


async def test_translate_tip_errors_passes_through_unrelated():
    @translate_tip_errors
    async def boom():
        raise ValueError("unrelated")

    with pytest.raises(ValueError, match="unrelated"):
        await boom()


@pytest_asyncio.fixture
async def gripper() -> FlexGripperController:
    api = await OT3API.build_hardware_simulator()
    ctrl = FlexGripperController.from_api(api, lock=asyncio.Lock())
    yield ctrl
    await api.clean_up()


async def test_grip_without_gripper_raises_defined_error(gripper: FlexGripperController):
    assert gripper.attached is False
    with pytest.raises(GripperNotAttachedError):
        await gripper.grip(force_newtons=15)


async def test_ungrip_without_gripper_raises_defined_error(gripper: FlexGripperController):
    with pytest.raises(GripperNotAttachedError):
        await gripper.ungrip()


def test_calibration_translate_probe_missing():
    exc = RuntimeError("calibration probe not attached")
    assert isinstance(FlexCalibrationController._translate(exc), CalibrationProbeNotAttachedError)


def test_calibration_translate_generic_failure():
    exc = RuntimeError("edge detection deviation too large")
    assert isinstance(FlexCalibrationController._translate(exc), CalibrationFailedError)
