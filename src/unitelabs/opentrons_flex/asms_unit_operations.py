"""
Guarded SiLA-only ASMS unit-operation qualification client.

This module intentionally does not upload a protocol or create a Protocol
Engine run.  It talks only to the connector's SiLA surface and exposes one
mechanical unit operation at a time.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import grpc
import grpc.aio
from sila.server import CommandConfirmation, CommandExecutionUUID
from unitelabs.cdk import SiLAServerConfig

from . import OpentronsFlexConfig, create_app
from .features import (
    LabwareDeckState,
    LabwareMovementProfile,
    LabwareMovementResult,
    LabwarePlanSummary,
    LiquidPosition,
    PipetteInfo,
    PipetteMount,
    TipLocation,
    TipPresence,
    TransferProfile,
)
from .features.gripper import GripperStatus
from .features.motion_control import MachineStatus

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
_GRIPPER_PACKAGE = "sila2.ca.accelerationconsortium.robots.grippercontroller.v1"
_GRIPPER_SERVICE = f"{_GRIPPER_PACKAGE}.GripperController"
_TEMPERATURE_PACKAGE = "sila2.ca.accelerationconsortium.modules.temperaturecontroller.v2"
_TEMPERATURE_SERVICE = f"{_TEMPERATURE_PACKAGE}.TemperatureController"

_DECK_CONFIRMATION = "ASMS-UNIT-DECK-READY"
_STAGING_CONFIRMATION = "ASMS-UNIT-STAGING-A4-B4-C4-D4-CLEAR"
_PLATE_CONFIRMATION = "ASMS-UNIT-EMPTY-AXYGEN-A1-B1"
_TIP_CONFIRMATION = "ASMS-UNIT-TIPS-A3-A1"
_LIQUID_CONFIRMATION = "ASMS-UNIT-WATER-A2-TO-C2"
_TEMPERATURE_CONFIRMATION = "ASMS-UNIT-TEMP-C1-READY"
_ACTUATION_PHASES = ("home", "gripper-a1", "gripper-b1", "tip", "liquid", "temperature")
_PHASES = ("health", "preflight", *_ACTUATION_PHASES)


class UnitOperationError(RuntimeError):
    """Operator-recoverable local qualification failure."""


@dataclass(frozen=True)
class PositionConfig:
    """One absolute deck coordinate in millimetres."""

    x: float
    y: float
    z: float


@dataclass(frozen=True)
class PipettingConfig:
    """Prepared pipette, tip, and liquid geometry."""

    mount: str
    tiprack_uri: str
    tiprack_diameter: float
    tip_length: float
    tip_location: PositionConfig
    source: PositionConfig
    source_retract: PositionConfig
    destination: PositionConfig
    destination_retract: PositionConfig
    volume: float


@dataclass(frozen=True)
class UnitOperationManifest:
    """Local qualification contract; no geometry is accepted from SiLA callers."""

    expected_staging_slots: tuple[str, ...]
    movement_profile_sha256: str
    expected_pipette_models: tuple[str, ...]
    expected_plan_routes: dict[str, tuple[str, str, str]]
    initial_occupancy: dict[str, str]
    pipetting: PipettingConfig
    temperature_target: float


class UnitClient(Protocol):
    """Narrow client contract used by the guarded phase runner."""

    async def is_simulating(self) -> bool: ...

    async def machine_status(self) -> MachineStatus: ...

    async def attached_pipettes(self) -> list[PipetteInfo]: ...

    async def gripper_status(self) -> GripperStatus: ...

    async def available_plans(self) -> list[LabwarePlanSummary]: ...

    async def deck_state(self) -> LabwareDeckState: ...

    async def movement_profile(self) -> LabwareMovementProfile: ...

    async def temperature_device_info(self) -> object: ...

    async def home(self) -> None: ...

    async def move_labware(self, plan_identifier: str) -> LabwareMovementResult: ...

    async def configure_full_nozzle(self, mount: PipetteMount, tiprack_diameter: float) -> object: ...

    async def tip_presence(self, mount: PipetteMount) -> TipPresence: ...

    async def pick_up_tip(
        self,
        mount: PipetteMount,
        location: TipLocation,
        tip_length: float,
    ) -> TipPresence: ...

    async def drop_tip(self, mount: PipetteMount, location: TipLocation) -> TipPresence: ...

    async def transfer(
        self,
        mount: PipetteMount,
        config: PipettingConfig,
    ) -> None: ...

    async def set_temperature(self, target: float) -> object: ...

    async def deactivate_temperature(self) -> object: ...


def load_manifest(path: str | Path) -> UnitOperationManifest:
    """Load and validate the locally provisioned unit-operation contract."""
    manifest_path = Path(path).expanduser()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UnitOperationError(f"Cannot read ASMS unit manifest {manifest_path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise UnitOperationError("ASMS unit manifest must be a schema_version 1 JSON object.")

    staging = _string_tuple(data.get("expected_staging_slots"), "expected_staging_slots")
    if staging != ("A4", "B4", "C4", "D4"):
        raise UnitOperationError("Expected staging slots must be exactly A4, B4, C4, D4 for this deck.")
    movement_profile_sha256 = _text(data.get("movement_profile_sha256"), "movement_profile_sha256")
    expected_profile_sha256 = "fb8fa69a3d781acd2f0edb1a6e710dec626cdf5661b5d9db46b35bab88df14f3"
    if movement_profile_sha256 != expected_profile_sha256:
        raise UnitOperationError("Movement profile digest does not match the pinned ASMS gripper geometry.")
    pipette_models = _string_tuple(data.get("expected_pipette_models"), "expected_pipette_models")
    if pipette_models != ("flex_8channel_1000", "p1000_multi_flex"):
        raise UnitOperationError("Expected pipette models do not match the exact RIGHT eight-channel API aliases.")
    routes_raw = _mapping(data.get("expected_plan_routes"), "expected_plan_routes")
    routes: dict[str, tuple[str, str, str]] = {}
    for identifier, value in routes_raw.items():
        if not isinstance(identifier, str) or not identifier:
            raise UnitOperationError("Every movement plan identifier must be a non-empty string.")
        route = _string_tuple(value, f"expected_plan_routes[{identifier!r}]")
        if len(route) != 3:
            raise UnitOperationError(f"Plan {identifier!r} must declare labware, source, and destination.")
        routes[identifier] = (route[0], route[1], route[2])
    required_routes = {
        "asms-v1-a1-to-b2",
        "asms-v1-b2-to-a1",
        "asms-v1-b1-to-b2",
        "asms-v1-b2-to-b1",
    }
    if set(routes) != required_routes:
        raise UnitOperationError(f"Movement plan set does not match the exact ASMS unit routes: {sorted(routes)}.")

    occupancy_raw = _mapping(data.get("initial_occupancy"), "initial_occupancy")
    occupancy = {
        _text(key, "initial_occupancy location"): _text(value, f"initial_occupancy[{key!r}]")
        for key, value in occupancy_raw.items()
    }
    if occupancy != {"A1": "asms-plate", "B1": "elution-plate"}:
        raise UnitOperationError("Initial deck ledger must contain only asms-plate at A1 and elution-plate at B1.")

    p = _mapping(data.get("pipetting"), "pipetting")
    pipetting = PipettingConfig(
        mount=_text(p.get("mount"), "pipetting.mount"),
        tiprack_uri=_text(p.get("tiprack_uri"), "pipetting.tiprack_uri"),
        tiprack_diameter=_positive_number(p.get("tiprack_diameter"), "pipetting.tiprack_diameter"),
        tip_length=_positive_number(p.get("tip_length"), "pipetting.tip_length"),
        tip_location=_position(p.get("tip_location"), "pipetting.tip_location"),
        source=_position(p.get("source"), "pipetting.source"),
        source_retract=_position(p.get("source_retract"), "pipetting.source_retract"),
        destination=_position(p.get("destination"), "pipetting.destination"),
        destination_retract=_position(p.get("destination_retract"), "pipetting.destination_retract"),
        volume=_positive_number(p.get("volume"), "pipetting.volume"),
    )
    if pipetting.mount != "RIGHT":
        raise UnitOperationError("The exact ASMS unit manifest requires the RIGHT pipette mount.")
    expected_pipetting = PipettingConfig(
        mount="RIGHT",
        tiprack_uri="opentrons/opentrons_flex_96_tiprack_1000ul/1",
        tiprack_diameter=5.47,
        tip_length=95.6,
        tip_location=PositionConfig(342.38, 395.38, 99.0),
        source=PositionConfig(23.38, 149.74, 18.85),
        source_retract=PositionConfig(23.38, 149.74, 51.4),
        destination=PositionConfig(178.21, 181.62, 12.22),
        destination_retract=PositionConfig(178.21, 181.62, 41.7),
        volume=20.0,
    )
    if pipetting != expected_pipetting:
        raise UnitOperationError("Pipetting geometry does not match the pinned ASMS unit-operation contract.")
    target = _positive_number(data.get("temperature_target"), "temperature_target")
    if target != 4.0:
        raise UnitOperationError("temperature_target must be exactly 4 degrees Celsius for this unit test.")
    return UnitOperationManifest(
        staging,
        movement_profile_sha256,
        pipette_models,
        routes,
        occupancy,
        pipetting,
        target,
    )


def required_confirmations(phase: str) -> dict[str, str]:
    """Return the exact human confirmation tokens required by one phase."""
    if phase not in _ACTUATION_PHASES:
        return {}
    required = {
        "confirm_deck_ready": _DECK_CONFIRMATION,
        "confirm_staging_slots_clear": _STAGING_CONFIRMATION,
    }
    if phase.startswith("gripper"):
        required["confirm_empty_plates"] = _PLATE_CONFIRMATION
    elif phase == "tip":
        required["confirm_tip_column"] = _TIP_CONFIRMATION
    elif phase == "liquid":
        required["confirm_tip_column"] = _TIP_CONFIRMATION
        required["confirm_test_liquid"] = _LIQUID_CONFIRMATION
    elif phase == "temperature":
        required["confirm_temperature_module"] = _TEMPERATURE_CONFIRMATION
    return required


async def run_phase(
    manifest: UnitOperationManifest,
    client: UnitClient,
    phase: str,
    *,
    execute: bool = False,
    confirmations: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Validate or run exactly one guarded unit operation."""
    if phase not in _PHASES:
        raise UnitOperationError(f"Unknown phase {phase!r}.")
    if phase in _ACTUATION_PHASES:
        if not execute:
            raise UnitOperationError(f"{phase} is an actuation phase; pass --execute after reviewing the deck.")
        supplied = confirmations or {}
        for name, expected in required_confirmations(phase).items():
            if supplied.get(name) != expected:
                raise UnitOperationError(f"{name} must exactly equal {expected!r}.")

    inventory = await _inventory(manifest, client, require_ready=phase != "health")
    if phase == "health":
        return {"phase": phase, **inventory}
    if phase == "preflight":
        return {"phase": phase, **inventory, "ready": True}

    if phase == "home":
        await client.home()
    elif phase == "gripper-a1":
        await _round_trip(client, "asms-v1-a1-to-b2", "asms-v1-b2-to-a1")
    elif phase == "gripper-b1":
        await _round_trip(client, "asms-v1-b1-to-b2", "asms-v1-b2-to-b1")
    elif phase in {"tip", "liquid"}:
        await _run_tip_or_liquid(client, manifest.pipetting, transfer=phase == "liquid")
    elif phase == "temperature":
        await _run_temperature(client, manifest.temperature_target)

    after = await _require_machine_ready(client)
    return {
        "phase": phase,
        "completed": True,
        "machine_status": _machine_payload(after),
        "operator_note": (
            "If physical state differs from this result, stop and reconcile the deck before another command."
        ),
    }


