"""Simulation-mode tests for the Flex SiLA features.

Builds each feature on a real ``OT3API`` simulator and calls its commands
directly, exercising the feature -> controller -> simulator chain. SiLA
wire-format / feature-definition generation is covered separately in CI with the
real CDK; here the CDK may be a conftest stub (see tests/conftest.py), so these
tests assert behaviour, not SiLA serialisation.
"""

import asyncio

import pytest
import pytest_asyncio
from opentrons.hardware_control.ot3api import OT3API

from unitelabs.opentrons_flex.features.gripper import GripperFeature
from unitelabs.opentrons_flex.features.motion_control import Lights, MotionControlFeature, Mount, Position
from unitelabs.opentrons_flex.features.pipette import PipetteFeature
from unitelabs.opentrons_flex.io import (
    FlexGripperController,
    FlexMotionController,
    GripperNotAttachedError,
)


@pytest_asyncio.fixture
async def api() -> OT3API:
    api = await OT3API.build_hardware_simulator()
    yield api
    await api.clean_up()


@pytest_asyncio.fixture
async def motion(api: OT3API) -> MotionControlFeature:
    return MotionControlFeature(FlexMotionController.from_api(api, lock=asyncio.Lock()))


@pytest_asyncio.fixture
async def pipette(api: OT3API) -> PipetteFeature:
    return PipetteFeature(FlexMotionController.from_api(api, lock=asyncio.Lock()))


@pytest_asyncio.fixture
async def gripper(api: OT3API) -> GripperFeature:
    return GripperFeature(FlexGripperController.from_api(api, lock=asyncio.Lock()))


# ── Motion ──────────────────────────────────────────────────────────────────


async def test_home_and_get_position(motion: MotionControlFeature):
    await motion.home()
    pos = await motion.get_position(Mount.LEFT)
    assert isinstance(pos, Position)


async def test_move_relative_returns_offset_position(motion: MotionControlFeature):
    await motion.home()
    start = await motion.get_position(Mount.LEFT)
    moved = await motion.move_relative(Mount.LEFT, delta_x=-5, delta_y=-5, delta_z=-2)
    assert isinstance(moved, Position)
    assert moved.x == pytest.approx(start.x - 5, abs=1e-3)
    assert moved.z == pytest.approx(start.z - 2, abs=1e-3)


async def test_set_lights_returns_lights(motion: MotionControlFeature):
    result = await motion.set_lights(button=True, rails=False)
    assert isinstance(result, Lights)


# ── Pipette ─────────────────────────────────────────────────────────────────


async def test_get_attached_pipettes_reports_both_mounts(pipette: PipetteFeature):
    pipettes = await pipette.get_attached_pipettes()
    assert {p.mount for p in pipettes} == {Mount.LEFT, Mount.RIGHT}


async def test_no_pipette_reports_not_attached(pipette: PipetteFeature):
    pipettes = await pipette.get_attached_pipettes()
    assert all(p.attached is False for p in pipettes)
    assert all(p.model == "" for p in pipettes)  # no sentinel garbage when absent


# ── Gripper ─────────────────────────────────────────────────────────────────


async def test_grip_without_gripper_raises(gripper: GripperFeature):
    with pytest.raises(GripperNotAttachedError):
        await gripper.grip(force=15.0)


# ── Wiring ──────────────────────────────────────────────────────────────────


async def test_features_construct_without_error(api: OT3API):
    lock = asyncio.Lock()
    motion = FlexMotionController.from_api(api, lock=lock)
    gripper = FlexGripperController.from_api(api, lock=lock)
    # Construction runs each feature's SiLA metadata setup (real CDK) or the stub.
    assert MotionControlFeature(motion) is not None
    assert PipetteFeature(motion) is not None
    assert GripperFeature(gripper) is not None
