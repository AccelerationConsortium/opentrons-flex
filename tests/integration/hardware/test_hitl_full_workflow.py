"""Guarded, manifest-driven Flex system acceptance against real hardware.

This is deliberately one physical campaign rather than a collection of isolated
endpoint calls.  It validates the same plate and liquid state transitions that
an operator will use in a real assay while keeping every coordinate and gripper
route in local, pre-provisioned configuration.
"""

from __future__ import annotations

import asyncio
import contextlib
import grpc.aio
import httpx
import pytest

from unitelabs.opentrons_flex.acceptance import (
    AcceptanceManifest,
    Coordinate,
    PlanContract,
    WellGeometryConfig,
    validate_plan_contract,
)
from unitelabs.opentrons_flex.features import (
    LabwareDeckState,
    LabwareMovementResult,
    LabwarePlanSummary,
    LiquidPosition,
    Mount,
    NozzleConfiguration,
    PipetteInfo,
    PipetteMount,
    PlateMeasurement,
    PlatePresence,
    ReaderLidStatus,
    TipLocation,
    TipPresence,
    TransferProfile,
    VerifiedLiquidClass,
    VerifiedTransferResult,
    WellGeometry,
)
from unitelabs.opentrons_flex.features.motion_control import MachineStatus
from unitelabs.opentrons_flex.features.thermocycler import ThermocyclerProfileStep

from ..absorbance_reader_client import AbsorbanceReaderClient
from ..flex_stacker_client import FlexStackerClient
from ..heater_shaker_client import HeaterShakerClient
from ..observable import call_observable
from ..temperature_module_client import TemperatureModuleClient
from ..thermocycler_client import ThermocyclerClient

_MOTION_PACKAGE = "sila2.ca.accelerationconsortium.robots.motioncontroller.v2"
_MOTION_SERVICE = f"{_MOTION_PACKAGE}.MotionController"
_PIPETTE_PACKAGE = "sila2.ca.accelerationconsortium.robots.pipettecontroller.v1"
_PIPETTE_SERVICE = f"{_PIPETTE_PACKAGE}.PipetteController"
_TIP_PACKAGE = "sila2.ca.accelerationconsortium.robots.tipcontroller.v1"
_TIP_SERVICE = f"{_TIP_PACKAGE}.TipController"
_LIQUID_PACKAGE = "sila2.ca.accelerationconsortium.robots.liquidhandlingcontroller.v1"
_LIQUID_SERVICE = f"{_LIQUID_PACKAGE}.LiquidHandlingController"
_LABWARE_PACKAGE = "sila2.ca.accelerationconsortium.robots.labwaremovementcontroller.v1"
_LABWARE_SERVICE = f"{_LABWARE_PACKAGE}.LabwareMovementController"

pytestmark = [pytest.mark.hardware_only, pytest.mark.acceptance_workflow_actuation]


class _FeatureClient:
    """Small codec-backed client for the connector's robot features."""

    def __init__(self, channel: grpc.aio.Channel, protobuf: object, package: str, service: str) -> None:
        self._channel = channel
        self._protobuf = protobuf
        self._package = package
        self._service = service

    async def command(self, method: str, parameters: dict | None = None) -> object:
        request = await self._protobuf.encode(f"{self._package}.{method}_Parameters", parameters or {})
        response = await self._channel.unary_unary(f"/{self._service}/{method}")(request)
        decoded = await self._protobuf.decode(f"{self._package}.{method}_Responses", response)
        return next(iter(decoded.values()), None)

    async def observable(self, method: str, parameters: dict | None = None, timeout: float = 300.0) -> object:
        decoded = await call_observable(
            self._channel,
            self._protobuf,
            self._service,
            self._package,
            method,
            parameters,
            timeout_s=timeout,
        )
        return next(iter(decoded.values()), None)

    async def property(self, method: str) -> object:
        response = await self._channel.unary_unary(f"/{self._service}/{method}")(b"")
        decoded = await self._protobuf.decode(f"{self._package}.{method}_Responses", response)
        return next(iter(decoded.values()))


def _position(value: Coordinate) -> LiquidPosition:
    return LiquidPosition(value.x, value.y, value.z)


def _well(value: WellGeometryConfig) -> WellGeometry:
    return WellGeometry(value.x, value.y, value.bottom_z, value.top_z, value.length, value.width)


def _mounts(name: str) -> tuple[Mount, PipetteMount]:
    return Mount[name], PipetteMount[name]


async def _assert_machine_ok(motion: _FeatureClient, after: str) -> MachineStatus:
    status = await motion.property("Get_MachineStatus")
    assert isinstance(status, MachineStatus)
    assert status.is_error_state is False, f"robot entered an error state {after}: {status.message}"
    assert status.estop == "DISENGAGED", f"E-stop is not disengaged {after}: {status.estop}"
    assert status.door_open is False, f"Flex door is open {after}; close it before actuation"
    return status