async def _inventory(
    manifest: UnitOperationManifest,
    client: UnitClient,
    *,
    require_ready: bool,
) -> dict[str, Any]:
    if await client.is_simulating():
        raise UnitOperationError("The target reports simulation; live unit qualification requires the real Flex.")

    pipettes = await client.attached_pipettes()
    right = next((item for item in pipettes if _enum_text(item.mount) == "RIGHT"), None)
    if right is None or not right.attached:
        raise UnitOperationError("No pipette is attached on the RIGHT mount.")
    if right.model not in manifest.expected_pipette_models:
        raise UnitOperationError(
            f"RIGHT pipette model {right.model!r} is not one of {manifest.expected_pipette_models!r}."
        )
    if right.channels != 8:
        raise UnitOperationError(f"RIGHT pipette must have 8 channels; connector reported {right.channels}.")
    if not right.min_volume <= manifest.pipetting.volume <= right.max_volume:
        raise UnitOperationError(
            f"Test volume {manifest.pipetting.volume} is outside RIGHT pipette range "
            f"{right.min_volume}-{right.max_volume}."
        )
    if require_ready and right.has_tip:
        raise UnitOperationError(
            "RIGHT pipette reports an attached tip. Remove it physically, restart connector-unit mode, "
            "and rerun preflight before actuation."
        )

    gripper = await client.gripper_status()
    if not gripper.attached:
        raise UnitOperationError("The Flex gripper is not attached.")

    plans = await client.available_plans()
    profile = await client.movement_profile()
    if profile.sha256 != manifest.movement_profile_sha256:
        raise UnitOperationError(
            "Connector movement profile digest does not match the pinned local geometry. "
            f"Expected {manifest.movement_profile_sha256}, found {profile.sha256}."
        )
    actual_routes = {
        plan.plan_identifier: (
            plan.labware_identifier,
            plan.source_location,
            plan.destination_location,
        )
        for plan in plans
        if not plan.is_lid
    }
    if actual_routes != manifest.expected_plan_routes:
        raise UnitOperationError(f"Connector movement plans do not match the local manifest. Found {actual_routes!r}.")

    deck = await client.deck_state()
    occupancy = {item.location_identifier: item.labware_identifier for item in deck.occupied_locations}
    machine = await client.machine_status()
    temperature_info = await client.temperature_device_info()
    if require_ready:
        _validate_machine_ready(machine)
        if not deck.valid:
            raise UnitOperationError("The durable deck ledger is invalid; physically reconcile it before actuation.")
        if occupancy != manifest.initial_occupancy:
            raise UnitOperationError(
                f"Deck ledger does not match the prepared initial state. Expected "
                f"{manifest.initial_occupancy!r}, found {occupancy!r}."
            )

    return {
        "connector_health": "PASS",
        "hardware_inventory": "PASS",
        "pipette_model": right.model,
        "pipette_has_tip": right.has_tip,
        "gripper_model": gripper.model,
        "temperature_module_model": str(getattr(temperature_info, "model", "")),
        "temperature_module_serial": str(getattr(temperature_info, "serial_number", "")),
        "plan_routes": sorted(actual_routes),
        "deck_ledger_valid": deck.valid,
        "deck_occupancy": occupancy,
        "machine_status": _machine_payload(machine),
        "staging_slots_to_keep_clear": list(manifest.expected_staging_slots),
    }


