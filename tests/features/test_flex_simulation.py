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
from opentrons.hardware_control.types import OT3Mount

from unitelabs.opentrons_flex.features.calibration import CalibrationFeature, GripperJaw, PipetteMount
from unitelabs.opentrons_flex.features.gripper import GripperFeature
from unitelabs.opentrons_flex.features.motion_control import Lights, MotionControlFeature, Mount, Position
from unitelabs.opentrons_flex.features._progress import OperationProgress
from unitelabs.opentrons_flex.features.pipette import PipetteFeature
from unitelabs.opentrons_flex.features.tip_controller import PipetteMount as TipPipetteMount
from unitelabs.opentrons_flex.features.tip_controller import TipController, TipLocation, TipPresence
from unitelabs.opentrons_flex.io import (
    CalibrationFailedError,
    CalibrationProbeNotAttachedError,
    FlexCalibrationController,
    FlexGripperController,
    FlexMotionController,
    GripperNotAttachedError,
    NotHomedError,
    PipetteNotAttachedError,
    TipNotAttachedError,
)


class _Status:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def update(self, **kwargs) -> None:
        self.updates.append(kwargs)


class _Intermediate:
    def __init__(self) -> None:
        self.messages: list[OperationProgress] = []

    def send(self, message: OperationProgress) -> None:
        self.messages.append(message)


def _obs() -> tuple[_Status, _Intermediate]:
    return _Status(), _Intermediate()


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
async def attached_tip_controller() -> TipController:
    api = await OT3API.build_hardware_simulator(
        attached_instruments={
            OT3Mount.LEFT: {"model": "p1000_single_v3.0", "id": "sim-left"},
        }
    )
    await api.home()
    yield TipController(FlexMotionController.from_api(api, lock=asyncio.Lock()))
    await api.clean_up()


@pytest_asyncio.fixture
async def gripper(api: OT3API) -> GripperFeature:
    return GripperFeature(FlexGripperController.from_api(api, lock=asyncio.Lock()))


@pytest_asyncio.fixture
async def calibration(api: OT3API) -> CalibrationFeature:
    return CalibrationFeature(FlexCalibrationController.from_api(api, lock=asyncio.Lock()))


# ── Motion ──────────────────────────────────────────────────────────────────


async def test_home_and_get_position(motion: MotionControlFeature):
    status, intermediate = _obs()
    await motion.home(status=status, intermediate=intermediate)
    status, intermediate = _obs()
    pos = await motion.get_position(Mount.LEFT, status=status, intermediate=intermediate)
    assert isinstance(pos, Position)
    assert status.updates
    assert intermediate.messages


async def test_home_mount_reports_progress(motion: MotionControlFeature):
    status, intermediate = _obs()
    await motion.home_mount(Mount.LEFT, status=status, intermediate=intermediate)
    assert status.updates[-1]["progress"] == pytest.approx(1.0)
    assert intermediate.messages[-1].message == "LEFT mount home completed."


async def test_move_to_returns_requested_position(motion: MotionControlFeature):
    status, intermediate = _obs()
    await motion.home(status=status, intermediate=intermediate)
    status, intermediate = _obs()
    start = await motion.get_position(Mount.LEFT, status=status, intermediate=intermediate)
    target = Position(x=start.x - 10, y=start.y - 10, z=start.z - 5)
    status, intermediate = _obs()
    moved = await motion.move_to(
        Mount.LEFT,
        x=target.x,
        y=target.y,
        z=target.z,
        speed=0.0,
        status=status,
        intermediate=intermediate,
    )
    assert moved.x == pytest.approx(target.x)
    assert moved.y == pytest.approx(target.y)
    assert moved.z == pytest.approx(target.z)
    assert intermediate.messages[-1].message == "LEFT absolute move completed."


