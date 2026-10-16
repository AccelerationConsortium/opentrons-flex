from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from opentrons.hardware_control.ot3api import OT3API
from opentrons.hardware_control.types import OT3Mount

from unitelabs.opentrons_flex.asms_unit_operations import (
    GrpcUnitClient,
    UnitOperationError,
    load_manifest,
    required_confirmations,
    run_phase,
)
from unitelabs.opentrons_flex.features import (
    LabwareDeckState,
    LabwareMovementProfile,
    LabwarePlanSummary,
    OccupiedLocation,
    PipetteInfo,
    PipetteMount,
    TipPresence,
)
from unitelabs.opentrons_flex.features.gripper import GripperStatus
from unitelabs.opentrons_flex.features.motion_control import MachineStatus
from unitelabs.opentrons_flex.io import load_labware_movement_config
from unitelabs.opentrons_flex.io import (
    FlexGripperController,
    FlexLabwareMovementController,
    FlexMotionController,
    LabwareMovementState,
)

ROOT = Path(__file__).resolve().parents[1]


def _manifest():
    return load_manifest(ROOT / "config/asms_unit_operations.json")


class FakeClient:
    def __init__(self) -> None:
        manifest = _manifest()
        self.simulating = False
        self.machine = MachineStatus("DISENGAGED", False, False, "ready")
        self.pipettes = [
            PipetteInfo(
                mount=PipetteMount.RIGHT,
                attached=True,
                model="p1000_multi_flex",
                name="flex_8channel_1000",
                pipette_id="P1000",
                channels=8,
                min_volume=5.0,
                max_volume=1000.0,
                has_tip=False,
            )
        ]
        self.gripper = GripperStatus(True, "gripperV1", "GRIPPER", "READY")
        self.plans = [
            LabwarePlanSummary(identifier, route[0], route[1], route[2], False)
            for identifier, route in manifest.expected_plan_routes.items()
        ]
        self.profile = LabwareMovementProfile(manifest.movement_profile_sha256)
        self.deck = LabwareDeckState(
            True,
            [
                OccupiedLocation("A1", "asms-plate"),
                OccupiedLocation("B1", "elution-plate"),
            ],
        )
        self.calls: list[str] = []

    async def is_simulating(self):
        return self.simulating

    async def machine_status(self):
        return self.machine

    async def attached_pipettes(self):
        return self.pipettes

    async def gripper_status(self):
        return self.gripper

    async def available_plans(self):
        return self.plans

    async def deck_state(self):
        return self.deck

    async def movement_profile(self):
        return self.profile

    async def temperature_device_info(self):
        return SimpleNamespace(model="temperatureModuleV2", serial_number="TM-1")

    async def home(self):
        self.calls.append("home")

    async def move_labware(self, plan_identifier):
        self.calls.append(plan_identifier)
        return SimpleNamespace(plan_identifier=plan_identifier)

    async def configure_full_nozzle(self, mount, tiprack_diameter):
        self.calls.append(f"configure:{mount.value}:{tiprack_diameter}")

    async def tip_presence(self, mount):
        self.calls.append(f"presence:{mount.value}")
        return TipPresence.ABSENT

    async def pick_up_tip(self, mount, location, tip_length):
        self.calls.append(f"pickup:{mount.value}:{tip_length}")
        return TipPresence.PRESENT

    async def drop_tip(self, mount, location):
        self.calls.append(f"drop:{mount.value}")
        return TipPresence.ABSENT

    async def transfer(self, mount, config):
        self.calls.append(f"transfer:{mount.value}:{config.volume}")

    async def set_temperature(self, target):
        self.calls.append(f"temperature:{target}")

    async def deactivate_temperature(self):
        self.calls.append("temperature:off")


def _confirmations(phase: str) -> dict[str, str]:
    return required_confirmations(phase)


class _StallingChannel:
    def unary_unary(self, _path):
        async def call(_request, *, timeout):
            await asyncio.Event().wait()

        return call


class _MinimalCodec:
    async def encode(self, _message, _parameters):
        return b"request"


def test_exact_manifest_loads_and_preserves_magnetic_block_offsets() -> None:
    manifest = _manifest()
    movement = load_labware_movement_config(ROOT / "config/asms_unit_labware_movement.json")
    by_identifier = {plan.identifier: plan for plan in movement.plans}

    assert manifest.expected_staging_slots == ("A4", "B4", "C4", "D4")
    assert manifest.movement_profile_sha256 == movement.profile_sha256
    assert manifest.pipetting.mount == "RIGHT"
    assert manifest.pipetting.volume == 20.0
    assert movement.initial_occupancy == {"A1": "asms-plate", "B1": "elution-plate"}
    assert by_identifier["asms-v1-a1-to-b2"].destination_grip_point.z == -1.675
    assert by_identifier["asms-v1-b2-to-a1"].source_grip_point.z == -2.675


def test_manifest_rejects_incomplete_staging_topology(tmp_path: Path) -> None:
    source = (ROOT / "config/asms_unit_operations.json").read_text()
    path = tmp_path / "manifest.json"
    path.write_text(source.replace('"C4",\n    "D4"', '"C4"'))

    with pytest.raises(UnitOperationError, match="exactly A4, B4, C4, D4"):
        load_manifest(path)


def test_manifest_rejects_changed_pipetting_geometry(tmp_path: Path) -> None:
    source = (ROOT / "config/asms_unit_operations.json").read_text()
    path = tmp_path / "manifest.json"
    path.write_text(source.replace('"volume": 20.0', '"volume": 200.0'))

    with pytest.raises(UnitOperationError, match="pinned ASMS unit-operation contract"):
        load_manifest(path)