async def _round_trip(client: UnitClient, outbound: str, inbound: str) -> None:
    first = await client.move_labware(outbound)
    if first.plan_identifier != outbound:
        raise UnitOperationError(f"Connector returned unexpected outbound plan {first.plan_identifier!r}.")
    await _require_machine_ready(client)
    second = await client.move_labware(inbound)
    if second.plan_identifier != inbound:
        raise UnitOperationError(f"Connector returned unexpected return plan {second.plan_identifier!r}.")


async def _run_tip_or_liquid(client: UnitClient, config: PipettingConfig, *, transfer: bool) -> None:
    mount = PipetteMount[config.mount]
    await client.configure_full_nozzle(mount, config.tiprack_diameter)
    if await client.tip_presence(mount) is not TipPresence.ABSENT:
        raise UnitOperationError("RIGHT pipette already has a tip; reconcile it before the prepared pickup.")
    location = TipLocation(config.tip_location.x, config.tip_location.y, config.tip_location.z)
    picked = await client.pick_up_tip(mount, location, config.tip_length)
    if picked is not TipPresence.PRESENT:
        raise UnitOperationError("Tip pickup returned without a sensor-verified attached tip.")
    try:
        if transfer:
            await client.transfer(mount, config)
    finally:
        dropped = await client.drop_tip(mount, location)
    if dropped is not TipPresence.ABSENT:
        raise UnitOperationError("Tip return finished without a sensor-verified empty pipette.")