async def test_move_relative_returns_offset_position(motion: MotionControlFeature):
    status, intermediate = _obs()
    await motion.home(status=status, intermediate=intermediate)
    status, intermediate = _obs()
    start = await motion.get_position(Mount.LEFT, status=status, intermediate=intermediate)
    status, intermediate = _obs()
    moved = await motion.move_relative(
        Mount.LEFT,
        delta_x=-5,
        delta_y=-5,
        delta_z=-2,
        speed=0.0,
        status=status,
        intermediate=intermediate,
    )
    assert isinstance(moved, Position)
    assert moved.x == pytest.approx(start.x - 5, abs=1e-3)
    assert moved.z == pytest.approx(start.z - 2, abs=1e-3)
    assert intermediate.messages[-1].message.endswith("completed.")


async def test_set_lights_returns_lights(motion: MotionControlFeature):
    status, intermediate = _obs()
    result = await motion.set_lights(button=True, rails=False, status=status, intermediate=intermediate)
    assert isinstance(result, Lights)


@pytest.mark.parametrize(("button", "rails"), [(True, False), (False, True), (True, True), (False, False)])
async def test_lights_return_typed_state_and_rails_roundtrip(
    motion: MotionControlFeature,
    button: bool,
    rails: bool,
):
    status, intermediate = _obs()
    result = await motion.set_lights(button=button, rails=rails, status=status, intermediate=intermediate)
    # The OT3 simulator does not persist the status-bar button state, but it
    # does persist rail lights. Real button-light verification belongs in live
    # smoke testing.
    assert isinstance(result.button, bool)
    assert result.rails is rails
    assert await motion.lights() == result


# ── Pipette ─────────────────────────────────────────────────────────────────


async def test_get_attached_pipettes_reports_both_mounts(pipette: PipetteFeature):
    status, intermediate = _obs()
    pipettes = await pipette.get_attached_pipettes(status=status, intermediate=intermediate)
    assert {p.mount for p in pipettes} == {TipPipetteMount.LEFT, TipPipetteMount.RIGHT}


async def test_no_pipette_reports_not_attached(pipette: PipetteFeature):
    status, intermediate = _obs()
    pipettes = await pipette.get_attached_pipettes(status=status, intermediate=intermediate)
    assert all(p.attached is False for p in pipettes)
    assert all(p.model == "" for p in pipettes)  # no sentinel garbage when absent


async def test_empty_pipette_metadata_is_operator_safe(pipette: PipetteFeature):
    status, intermediate = _obs()
    pipettes = await pipette.get_attached_pipettes(status=status, intermediate=intermediate)
    assert all(p.name == "" for p in pipettes)
    assert all(p.pipette_id == "" for p in pipettes)
    assert all(p.channels == 0 for p in pipettes)
    assert all(p.min_volume == 0.0 for p in pipettes)
    assert all(p.max_volume == 0.0 for p in pipettes)
    assert all(p.has_tip is False for p in pipettes)


async def test_tip_lifecycle_round_trip(attached_tip_controller: TipController):
    assert await attached_tip_controller.get_tip_presence(TipPipetteMount.LEFT) is TipPresence.ABSENT
    point = await attached_tip_controller._controller.gantry_position(OT3Mount.LEFT)
    location = TipLocation(x=point.x, y=point.y, z=point.z)

    status, intermediate = _obs()
    picked_up = await attached_tip_controller.pick_up_tip(
        TipPipetteMount.LEFT,
        location=location,
        tip_length=95.6,
        prep_after=False,
        status=status,
        intermediate=intermediate,
    )
    assert picked_up is TipPresence.PRESENT
    assert intermediate.messages[-1].message == "Tip pickup on LEFT verified."

    point = await attached_tip_controller._controller.gantry_position(OT3Mount.LEFT)
    location = TipLocation(x=point.x, y=point.y, z=point.z)
    status, intermediate = _obs()
    dropped = await attached_tip_controller.drop_tip(
        TipPipetteMount.LEFT,
        location=location,
        home_after=False,
        status=status,
        intermediate=intermediate,
    )
    assert dropped is TipPresence.ABSENT
    assert intermediate.messages[-1].message == "Tip drop on LEFT verified."


