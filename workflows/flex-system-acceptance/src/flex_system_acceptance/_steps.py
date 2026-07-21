"""Tracked Unitelabs steps for a manifest-driven Flex system acceptance run."""

from __future__ import annotations

import contextlib

from prefect import task
from prefect.cache_policies import NONE
from unitelabs.sdk import get_logger

from unitelabs.opentrons_flex.acceptance import (
    AcceptanceManifest,
    Coordinate,
    PlanContract,
    WellGeometryConfig,
    validate_plan_contract,
)
from unitelabs.opentrons_flex.features import (
    LiquidPosition,
    Mount,
    PipetteMount,
    TipLocation,
    TransferProfile,
    VerifiedLiquidClass,
    WellGeometry,
)
from unitelabs.opentrons_flex.features.thermocycler import ThermocyclerProfileStep

from ._helpers import feature, field, get_flex_service, invoke


def _position(value: Coordinate) -> LiquidPosition:
    return LiquidPosition(value.x, value.y, value.z)


def _well(value: WellGeometryConfig) -> WellGeometry:
    return WellGeometry(value.x, value.y, value.bottom_z, value.top_z, value.length, value.width)


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


async def _assert_machine_ready(features: dict, after: str) -> None:
    machine = await invoke(features["motion"], ("machine_status", "get_machine_status"))
    if field(machine, "is_error_state") or field(machine, "estop") != "DISENGAGED" or field(machine, "door_open"):
        msg = f"Flex is not ready {after}: clear errors, disengage the E-stop, and close the door."
        raise ValueError(msg)


async def _assert_deck_valid(features: dict, after: str) -> object:
    state = await invoke(features["labware"], ("deck_state", "get_deck_state"))
    if not field(state, "valid"):
        msg = f"Connector deck ledger is invalid {after}; reconcile physical locations before continuing."
        raise ValueError(msg)
    return state


async def _move(features: dict, plan_identifier: str, *, lid: bool = False) -> object:
    method = "move_lid" if lid else "move_labware"
    result = await invoke(features["labware"], method, plan_identifier=plan_identifier)
    await _assert_machine_ready(features, f"after {plan_identifier!r}")
    await _assert_deck_valid(features, f"after {plan_identifier!r}")
    return result


@task(name="Step: Validate Flex acceptance manifest", cache_policy=NONE, retries=0)
async def validate_manifest_step(manifest: dict) -> AcceptanceManifest:
    """Validate every physical input before connecting to the robot."""
    return AcceptanceManifest.parse(manifest)


@task(name="Step: Connect and preflight Flex", cache_policy=NONE, retries=0)
async def connect_and_preflight_step(device_name: str, manifest: AcceptanceManifest):
    """Connect, resolve every required feature, and verify module identities."""
    service, client = await get_flex_service(device_name)
    features = {
        name: feature(service, identifier)
        for name, identifier in {
            "motion": "MotionController",
            "pipette": "PipetteController",
            "tip": "TipController",
            "liquid": "LiquidHandlingController",
            "labware": "LabwareMovementController",
            "heater_shaker": "HeaterShakerController",
            "thermocycler": "ThermocyclerController",
            "temperature_module": "TemperatureController",
            "reader": "AbsorbanceReaderController",
            "stacker": "FlexStackerController",
            "stacker_maintenance": "FlexStackerMaintenanceController",
        }.items()
    }
    identities = (
        ("heater_shaker", manifest.modules.heater_shaker_serial),
        ("thermocycler", manifest.modules.thermocycler_serial),
        ("temperature_module", manifest.modules.temperature_module_serial),
        ("reader", manifest.modules.plate_reader_serial),
        ("stacker", manifest.modules.stacker_serial),
    )
    for name, expected in identities:
        info = await invoke(features[name], ("device_info", "get_device_info"))
        actual = field(info, "serial_number")
        if actual != expected:
            msg = f"{name} serial mismatch: expected {expected!r}, found {actual!r}."
            raise ValueError(msg)

    if await invoke(features["motion"], ("is_simulating", "get_is_simulating")):
        msg = "Hardware acceptance cannot run against a simulator."
        raise ValueError(msg)
    await _assert_machine_ready(features, "during preflight")

    plans = await invoke(features["labware"], ("available_plans", "get_available_plans"))
    state = await _assert_deck_valid(features, "during preflight")
    validate_plan_contract(
        manifest,
        tuple(
            PlanContract(
                str(field(plan, "plan_identifier")),
                str(field(plan, "labware_identifier")),
                str(field(plan, "source_location")),
                str(field(plan, "destination_location")),
                bool(field(plan, "is_lid")),
            )
            for plan in plans
        ),
        deck_valid=bool(field(state, "valid")),
        occupancy={
            str(field(item, "location_identifier")): str(field(item, "labware_identifier"))
            for item in field(state, "occupied_locations")
        },
    )

    mount = PipetteMount[manifest.pipetting.mount]
    attached = await invoke(features["pipette"], "get_attached_pipettes")
    selected = next((item for item in attached if _enum_value(field(item, "mount")) == mount.value), None)
    if selected is None or not field(selected, "attached"):
        msg = f"No pipette is attached on {mount.value}."
        raise ValueError(msg)
    for name, volume in (
        ("transfer_volume", manifest.pipetting.transfer_volume),
        ("mix_volume", manifest.pipetting.mix_volume),
    ):
        if not field(selected, "min_volume") <= volume <= field(selected, "max_volume"):
            msg = f"pipetting.{name} is outside the selected pipette range."
            raise ValueError(msg)

    reader_status = await invoke(features["reader"], ("status", "get_status"))
    unsupported = set(manifest.modules.plate_reader_wavelengths) - set(field(reader_status, "supported_wavelengths"))
    if unsupported:
        msg = f"Plate Reader does not support manifest wavelengths: {sorted(unsupported)}."
        raise ValueError(msg)
    return service, client, features