async def _assert_deck_valid(labware: _FeatureClient, after: str) -> LabwareDeckState:
    state = await labware.property("Get_DeckState")
    assert isinstance(state, LabwareDeckState)
    assert state.valid is True, f"connector deck ledger became invalid {after}; reconcile before continuing"
    return state


async def _move(
    labware: _FeatureClient,
    motion: _FeatureClient,
    plan_identifier: str,
    *,
    lid: bool = False,
) -> LabwareMovementResult:
    method = "MoveLid" if lid else "MoveLabware"
    result = await labware.observable(method, {"plan_identifier": plan_identifier})
    assert isinstance(result, LabwareMovementResult)
    assert result.plan_identifier == plan_identifier
    await _assert_machine_ok(motion, f"after {plan_identifier}")
    await _assert_deck_valid(labware, f"after {plan_identifier}")
    return result


async def _preflight(
    manifest: AcceptanceManifest,
    run_context,
    http_client: httpx.Client,
    motion: _FeatureClient,
    pipette: _FeatureClient,
    labware: _FeatureClient,
    heater_shaker: HeaterShakerClient,
    thermocycler: ThermocyclerClient,
    temperature_module: TemperatureModuleClient,
    reader: AbsorbanceReaderClient,
    stacker: FlexStackerClient,
    record_property,
) -> tuple[Mount, PipetteMount]:
    assert run_context.device_id == manifest.expected_robot_host, (
        f"manifest targets {manifest.expected_robot_host!r}, but pytest targets {run_context.device_id!r}"
    )
    health = await asyncio.to_thread(http_client.get, "/health")
    assert health.status_code == 200, f"parallel robot-server is not healthy: {health.status_code} {health.text[:300]}"
    assert await motion.property("Get_IsSimulating") is False, "hardware acceptance cannot run against a simulator"
    await _assert_machine_ok(motion, "during preflight")

    plans = await labware.property("Get_AvailablePlans")
    assert isinstance(plans, list) and all(isinstance(item, LabwarePlanSummary) for item in plans)
    deck_state = await _assert_deck_valid(labware, "during preflight")
    validate_plan_contract(
        manifest,
        tuple(
            PlanContract(
                plan.plan_identifier,
                plan.labware_identifier,
                plan.source_location,
                plan.destination_location,
                plan.is_lid,
            )
            for plan in plans
        ),
        deck_valid=deck_state.valid,
        occupancy={item.location_identifier: item.labware_identifier for item in deck_state.occupied_locations},
    )

    module_clients = (
        ("heater_shaker", heater_shaker, manifest.modules.heater_shaker_serial),
        ("thermocycler", thermocycler, manifest.modules.thermocycler_serial),
        ("temperature_module", temperature_module, manifest.modules.temperature_module_serial),
        ("plate_reader", reader, manifest.modules.plate_reader_serial),
        ("stacker", stacker, manifest.modules.stacker_serial),
    )
    for name, client, expected_serial in module_clients:
        info = await client.get_device_info()
        assert info.serial_number == expected_serial, (
            f"{name} serial mismatch: expected {expected_serial!r}, found {info.serial_number!r}"
        )
        record_property(f"{name}_serial", info.serial_number)

    reader_status = await reader.get_status()
    unsupported = set(manifest.modules.plate_reader_wavelengths) - set(reader_status.supported_wavelengths)
    assert not unsupported, f"plate reader does not support manifest wavelengths: {sorted(unsupported)}"

    robot_mount, pipette_mount = _mounts(manifest.pipetting.mount)
    attached = await pipette.observable("GetAttachedPipettes")
    assert isinstance(attached, list) and all(isinstance(item, PipetteInfo) for item in attached)
    selected = next(item for item in attached if item.mount is pipette_mount)
    assert selected.attached, f"no pipette is attached on {pipette_mount.value}"
    assert manifest.pipetting.transfer_volume <= selected.max_volume
    assert manifest.pipetting.mix_volume <= selected.max_volume
    assert manifest.pipetting.transfer_volume >= selected.min_volume
    assert manifest.pipetting.mix_volume >= selected.min_volume
    record_property("pipette_id", selected.pipette_id)
    record_property("pipette_model", selected.model)
    return robot_mount, pipette_mount