async def _run_temperature(client: UnitClient, target: float) -> None:
    try:
        await client.set_temperature(target)
    finally:
        await client.deactivate_temperature()


async def _require_machine_ready(client: UnitClient) -> MachineStatus:
    status = await client.machine_status()
    _validate_machine_ready(status)
    return status


def _validate_machine_ready(status: MachineStatus) -> None:
    if status.is_error_state:
        raise UnitOperationError(f"Flex reports an error state: {status.message}")
    if status.estop != "DISENGAGED":
        raise UnitOperationError(f"Flex E-stop is {status.estop}; disengage and re-home before actuation.")
    if status.door_open:
        raise UnitOperationError("Flex door is open; close it before actuation.")


class GrpcUnitClient:
    """Raw gRPC client for the connector's generated SiLA feature surface."""

    def __init__(self, channel: grpc.aio.Channel, protobuf: object) -> None:
        self._channel = channel
        self._protobuf = protobuf

    async def _unary(self, path: str, request: bytes, *, timeout: float) -> bytes:
        call = self._channel.unary_unary(path)
        return await asyncio.wait_for(call(request, timeout=timeout), timeout=timeout + 1.0)

    async def _property(
        self,
        service: str,
        package: str,
        method: str,
        *,
        timeout: float = 15.0,
    ) -> object:
        response = await self._unary(f"/{service}/{method}", b"", timeout=timeout)
        decoded = await self._protobuf.decode(f"{package}.{method}_Responses", response)
        return next(iter(decoded.values()))

    async def _command(
        self,
        service: str,
        package: str,
        method: str,
        parameters: dict[str, Any] | None = None,
    ) -> object:
        request = await self._protobuf.encode(f"{package}.{method}_Parameters", parameters or {})
        response = await self._unary(f"/{service}/{method}", request, timeout=30.0)
        decoded = await self._protobuf.decode(f"{package}.{method}_Responses", response)
        return next(iter(decoded.values()), None)

    async def _observable(
        self,
        service: str,
        package: str,
        method: str,
        parameters: dict[str, Any] | None = None,
        *,
        timeout: float = 300.0,
        emergency_on_failure: bool = True,
    ) -> object:
        try:
            return await asyncio.wait_for(
                self._poll_observable(service, package, method, parameters, timeout=timeout),
                timeout=timeout,
            )
        except asyncio.CancelledError:
            if emergency_on_failure:
                await self._halt_after_uncertain_execution(method)
            raise
        except (asyncio.TimeoutError, TimeoutError, grpc.aio.AioRpcError) as exc:
            timed_out = isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or (
                exc.code() is grpc.StatusCode.DEADLINE_EXCEEDED
            )
            if not timed_out:
                raise
            if emergency_on_failure:
                await self._halt_after_uncertain_execution(method)
            raise UnitOperationError(
                f"{method} exceeded its {timeout:g}-second deadline. EmergencyStop was issued; "
                "physically inspect the robot and complete recovery before another operation."
            ) from exc

    async def _poll_observable(
        self,
        service: str,
        package: str,
        method: str,
        parameters: dict[str, Any] | None,
        *,
        timeout: float,
    ) -> object:
        request = await self._protobuf.encode(f"{package}.{method}_Parameters", parameters or {})
        confirmation_raw = await self._unary(
            f"/{service}/{method}",
            request,
            timeout=min(15.0, timeout),
        )
        confirmation = CommandConfirmation.decode(confirmation_raw)
        execution_id = CommandExecutionUUID(value=confirmation.command_execution_uuid.value).encode()
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                response = await self._unary(
                    f"/{service}/{method}_Result",
                    execution_id,
                    timeout=min(5.0, remaining),
                )
                decoded = await self._protobuf.decode(f"{package}.{method}_Responses", response)
                return next(iter(decoded.values()), None)
            except grpc.aio.AioRpcError as exc:
                details = base64.b64decode(exc.details() or "")
                not_ready = exc.code() is grpc.StatusCode.ABORTED and b"Result is not ready" in details
                if not not_ready:
                    raise
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError from exc
                await asyncio.sleep(0.1)

    async def _halt_after_uncertain_execution(self, method: str) -> None:
        async def halt() -> None:
            try:
                await self._observable(
                    _MOTION_SERVICE,
                    _MOTION_PACKAGE,
                    "EmergencyStop",
                    timeout=30.0,
                    emergency_on_failure=False,
                )
            except Exception as exc:
                raise UnitOperationError(
                    f"{method} execution state is uncertain and EmergencyStop could not be confirmed. "
                    "Press the physical E-stop now and do not continue until the deck and tip state are reconciled."
                ) from exc

        halt_task = asyncio.create_task(halt())
        while not halt_task.done():
            try:
                await asyncio.shield(halt_task)
            except asyncio.CancelledError:
                continue
        await halt_task

    async def is_simulating(self) -> bool:
        return bool(await self._property(_MOTION_SERVICE, _MOTION_PACKAGE, "Get_IsSimulating"))

    async def machine_status(self) -> MachineStatus:
        return await self._property(_MOTION_SERVICE, _MOTION_PACKAGE, "Get_MachineStatus")  # type: ignore[return-value]

    async def attached_pipettes(self) -> list[PipetteInfo]:
        return await self._observable(
            _PIPETTE_SERVICE,
            _PIPETTE_PACKAGE,
            "GetAttachedPipettes",
            timeout=30.0,
            emergency_on_failure=False,
        )  # type: ignore[return-value]

    async def gripper_status(self) -> GripperStatus:
        return await self._property(_GRIPPER_SERVICE, _GRIPPER_PACKAGE, "Get_Status")  # type: ignore[return-value]

    async def available_plans(self) -> list[LabwarePlanSummary]:
        return await self._property(_LABWARE_SERVICE, _LABWARE_PACKAGE, "Get_AvailablePlans")  # type: ignore[return-value]

    async def deck_state(self) -> LabwareDeckState:
        return await self._property(_LABWARE_SERVICE, _LABWARE_PACKAGE, "Get_DeckState")  # type: ignore[return-value]

    async def movement_profile(self) -> LabwareMovementProfile:
        return await self._property(
            _LABWARE_SERVICE,
            _LABWARE_PACKAGE,
            "Get_ProfileIdentity",
        )  # type: ignore[return-value]

    async def temperature_device_info(self) -> object:
        return await self._property(_TEMPERATURE_SERVICE, _TEMPERATURE_PACKAGE, "Get_DeviceInfo")

    async def home(self) -> None:
        await self._observable(_MOTION_SERVICE, _MOTION_PACKAGE, "Home", timeout=180.0)

    async def move_labware(self, plan_identifier: str) -> LabwareMovementResult:
        return await self._observable(
            _LABWARE_SERVICE,
            _LABWARE_PACKAGE,
            "MoveLabware",
            {"plan_identifier": plan_identifier},
        )  # type: ignore[return-value]

    async def configure_full_nozzle(self, mount: PipetteMount, tiprack_diameter: float) -> object:
        return await self._command(
            _PIPETTE_SERVICE,
            _PIPETTE_PACKAGE,
            "ConfigureFullNozzleLayout",
            {"mount": mount, "tiprack_diameter": tiprack_diameter},
        )

    async def tip_presence(self, mount: PipetteMount) -> TipPresence:
        return await self._command(
            _TIP_SERVICE,
            _TIP_PACKAGE,
            "GetTipPresence",
            {"mount": mount},
        )  # type: ignore[return-value]

    async def pick_up_tip(
        self,
        mount: PipetteMount,
        location: TipLocation,
        tip_length: float,
    ) -> TipPresence:
        return await self._observable(
            _TIP_SERVICE,
            _TIP_PACKAGE,
            "PickUpTip",
            {
                "mount": mount,
                "location": location,
                "tip_length": tip_length,
                "prep_after": False,
            },
        )  # type: ignore[return-value]

    async def drop_tip(self, mount: PipetteMount, location: TipLocation) -> TipPresence:
        return await self._observable(
            _TIP_SERVICE,
            _TIP_PACKAGE,
            "DropTip",
            {"mount": mount, "location": location, "home_after": False},
        )  # type: ignore[return-value]

    async def transfer(self, mount: PipetteMount, config: PipettingConfig) -> None:
        await self._observable(
            _LIQUID_SERVICE,
            _LIQUID_PACKAGE,
            "Transfer",
            {
                "mount": mount,
                "source": LiquidPosition(config.source.x, config.source.y, config.source.z),
                "source_retract": LiquidPosition(
                    config.source_retract.x,
                    config.source_retract.y,
                    config.source_retract.z,
                ),
                "destination": LiquidPosition(
                    config.destination.x,
                    config.destination.y,
                    config.destination.z,
                ),
                "destination_retract": LiquidPosition(
                    config.destination_retract.x,
                    config.destination_retract.y,
                    config.destination_retract.z,
                ),
                "volume": config.volume,
                "profile": TransferProfile(1.0, 1.0, 0.0, 0.0, 0, 0.0, 0, 0.0, True),
            },
        )

    async def set_temperature(self, target: float) -> object:
        return await self._observable(
            _TEMPERATURE_SERVICE,
            _TEMPERATURE_PACKAGE,
            "SetTemperature",
            {"temperature": target},
            timeout=120.0,
        )

    async def deactivate_temperature(self) -> object:
        return await self._observable(
            _TEMPERATURE_SERVICE,
            _TEMPERATURE_PACKAGE,
            "Deactivate",
            timeout=120.0,
        )