@task(name="Step: Home and configure Flex", cache_policy=NONE, retries=0)
async def home_and_configure_step(features: dict, manifest: AcceptanceManifest) -> None:
    """Home the robot and configure the selected pipette nozzle layout."""
    p = manifest.pipetting
    mount = PipetteMount[p.mount]
    await invoke(features["motion"], "set_lights", button=True, rails=True)
    await invoke(features["motion"], "home")
    await invoke(
        features["pipette"],
        "configure_full_nozzle_layout",
        mount=mount,
        tiprack_diameter=p.tiprack_diameter,
    )


@task(name="Step: Validate Flex Stacker", cache_policy=NONE, retries=0)
async def stacker_step(features: dict, manifest: AcceptanceManifest) -> None:
    """Retrieve and return one prepared labware item without claiming deck-ledger ownership."""
    height = manifest.modules.stacker_labware_height
    await invoke(features["stacker_maintenance"], "home_all", ignore_latch=False)
    await invoke(
        features["stacker"],
        "retrieve_labware",
        labware_height=height,
        enforce_hopper_labware_sensing=True,
        enforce_shuttle_labware_sensing=True,
    )
    await invoke(
        features["stacker"],
        "store_labware",
        labware_height=height,
        enforce_shuttle_labware_sensing=True,
    )
    await invoke(features["stacker_maintenance"], "deactivate")


@task(name="Step: Thermocycler profile", cache_policy=NONE, retries=0)
async def thermocycler_step(features: dict, manifest: AcceptanceManifest) -> None:
    """Move the process plate through a short guarded thermal profile."""
    controller = features["thermocycler"]
    m = manifest.modules
    await invoke(controller, "open_lid")
    await _move(features, manifest.plans["process_deck_to_thermocycler"])
    await invoke(controller, "close_lid")
    await invoke(controller, "set_lid_temperature", temperature=m.thermocycler_lid_temperature)
    await invoke(controller, "wait_for_lid_temperature")
    steps = [
        ThermocyclerProfileStep(step.temperature, step.hold_time, step.ramp_rate) for step in m.thermocycler_profile
    ]
    await invoke(controller, "execute_profile", steps=steps, repetitions=1, volume=m.thermocycler_volume)
    await invoke(controller, "deactivate_all")
    await invoke(controller, "open_lid")
    await _move(features, manifest.plans["thermocycler_to_process_deck"])


@task(name="Step: Flex liquid handling", cache_policy=NONE, retries=0)
async def liquid_handling_step(features: dict, manifest: AcceptanceManifest) -> None:
    """Probe, mix, run explicit and verified transfers, touch, then discard the tip."""
    p = manifest.pipetting
    mount = PipetteMount[p.mount]
    robot_mount = Mount[p.mount]
    await invoke(
        features["tip"],
        "pick_up_tip",
        mount=mount,
        location=TipLocation(p.tip_pickup.x, p.tip_pickup.y, p.tip_pickup.z),
        tip_length=p.tip_length,
        prep_after=False,
    )
    await invoke(
        features["motion"],
        "move_to",
        mount=robot_mount,
        x=p.source_retract.x,
        y=p.source_retract.y,
        z=p.source_retract.z,
        speed=0.0,
    )
    await invoke(features["liquid"], "probe_liquid_level", mount=mount, maximum_distance=p.probe_maximum_distance)
    await invoke(
        features["motion"],
        "move_to",
        mount=robot_mount,
        x=p.source.x,
        y=p.source.y,
        z=p.source.z,
        speed=0.0,
    )
    await invoke(
        features["liquid"],
        "mix",
        mount=mount,
        cycles=p.mix_cycles,
        volume=p.mix_volume,
        aspirate_rate=1.0,
        dispense_rate=1.0,
    )
    await invoke(
        features["liquid"],
        "transfer",
        mount=mount,
        source=_position(p.source),
        source_retract=_position(p.source_retract),
        destination=_position(p.destination),
        destination_retract=_position(p.destination_retract),
        volume=p.transfer_volume,
        profile=TransferProfile(1.0, 1.0, 0.0, 0.0, 0, 0.0, 0, 0.0, True),
    )
    await invoke(
        features["liquid"],
        "transfer_with_verified_liquid_class",
        mount=mount,
        source_well=_well(p.source_well),
        destination_well=_well(p.destination_well),
        volume=p.transfer_volume,
        liquid_class=VerifiedLiquidClass.WATER,
        tiprack_uri=p.tiprack_uri,
    )
    await invoke(
        features["liquid"],
        "touch_tip",
        mount=mount,
        well=_well(p.destination_well),
        z_offset=-1.0,
        distance_from_edge=1.0,
        speed=20.0,
    )
    await invoke(
        features["tip"],
        "drop_tip",
        mount=mount,
        location=TipLocation(p.tip_drop.x, p.tip_drop.y, p.tip_drop.z),
        home_after=False,
    )