@pytest.mark.asyncio
async def test_exact_gripper_geometry_is_accepted_by_pinned_ot3_bounds(tmp_path: Path) -> None:
    movement = load_labware_movement_config(ROOT / "config/asms_unit_labware_movement.json")
    api = await OT3API.build_hardware_simulator(
        attached_instruments={
            OT3Mount.GRIPPER: {
                "model": "gripperV1",
                "id": "GRIPPER-SIM-1",
            }
        }
    )
    state = LabwareMovementState(tmp_path / "deck-state.json", movement.initial_occupancy)
    try:
        lock = asyncio.Lock()
        motion = FlexMotionController.from_api(api, lock=lock)
        gripper = FlexGripperController.from_api(api, lock=lock)
        controller = FlexLabwareMovementController(
            motion,
            gripper,
            plans=movement.plans,
            state=state,
        )
        assert len(controller.available_plans) == 4
    finally:
        state.close()
        await api.clean_up()


@pytest.mark.asyncio
async def test_health_reports_invalid_ledger_without_allowing_preflight() -> None:
    client = FakeClient()
    client.deck = replace(client.deck, valid=False)

    health = await run_phase(_manifest(), client, "health")

    assert health["connector_health"] == "PASS"
    assert health["deck_ledger_valid"] is False
    with pytest.raises(UnitOperationError, match="deck ledger is invalid"):
        await run_phase(_manifest(), client, "preflight")


@pytest.mark.asyncio
async def test_observable_rpc_deadline_triggers_emergency_halt() -> None:
    client = GrpcUnitClient(_StallingChannel(), _MinimalCodec())  # type: ignore[arg-type]
    client._halt_after_uncertain_execution = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(UnitOperationError, match="EmergencyStop was issued"):
        await client._observable("service", "package", "Move", timeout=0.01)

    client._halt_after_uncertain_execution.assert_awaited_once_with("Move")


@pytest.mark.asyncio
async def test_preflight_requires_exact_initial_occupancy() -> None:
    client = FakeClient()
    client.deck = LabwareDeckState(True, [OccupiedLocation("B2", "asms-plate")])

    with pytest.raises(UnitOperationError, match="prepared initial state"):
        await run_phase(_manifest(), client, "preflight")


@pytest.mark.asyncio
async def test_preflight_rejects_attached_tip() -> None:
    client = FakeClient()
    client.pipettes[0] = replace(client.pipettes[0], has_tip=True)

    health = await run_phase(_manifest(), client, "health")

    assert health["pipette_has_tip"] is True
    with pytest.raises(UnitOperationError, match="attached tip"):
        await run_phase(_manifest(), client, "preflight")


@pytest.mark.asyncio
async def test_preflight_rejects_same_routes_with_different_server_geometry() -> None:
    client = FakeClient()
    client.profile = LabwareMovementProfile("0" * 64)

    with pytest.raises(UnitOperationError, match="profile digest"):
        await run_phase(_manifest(), client, "preflight")


@pytest.mark.asyncio
async def test_actuation_requires_execute_and_exact_tokens() -> None:
    client = FakeClient()

    with pytest.raises(UnitOperationError, match="pass --execute"):
        await run_phase(_manifest(), client, "home")
    with pytest.raises(UnitOperationError, match="confirm_deck_ready"):
        await run_phase(_manifest(), client, "home", execute=True)


@pytest.mark.asyncio
async def test_gripper_phase_runs_only_one_round_trip() -> None:
    client = FakeClient()

    result = await run_phase(
        _manifest(),
        client,
        "gripper-a1",
        execute=True,
        confirmations=_confirmations("gripper-a1"),
    )

    assert result["completed"] is True
    assert client.calls == ["asms-v1-a1-to-b2", "asms-v1-b2-to-a1"]


@pytest.mark.asyncio
async def test_liquid_phase_owns_one_sensor_verified_tip_lifecycle() -> None:
    client = FakeClient()

    await run_phase(
        _manifest(),
        client,
        "liquid",
        execute=True,
        confirmations=_confirmations("liquid"),
    )

    assert client.calls == [
        "configure:RIGHT:5.47",
        "presence:RIGHT",
        "pickup:RIGHT:95.6",
        "transfer:RIGHT:20.0",
        "drop:RIGHT",
    ]


@pytest.mark.asyncio
async def test_liquid_phase_attempts_tip_return_when_transfer_fails() -> None:
    client = FakeClient()

    async def fail_transfer(_mount, _config):
        client.calls.append("transfer:failed")
        raise RuntimeError("transfer failed")

    client.transfer = fail_transfer
    with pytest.raises(RuntimeError, match="transfer failed"):
        await run_phase(
            _manifest(),
            client,
            "liquid",
            execute=True,
            confirmations=_confirmations("liquid"),
        )

    assert client.calls[-2:] == ["transfer:failed", "drop:RIGHT"]


@pytest.mark.asyncio
async def test_temperature_is_deactivated_when_set_fails() -> None:
    client = FakeClient()

    async def fail(_target):
        raise RuntimeError("module error")

    client.set_temperature = fail
    with pytest.raises(RuntimeError, match="module error"):
        await run_phase(
            _manifest(),
            client,
            "temperature",
            execute=True,
            confirmations=_confirmations("temperature"),
        )

    assert client.calls == ["temperature:off"]