@contextlib.asynccontextmanager
async def live_client(address: str) -> AsyncIterator[GrpcUnitClient]:
    """Build a local codec, then connect it to the selected live SiLA server."""
    config = OpentronsFlexConfig(
        use_simulator=True,
        simulated_gripper=True,
        simulated_temperature_module=True,
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
    )
    application = create_app(config)
    connector = await application.__anext__()
    channel = grpc.aio.insecure_channel(address)
    try:
        yield GrpcUnitClient(channel, connector.sila_server.protobuf)
    finally:
        await channel.close()
        with contextlib.suppress(StopAsyncIteration):
            await application.__anext__()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate one guarded ASMS unit operation through connector-only SiLA."
    )
    parser.add_argument("phase", choices=_PHASES)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-deck-ready")
    parser.add_argument("--confirm-staging-slots-clear")
    parser.add_argument("--confirm-empty-plates")
    parser.add_argument("--confirm-tip-column")
    parser.add_argument("--confirm-test-liquid")
    parser.add_argument("--confirm-temperature-module")
    return parser


async def _main_async(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load_manifest(args.manifest)
    confirmations = {
        "confirm_deck_ready": args.confirm_deck_ready,
        "confirm_staging_slots_clear": args.confirm_staging_slots_clear,
        "confirm_empty_plates": args.confirm_empty_plates,
        "confirm_tip_column": args.confirm_tip_column,
        "confirm_test_liquid": args.confirm_test_liquid,
        "confirm_temperature_module": args.confirm_temperature_module,
    }
    async with live_client(f"{args.host}:{args.port}") as client:
        return await run_phase(
            manifest,
            client,
            args.phase,
            execute=args.execute,
            confirmations=confirmations,
        )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point with stable JSON output for the Windows lab operator."""
    args = _parser().parse_args(argv)
    try:
        result = asyncio.run(_main_async(args))
    except (UnitOperationError, OSError, TimeoutError, grpc.RpcError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


def _mapping(value: object, name: str) -> dict[Any, Any]:
    if not isinstance(value, dict):
        raise UnitOperationError(f"{name} must be a JSON object.")
    return value


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise UnitOperationError(f"{name} must be an array of non-empty strings.")
    return tuple(value)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise UnitOperationError(f"{name} must be a non-empty string.")
    return value


def _positive_number(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise UnitOperationError(f"{name} must be a positive number.")
    return float(value)


def _position(value: object, name: str) -> PositionConfig:
    raw = _mapping(value, name)
    coordinates = []
    for axis in ("x", "y", "z"):
        coordinate = raw.get(axis)
        if not isinstance(coordinate, (int, float)) or isinstance(coordinate, bool):
            raise UnitOperationError(f"{name}.{axis} must be a number.")
        coordinates.append(float(coordinate))
    return PositionConfig(*coordinates)


def _enum_text(value: object) -> str:
    return str(getattr(value, "value", value))


def _machine_payload(status: MachineStatus) -> dict[str, Any]:
    return {
        "estop": status.estop,
        "door_open": status.door_open,
        "is_error_state": status.is_error_state,
        "message": status.message,
    }


if __name__ == "__main__":
    raise SystemExit(main())