@task(name="Step: Heater-Shaker and Magnetic Block", cache_policy=NONE, retries=0)
async def heater_shaker_step(features: dict, manifest: AcceptanceManifest) -> None:
    """Heat, shake, and move the process plate across the passive Magnetic Block."""
    hs = features["heater_shaker"]
    m = manifest.modules
    plans = manifest.plans
    await invoke(hs, "open_latch")
    await _move(features, plans["process_deck_to_heater_shaker"])
    await invoke(hs, "close_latch")
    await invoke(hs, "set_temperature", temperature=m.heater_shaker_temperature)
    await invoke(hs, "wait_for_temperature", temperature=m.heater_shaker_temperature)
    await invoke(hs, "set_speed", speed=m.heater_shaker_speed)
    await invoke(hs, "get_status")
    await invoke(hs, "stop_shaking")
    await invoke(hs, "deactivate_heater")
    await invoke(hs, "open_latch")
    await _move(features, plans["heater_shaker_to_magnetic_block"])
    await _move(features, plans["magnetic_block_to_process_deck"])


@task(name="Step: Temperature Module", cache_policy=NONE, retries=0)
async def temperature_module_step(features: dict, manifest: AcceptanceManifest) -> None:
    """Move the assay plate onto the Temperature Module and hold its target."""
    controller = features["temperature_module"]
    await _move(features, manifest.plans["assay_deck_to_temperature_module"])
    await invoke(controller, "set_temperature_and_wait", temperature=manifest.modules.temperature_module_target)
    await invoke(controller, "deactivate")
    await _move(features, manifest.plans["temperature_module_to_assay_deck"])


@task(name="Step: Absorbance Plate Reader", cache_policy=NONE, retries=0)
async def plate_reader_step(features: dict, manifest: AcceptanceManifest) -> None:
    """Configure, cover, measure, uncover, and remove the prepared assay plate."""
    reader = features["reader"]
    m = manifest.modules
    plans = manifest.plans
    sample = next(value for value in m.plate_reader_wavelengths if value != m.plate_reader_reference_wavelength)
    await invoke(
        reader,
        "initialize_single_with_reference",
        wavelength=sample,
        reference_wavelength=m.plate_reader_reference_wavelength,
    )
    await invoke(reader, "initialize_multiple", wavelengths=list(m.plate_reader_wavelengths))
    await _move(features, plans["reader_lid_open"], lid=True)
    await _move(features, plans["assay_deck_to_reader"])
    await _move(features, plans["reader_lid_close"], lid=True)
    measurement = await invoke(reader, "read_plate")
    rows = field(measurement, "measurements")
    if any(len(field(row, "wells")) != 96 for row in rows):
        msg = "Plate Reader did not return exactly 96 wells per wavelength."
        raise ValueError(msg)
    await _move(features, plans["reader_lid_open"], lid=True)
    await _move(features, plans["reader_to_assay_deck"])
    await _move(features, plans["reader_lid_close"], lid=True)
    await invoke(reader, "deactivate")


async def safe_shutdown(features: dict) -> None:
    """Best-effort de-energization that never invents a gripper recovery move."""
    actions = (
        ("heater_shaker", "stop_shaking"),
        ("heater_shaker", "deactivate_heater"),
        ("thermocycler", "deactivate_all"),
        ("temperature_module", "deactivate"),
        ("reader", "deactivate"),
        ("stacker_maintenance", "deactivate"),
    )
    for feature_name, method in actions:
        with contextlib.suppress(Exception):
            await invoke(features[feature_name], method)
    with contextlib.suppress(Exception):
        await invoke(features["motion"], "set_lights", button=False, rails=False)
    get_logger().info("Flex acceptance shutdown actions completed; reconcile labware positions after any failure")