async def test_tip_state_without_pipette_is_defined_error(api: OT3API):
    feature = TipController(FlexMotionController.from_api(api, lock=asyncio.Lock()))
    with pytest.raises(PipetteNotAttachedError):
        await feature.get_tip_presence(TipPipetteMount.LEFT)


async def test_drop_without_tip_is_defined_error(attached_tip_controller: TipController):
    point = await attached_tip_controller._controller.gantry_position(OT3Mount.LEFT)
    status, intermediate = _obs()
    with pytest.raises(TipNotAttachedError):
        await attached_tip_controller.drop_tip(
            TipPipetteMount.LEFT,
            location=TipLocation(x=point.x, y=point.y, z=point.z),
            home_after=False,
            status=status,
            intermediate=intermediate,
        )


async def test_global_stop_requires_full_rehome_before_next_tip_operation(attached_tip_controller: TipController):
    point = await attached_tip_controller._controller.gantry_position(OT3Mount.LEFT)
    location = TipLocation(x=point.x, y=point.y, z=point.z)
    await attached_tip_controller._controller.stop()

    status, intermediate = _obs()
    with pytest.raises(NotHomedError, match="Fully re-home"):
        await attached_tip_controller.pick_up_tip(
            TipPipetteMount.LEFT,
            location=location,
            tip_length=95.6,
            prep_after=False,
            status=status,
            intermediate=intermediate,
        )


# ── Gripper ─────────────────────────────────────────────────────────────────


async def test_grip_without_gripper_raises(gripper: GripperFeature):
    status, intermediate = _obs()
    with pytest.raises(GripperNotAttachedError):
        await gripper.grip(force=15.0, status=status, intermediate=intermediate)
    assert intermediate.messages


# ── Calibration ─────────────────────────────────────────────────────────────


async def test_calibrate_pipette_without_probe_raises_defined_error(calibration: CalibrationFeature):
    status, intermediate = _obs()
    with pytest.raises((CalibrationProbeNotAttachedError, CalibrationFailedError)):
        await calibration.calibrate_pipette(
            PipetteMount.LEFT,
            slot=5,
            status=status,
            intermediate=intermediate,
        )
    assert intermediate.messages[0].message == "Starting LEFT pipette calibration."


async def test_calibrate_gripper_jaw_without_probe_raises_defined_error(calibration: CalibrationFeature):
    status, intermediate = _obs()
    with pytest.raises((CalibrationProbeNotAttachedError, CalibrationFailedError)):
        await calibration.calibrate_gripper_jaw(
            GripperJaw.FRONT,
            slot=5,
            status=status,
            intermediate=intermediate,
        )
    assert intermediate.messages[0].message == "Starting FRONT gripper jaw calibration."


async def test_calibrate_deck_without_probe_raises_defined_error(calibration: CalibrationFeature):
    status, intermediate = _obs()
    with pytest.raises((CalibrationProbeNotAttachedError, CalibrationFailedError)):
        await calibration.calibrate_deck(
            PipetteMount.LEFT,
            pipette_id="simulated-pipette",
            status=status,
            intermediate=intermediate,
        )
    assert intermediate.messages[0].message == "Starting deck calibration with LEFT."


# ── Wiring ──────────────────────────────────────────────────────────────────


async def test_features_construct_without_error(api: OT3API):
    lock = asyncio.Lock()
    motion = FlexMotionController.from_api(api, lock=lock)
    gripper = FlexGripperController.from_api(api, lock=lock)
    calibration = FlexCalibrationController.from_api(api, lock=lock)
    # Construction runs each feature's SiLA metadata setup (real CDK) or the stub.
    assert MotionControlFeature(motion) is not None
    assert PipetteFeature(motion) is not None
    assert TipController(motion) is not None
    assert GripperFeature(gripper) is not None
    assert CalibrationFeature(calibration) is not None
