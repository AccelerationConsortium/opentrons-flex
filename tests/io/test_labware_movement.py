"""Safety and positive-path coverage for allowlisted Flex labware movement."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio
from opentrons.hardware_control.modules.types import ModuleType
from opentrons.hardware_control.ot3api import OT3API
from opentrons.hardware_control.types import OT3Mount
from opentrons.types import Point

from unitelabs.opentrons_flex.io import (
    DestinationOccupiedError,
    DirectGripperControlDisabledError,
    FlexGripperController,
    FlexLabwareMovementController,
    FlexMotionController,
    GripActionError,
    LabwareGripGeometry,
    LabwareMovementNotAllowedError,
    LabwareMovementPlan,
    LabwareMovementState,
    NotHomedError,
    load_labware_movement_config,
)


def _geometry() -> LabwareGripGeometry:
    return LabwareGripGeometry(
        force_newtons=15.0,
        expected_width=74.0,
        uncertainty_wider=2.0,
        uncertainty_narrower=2.0,
    )


def _plan(home: Point, identifier: str = "plate-out", *, is_lid: bool = False) -> LabwareMovementPlan:
    return LabwareMovementPlan(
        identifier=identifier,
        labware_identifier="plate-1",
        is_lid=is_lid,
        source_identifier="D1",
        source_kind="DECK_SLOT",
        source_grip_point=Point(home.x - 80, home.y - 80, 80.0),
        destination_identifier="D2",
        destination_kind="DECK_SLOT",
        destination_grip_point=Point(home.x - 180, home.y - 80, 80.0),
        geometry=_geometry(),
        post_drop_slide_offset=Point(0, 0, 0),
    )


@pytest_asyncio.fixture
async def labware_controller(
    tmp_path: Path,
) -> tuple[OT3API, FlexLabwareMovementController, LabwareMovementPlan]:
    api = await OT3API.build_hardware_simulator(
        attached_instruments={
            OT3Mount.GRIPPER: {"model": "gripperV1.3", "id": "sim-gripper"},
        }
    )
    await api.home()
    lock = asyncio.Lock()
    motion = FlexMotionController.from_api(api, lock=lock)
    gripper = FlexGripperController.from_api(api, lock=lock)
    home = await api.gantry_position(OT3Mount.GRIPPER, refresh=True)
    plan = _plan(home)
    state = LabwareMovementState(tmp_path / "state.json", {"D1": "plate-1"})
    yield api, FlexLabwareMovementController(motion, gripper, [plan], state), plan
    state.close()
    await api.clean_up()


async def test_attached_gripper_moves_allowlisted_plan_through_official_waypoints(
    labware_controller: tuple[OT3API, FlexLabwareMovementController, LabwareMovementPlan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, controller, plan = labware_controller
    verified: list[dict[str, object]] = []
    real_jaw_can_home = api.gripper_jaw_can_home

    def jaw_can_home_under_lock() -> bool:
        assert controller._motion._lock._lock.locked()
        return real_jaw_can_home()

    monkeypatch.setattr(api, "gripper_jaw_can_home", jaw_can_home_under_lock)
    monkeypatch.setattr(api, "raise_error_if_gripper_pickup_failed", lambda **kwargs: verified.append(kwargs))

    resolved, result = await controller.move_labware(plan.identifier, expect_lid=False)

    assert resolved == plan
    assert verified and verified[0]["expected_grip_width"] == 74.0
    assert verified[0]["disable_geometry_grip_check"] is False
    assert result.final_position.x == pytest.approx(plan.destination_grip_point.x)
    assert result.final_position.y == pytest.approx(plan.destination_grip_point.y)
    assert result.jaw_width >= 0
    with pytest.raises(LabwareMovementNotAllowedError, match="does not show labware at source"):
        await controller.move_labware(plan.identifier, expect_lid=False)


async def test_occupied_destination_is_rejected_from_server_owned_state(
    labware_controller: tuple[OT3API, FlexLabwareMovementController, LabwareMovementPlan],
    tmp_path: Path,
) -> None:
    api, _, plan = labware_controller
    lock = asyncio.Lock()
    motion = FlexMotionController.from_api(api, lock=lock)
    gripper = FlexGripperController.from_api(api, lock=lock)
    state = LabwareMovementState(tmp_path / "occupied-state.json", {"D1": "plate-1", "D2": "plate-2"})
    controller = FlexLabwareMovementController(motion, gripper, [plan], state)
    with pytest.raises(DestinationOccupiedError, match="occupied"):
        await controller.move_labware(plan.identifier, expect_lid=False)


async def test_remote_call_cannot_supply_or_bypass_an_unconfigured_plan(
    labware_controller: tuple[OT3API, FlexLabwareMovementController, LabwareMovementPlan],
) -> None:
    _, controller, plan = labware_controller
    with pytest.raises(LabwareMovementNotAllowedError, match="not provisioned"):
        await controller.move_labware("remote-plan", expect_lid=False)
    with pytest.raises(LabwareMovementNotAllowedError, match="MoveLid"):
        lid_controller = FlexLabwareMovementController(
            controller._motion,
            controller._gripper,
            [replace(plan, identifier="lid-plan", is_lid=True)],
            controller._state,
        )
        await lid_controller.move_labware("lid-plan", expect_lid=False)


async def test_allowlisted_mode_blocks_raw_gripper_motion_and_jaw_actuation(
    labware_controller: tuple[OT3API, FlexLabwareMovementController, LabwareMovementPlan],
) -> None:
    _, controller, plan = labware_controller
    with pytest.raises(DirectGripperControlDisabledError, match="MoveTo"):
        await controller._motion.move_to(OT3Mount.GRIPPER, plan.source_grip_point)
    with pytest.raises(DirectGripperControlDisabledError, match="Grip"):
        await controller._gripper.grip(15.0)


@pytest.mark.parametrize(
    "module_type, state_attribute, state",
    [
        (ModuleType.HEATER_SHAKER, "labware_latch_status", "idle_closed"),
        (ModuleType.THERMOCYCLER, "lid_status", "closed"),
        (ModuleType.ABSORBANCE_READER, "lid_status", "closed"),
    ],
)
def test_closed_module_blocks_labware_movement(
    module_type: ModuleType,
    state_attribute: str,
    state: str,
) -> None:
    module = type("Module", (), {"MODULE_TYPE": module_type, state_attribute: state})()
    with pytest.raises(LabwareMovementNotAllowedError, match="not fully open"):
        FlexLabwareMovementController._assert_module_accessible(module, direction="source")


async def test_cancelled_labware_move_halts_and_requires_full_and_jaw_home(
    labware_controller: tuple[OT3API, FlexLabwareMovementController, LabwareMovementPlan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, controller, plan = labware_controller
    move_started = asyncio.Event()

    async def blocked_move(*args: object, **kwargs: object) -> None:
        move_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(api, "move_to", blocked_move)
    monkeypatch.setattr(api, "raise_error_if_gripper_pickup_failed", lambda **kwargs: None)
    task = asyncio.create_task(controller.move_labware(plan.identifier, expect_lid=False))
    await move_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert controller._motion._recovery_state.gripper_home_required
    assert controller._state is not None and controller._state.valid is False
    with pytest.raises(NotHomedError, match="Fully re-home"):
        await controller.move_labware(plan.identifier, expect_lid=False)


async def test_gripper_failure_is_defined_and_invalidates_server_deck_state(
    labware_controller: tuple[OT3API, FlexLabwareMovementController, LabwareMovementPlan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, controller, plan = labware_controller

    async def failed_grip(*args: object, **kwargs: object) -> None:
        raise RuntimeError("motor fault 42")

    monkeypatch.setattr(api, "grip", failed_grip)
    with pytest.raises(GripActionError, match="motor fault 42"):
        await controller.move_labware(plan.identifier, expect_lid=False)
    assert controller._state is not None and controller._state.valid is False


def test_local_json_loader_validates_and_loads_allowlisted_plan(tmp_path: Path) -> None:
    path = tmp_path / "labware.json"
    path.write_text(
        json.dumps(
            {
                "state_file": "state.json",
                "initial_occupancy": {"D1": "plate-1"},
                "plans": [
                    {
                        "identifier": "plate-out",
                        "labware_identifier": "plate-1",
                        "is_lid": False,
                        "source": {
                            "identifier": "D1",
                            "kind": "DECK_SLOT",
                            "grip_point": {"x": 100, "y": 100, "z": 80},
                        },
                        "destination": {
                            "identifier": "D2",
                            "kind": "DECK_SLOT",
                            "grip_point": {"x": 200, "y": 100, "z": 80},
                        },
                        "grip": {
                            "force_newtons": 15,
                            "expected_width": 74,
                            "uncertainty_wider": 2,
                            "uncertainty_narrower": 2,
                        },
                        "post_drop_slide_offset": {"x": 0, "y": 0, "z": 0},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_labware_movement_config(path)
    assert loaded.initial_occupancy == {"D1": "plate-1"}
    assert loaded.state_file == tmp_path / "state.json"
    assert loaded.plans[0].identifier == "plate-out"
    assert loaded.plans[0].geometry.expected_width == 74.0


def test_durable_state_survives_clean_restart_with_labware_identity(tmp_path: Path) -> None:
    path = tmp_path / "durable-state.json"
    state = LabwareMovementState(path, {"D1": "plate-1"})
    state.validate_move("D1", "D2", "plate-1")
    state.begin_move()
    state.complete_move("D1", "D2", "plate-1")
    state.close()

    restored = LabwareMovementState(path, {"D1": "stale-config-value"})
    assert restored.valid is True
    assert restored.occupancy == {"D2": "plate-1"}
    restored.close()


def test_unclean_restart_persistently_invalidates_deck_state(tmp_path: Path) -> None:
    path = tmp_path / "unclean-state.json"
    state = LabwareMovementState(path, {"D1": "plate-1"})
    state.begin_move()

    restored = LabwareMovementState(path, {"D1": "plate-1"})
    assert restored.valid is False
    with pytest.raises(LabwareMovementNotAllowedError, match="Durable deck state is invalid"):
        restored.validate_move("D1", "D2", "plate-1")
    restored.close()


def test_failed_completed_move_commit_keeps_in_memory_state_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = LabwareMovementState(tmp_path / "commit-failure.json", {"D1": "plate-1"})

    def fail_replace(source: Path, destination: Path) -> Path:
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(RuntimeError, match="Cannot persist"):
        state.complete_move("D1", "D2", "plate-1")

    assert state.valid is False
    assert state.occupancy == {"D1": "plate-1"}