async def test_manifest_driven_full_flex_acceptance_workflow(
    sila_channel,
    acceptance_manifest: AcceptanceManifest,
    run_context,
    http_client: httpx.Client,
    record_property,
) -> None:
    """Exercise robot motion, pipetting, gripper plans, and every active accessory."""
    channel, protobuf = sila_channel
    motion = _FeatureClient(channel, protobuf, _MOTION_PACKAGE, _MOTION_SERVICE)
    pipette = _FeatureClient(channel, protobuf, _PIPETTE_PACKAGE, _PIPETTE_SERVICE)
    tip = _FeatureClient(channel, protobuf, _TIP_PACKAGE, _TIP_SERVICE)
    liquid = _FeatureClient(channel, protobuf, _LIQUID_PACKAGE, _LIQUID_SERVICE)
    labware = _FeatureClient(channel, protobuf, _LABWARE_PACKAGE, _LABWARE_SERVICE)
    heater_shaker = HeaterShakerClient(channel, protobuf)
    thermocycler = ThermocyclerClient(channel, protobuf)
    temperature_module = TemperatureModuleClient(channel, protobuf)
    reader = AbsorbanceReaderClient(channel, protobuf)
    stacker = FlexStackerClient(channel, protobuf)
    manifest = acceptance_manifest
    p = manifest.pipetting
    m = manifest.modules
    plans = manifest.plans

    robot_mount, pipette_mount = await _preflight(
        manifest,
        run_context,
        http_client,
        motion,
        pipette,
        labware,
        heater_shaker,
        thermocycler,
        temperature_module,
        reader,
        stacker,
        record_property,
    )
    record_property("acceptance_schema_version", manifest.schema_version)

    try:
        record_property("phase_01", "home_and_configuration")
        await motion.observable("SetLights", {"button": True, "rails": True})
        await motion.observable("Home", timeout=180.0)
        await _assert_machine_ok(motion, "after homing")
        nozzle = await pipette.command(
            "ConfigureFullNozzleLayout",
            {"mount": pipette_mount, "tiprack_diameter": p.tiprack_diameter},
        )
        assert isinstance(nozzle, NozzleConfiguration) and nozzle.active_nozzles > 0

        record_property("phase_02", "stacker_round_trip")
        await stacker.home_all(ignore_latch=False)
        await stacker.retrieve_labware(
            m.stacker_labware_height,
            enforce_hopper_labware_sensing=True,
            enforce_shuttle_labware_sensing=True,
        )
        await stacker.store_labware(m.stacker_labware_height, enforce_shuttle_labware_sensing=True)
        await stacker.deactivate()

        record_property("phase_03", "thermocycler_profile")
        await thermocycler.open_lid()
        await _move(labware, motion, plans["process_deck_to_thermocycler"])
        await thermocycler.close_lid()
        await thermocycler.set_lid_temperature(m.thermocycler_lid_temperature)
        await thermocycler.wait_for_lid_temperature()
        await thermocycler.execute_profile(
            [
                ThermocyclerProfileStep(step.temperature, step.hold_time, step.ramp_rate)
                for step in m.thermocycler_profile
            ],
            repetitions=1,
            volume=m.thermocycler_volume,
        )
        await thermocycler.deactivate_all()
        await thermocycler.open_lid()
        await _move(labware, motion, plans["thermocycler_to_process_deck"])

        record_property("phase_04", "tip_and_liquid_handling")
        presence = await tip.command("GetTipPresence", {"mount": pipette_mount})
        assert presence is TipPresence.ABSENT, "prepared tip position requires an empty pipette"
        picked = await tip.observable(
            "PickUpTip",
            {
                "mount": pipette_mount,
                "location": TipLocation(p.tip_pickup.x, p.tip_pickup.y, p.tip_pickup.z),
                "tip_length": p.tip_length,
                "prep_after": False,
            },
        )
        assert picked is TipPresence.PRESENT
        await motion.observable(
            "MoveTo",
            {
                "mount": robot_mount,
                "x": p.source_retract.x,
                "y": p.source_retract.y,
                "z": p.source_retract.z,
                "speed": 0.0,
            },
        )
        await liquid.observable(
            "ProbeLiquidLevel",
            {"mount": pipette_mount, "maximum_distance": p.probe_maximum_distance},
        )
        await motion.observable(
            "MoveTo",
            {"mount": robot_mount, "x": p.source.x, "y": p.source.y, "z": p.source.z, "speed": 0.0},
        )
        await liquid.observable(
            "Mix",
            {
                "mount": pipette_mount,
                "cycles": p.mix_cycles,
                "volume": p.mix_volume,
                "aspirate_rate": 1.0,
                "dispense_rate": 1.0,
            },
        )
        await liquid.observable(
            "Transfer",
            {
                "mount": pipette_mount,
                "source": _position(p.source),
                "source_retract": _position(p.source_retract),
                "destination": _position(p.destination),
                "destination_retract": _position(p.destination_retract),
                "volume": p.transfer_volume,
                "profile": TransferProfile(1.0, 1.0, 0.0, 0.0, 0, 0.0, 0, 0.0, True),
            },
        )
        verified = await liquid.observable(
            "TransferWithVerifiedLiquidClass",
            {
                "mount": pipette_mount,
                "source_well": _well(p.source_well),
                "destination_well": _well(p.destination_well),
                "volume": p.transfer_volume,
                "liquid_class": VerifiedLiquidClass.WATER,
                "tiprack_uri": p.tiprack_uri,
            },
        )
        assert isinstance(verified, VerifiedTransferResult)
        await liquid.observable(
            "TouchTip",
            {
                "mount": pipette_mount,
                "well": _well(p.destination_well),
                "z_offset": -1.0,
                "distance_from_edge": 1.0,
                "speed": 20.0,
            },
        )
        dropped = await tip.observable(
            "DropTip",
            {
                "mount": pipette_mount,
                "location": TipLocation(p.tip_drop.x, p.tip_drop.y, p.tip_drop.z),
                "home_after": False,
            },
        )
        assert dropped is TipPresence.ABSENT
        await _assert_machine_ok(motion, "after liquid handling")

        record_property("phase_05", "heater_shaker_and_magnetic_block")
        await heater_shaker.open_latch()
        await _move(labware, motion, plans["process_deck_to_heater_shaker"])
        await heater_shaker.close_latch()
        await heater_shaker.set_temperature(m.heater_shaker_temperature)
        await heater_shaker.wait_for_temperature(m.heater_shaker_temperature)
        await heater_shaker.set_speed(m.heater_shaker_speed)
        await heater_shaker.get_status()
        await heater_shaker.stop_shaking()
        await heater_shaker.deactivate_heater()
        await heater_shaker.open_latch()
        await _move(labware, motion, plans["heater_shaker_to_magnetic_block"])
        await _move(labware, motion, plans["magnetic_block_to_process_deck"])

        record_property("phase_06", "temperature_module")
        await _move(labware, motion, plans["assay_deck_to_temperature_module"])
        await temperature_module.set_temperature_and_wait(m.temperature_module_target)
        await temperature_module.get_status()
        await temperature_module.deactivate()
        await _move(labware, motion, plans["temperature_module_to_assay_deck"])

        record_property("phase_07", "absorbance_reader")
        reader_status = await reader.get_status()
        assert reader_status.lid_status is ReaderLidStatus.ON
        assert reader_status.plate_presence is PlatePresence.ABSENT
        sample = next(value for value in m.plate_reader_wavelengths if value != m.plate_reader_reference_wavelength)
        await reader.initialize_single_with_reference(sample, m.plate_reader_reference_wavelength)
        await reader.initialize_multiple(list(m.plate_reader_wavelengths))
        await _move(labware, motion, plans["reader_lid_open"], lid=True)
        await _move(labware, motion, plans["assay_deck_to_reader"])
        await _move(labware, motion, plans["reader_lid_close"], lid=True)
        measurement = await reader.read_plate()
        assert isinstance(measurement, PlateMeasurement)
        assert {row.wavelength for row in measurement.measurements} == set(m.plate_reader_wavelengths)
        assert all(len(row.wells) == 96 for row in measurement.measurements)
        await _move(labware, motion, plans["reader_lid_open"], lid=True)
        await _move(labware, motion, plans["reader_to_assay_deck"])
        await _move(labware, motion, plans["reader_lid_close"], lid=True)
        await reader.deactivate()

        await _assert_machine_ok(motion, "at workflow completion")
        await _assert_deck_valid(labware, "at workflow completion")
        record_property("acceptance_result", "completed")
        # A failed run must never leave an approval-looking fingerprint in JUnit.
        record_property("commissioned_manifest_sha256", manifest.commissioning_digest())
    finally:
        # Only de-energize mechanisms here. Never guess a recovery
        # gripper route after a failed physical move; the operator must reconcile
        # the durable deck ledger first.
        # A failed pipetting move may leave the tip or liquid state uncertain.
        # Do not add another gantry move in cleanup; the operator must reconcile
        # and discard the tip after confirming that the robot is safe to home.
        with contextlib.suppress(Exception):
            await heater_shaker.stop_shaking()
        with contextlib.suppress(Exception):
            await heater_shaker.deactivate_heater()
        with contextlib.suppress(Exception):
            await thermocycler.deactivate_all()
        with contextlib.suppress(Exception):
            await temperature_module.deactivate()
        with contextlib.suppress(Exception):
            await reader.deactivate()
        with contextlib.suppress(Exception):
            await stacker.deactivate()
        with contextlib.suppress(Exception):
            await motion.observable("SetLights", {"button": False, "rails": False})
