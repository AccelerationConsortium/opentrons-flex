"""Guarded, operator-supervised induced-failure tests against a real Flex.

These tests prove the connector fails *safely* under representative error
conditions instead of merely forwarding commands. Every failure is expected to
surface as a SiLA Defined Execution Error (base64-encoded in the gRPC status
details) and must not leave the robot in an unrecoverable actuation state.

Test order is deliberate:

1. `test_channel_reconnect_recovers` — no failure; verifies the client can
   drop and re-establish the gRPC channel and read authoritative state. This
   is the closest automated proxy for "connector restart and reconnect" that
   does not require an operator to restart the deployed service (the full
   restart is documented in the runbook).
2. `test_volume_out_of_range_rejected` — a volume far above pipette capacity
   must be rejected before any actuation and leave the machine error-free.
3. `test_unknown_labware_plan_rejected` — an unprovisioned plan identifier
   must be rejected and leave the durable deck ledger valid.
4. `test_tip_pickup_failure_fail_closed` — MUST run last. The operator must
   remove the tip from the declared pickup position before the run; the
   sensor-verified pickup then fails and quarantines the mount. After this
   test the connector needs an operator reconciliation and restart (runbook).

Run with:

    uv run --extra test python -m pytest tests/integration/hardware/test_hitl_induced_failures.py \
        --robot <robot-ip>:50051 \
        --acceptance-manifest /path/to/flex_acceptance.json \
        --induced-failure-actuation -v
"""

from __future__ import annotations

import base64

import grpc.aio
import pytest

from unitelabs.opentrons_flex.acceptance import AcceptanceManifest
from unitelabs.opentrons_flex.features import (
    LabwareDeckState,
    Mount,
    PipetteMount,
    TipLocation,
    VerifiedLiquidClass,
    WellGeometry,
)
from unitelabs.opentrons_flex.features.motion_control import MachineStatus

from ..observable import call_observable

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

pytestmark = [
    pytest.mark.hardware_only,
    pytest.mark.induced_failure_actuation,
]

# A volume far beyond any Flex pipette capacity; must be rejected at the
# connector boundary before the hardware lock is even exercised.
_IMPOSSIBLE_VOLUME = 1_000_000.0


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

    async def observable(self, method: str, parameters: dict | None = None, timeout_s: float = 300.0) -> object:
        decoded = await call_observable(
            self._channel,
            self._protobuf,
            self._service,
            self._package,
            method,
            parameters,
            timeout_s=timeout_s,
        )
        return next(iter(decoded.values()), None)

    async def property(self, method: str) -> object:
        response = await self._channel.unary_unary(f"/{self._service}/{method}")(b"")
        decoded = await self._protobuf.decode(f"{self._package}.{method}_Responses", response)
        return next(iter(decoded.values()))


def _error_details(exc: grpc.aio.AioRpcError) -> bytes:
    """The SiLA Defined Execution Error travels base64-encoded in status details."""
    return base64.b64decode(exc.details() or b"")


def _well(value) -> WellGeometry:
    return WellGeometry(value.x, value.y, value.bottom_z, value.top_z, value.length, value.width)


def _mounts(name: str) -> tuple[Mount, PipetteMount]:
    return Mount[name], PipetteMount[name]


async def _assert_machine_ok(motion: _FeatureClient, after: str) -> MachineStatus:
    status = await motion.property("Get_MachineStatus")
    assert isinstance(status, MachineStatus)
    assert status.estop == "DISENGAGED", f"E-stop engaged {after}: {status.estop}"
    assert status.door_open is False, f"Flex door open {after}; close it"
    return status


def _require_manifest(acceptance_manifest: AcceptanceManifest | None) -> AcceptanceManifest:
    assert acceptance_manifest is not None, "--acceptance-manifest is required for induced-failure tests"
    return acceptance_manifest


async def _preflight(motion: _FeatureClient, pipette: _FeatureClient) -> tuple[Mount, PipetteMount]:
    assert await motion.property("Get_IsSimulating") is False, "induced-failure tests cannot run against a simulator"
    await _assert_machine_ok(motion, "during preflight")
    attached = await pipette.observable("GetAttachedPipettes")
    # Preflight requires the pipette to be present so the tests fail closed
    # before any actuation if the deck is not prepared.
    assert attached, "no attached pipette inventory"
    return _mounts("LEFT")  # mount is only used for typed parameters; failures are mount-agnostic


async def test_channel_reconnect_recovers(
    sila_channel,
    run_context,
) -> None:
    """A dropped gRPC channel can be re-established and authoritative state read again."""
    channel, protobuf = sila_channel
    motion = _FeatureClient(channel, protobuf, _MOTION_PACKAGE, _MOTION_SERVICE)
    await _assert_machine_ok(motion, "before reconnect")
    # Drop the channel; the SiLA server must tolerate the disconnect without
    # entering an error state, and a fresh channel must immediately read state.
    await channel.close()
    fresh = grpc.aio.insecure_channel(f"{run_context.device_id}:50051")
    try:
        motion2 = _FeatureClient(fresh, protobuf, _MOTION_PACKAGE, _MOTION_SERVICE)
        status = await motion2.property("Get_MachineStatus")
        assert isinstance(status, MachineStatus)
        assert status.is_error_state is False, f"reconnect observed error state: {status.message}"
    finally:
        await fresh.close()


async def test_volume_out_of_range_rejected(
    acceptance_manifest,
    sila_channel,
    run_context,
    http_client,
) -> None:
    """A volume beyond pipette capacity is rejected before actuation, machine stays clean."""
    manifest = _require_manifest(acceptance_manifest)
    channel, protobuf = sila_channel
    motion = _FeatureClient(channel, protobuf, _MOTION_PACKAGE, _MOTION_SERVICE)
    pipette = _FeatureClient(channel, protobuf, _PIPETTE_PACKAGE, _PIPETTE_SERVICE)
    liquid = _FeatureClient(channel, protobuf, _LIQUID_PACKAGE, _LIQUID_SERVICE)
    robot_mount, pipette_mount = await _preflight(motion, pipette)
    _ = robot_mount

    # Attached pipette capacity is authoritative; the manifest mount must be attached.
    attached = await pipette.observable("GetAttachedPipettes")
    selected = next(item for item in attached if item.mount is pipette_mount)
    assert selected.attached, f"pipette on {pipette_mount.value} is not attached"
    assert selected.max_volume < _IMPOSSIBLE_VOLUME, "test volume must exceed pipette capacity"

    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await liquid.observable(
            "TransferWithVerifiedLiquidClass",
            {
                "mount": pipette_mount,
                "source_well": _well(manifest.pipetting.source_well),
                "destination_well": _well(manifest.pipetting.destination_well),
                "volume": _IMPOSSIBLE_VOLUME,
                "liquid_class": VerifiedLiquidClass.WATER,
                "tiprack_uri": manifest.pipetting.tiprack_uri,
            },
            timeout_s=60.0,
        )
    details = _error_details(excinfo.value)
    assert b"LiquidVolumeOutOfRangeError" in details, f"unexpected error payload: {details[:300]}"
    # Fail-safe: the machine must not have entered an error state from a
    # rejected parameter, and no physical movement may have occurred.
    await _assert_machine_ok(motion, "after volume-out-of-range rejection")


async def test_unknown_labware_plan_rejected(
    acceptance_manifest,
    sila_channel,
    run_context,
    http_client,
) -> None:
    """An unprovisioned plan identifier is rejected and the deck ledger stays valid."""
    _require_manifest(acceptance_manifest)
    channel, protobuf = sila_channel
    motion = _FeatureClient(channel, protobuf, _MOTION_PACKAGE, _MOTION_SERVICE)
    pipette = _FeatureClient(channel, protobuf, _PIPETTE_PACKAGE, _PIPETTE_SERVICE)
    labware = _FeatureClient(channel, protobuf, _LABWARE_PACKAGE, _LABWARE_SERVICE)
    await _preflight(motion, pipette)

    plans = await labware.property("Get_AvailablePlans")
    assert isinstance(plans, list)
    known = {plan.plan_identifier for plan in plans}
    bogus = "UNITELABS_NO_SUCH_PLAN"
    assert bogus not in known, "test plan identifier must not be provisioned"

    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await labware.observable("MoveLabware", {"plan_identifier": bogus}, timeout_s=60.0)
    details = _error_details(excinfo.value)
    assert b"LabwareMovementNotAllowedError" in details, f"unexpected error payload: {details[:300]}"

    state = await labware.property("Get_DeckState")
    assert isinstance(state, LabwareDeckState)
    assert state.valid is True, "deck ledger must remain valid after a rejected plan"
    await _assert_machine_ok(motion, "after unknown-plan rejection")


async def test_tip_pickup_failure_fail_closed(
    acceptance_manifest,
    sila_channel,
    run_context,
    http_client,
) -> None:
    """Sensor-verified pickup from an EMPTY position fails closed and quarantines the mount.

    OPERATOR REQUIREMENT: before this test, remove the tip from the declared
    pickup position in the manifest. After this test the connector's
    recovery state requires an operator to reconcile the mount and restart the
    connector (see docs/real_flex_test_runbook.md). No further actuation tests
    may run in the same session.
    """
    manifest = _require_manifest(acceptance_manifest)
    channel, protobuf = sila_channel
    motion = _FeatureClient(channel, protobuf, _MOTION_PACKAGE, _MOTION_SERVICE)
    pipette = _FeatureClient(channel, protobuf, _PIPETTE_PACKAGE, _PIPETTE_SERVICE)
    tip = _FeatureClient(channel, protobuf, _TIP_PACKAGE, _TIP_SERVICE)
    _, pipette_mount = await _preflight(motion, pipette)

    p = manifest.pipetting
    presence = await tip.command("GetTipPresence", {"mount": pipette_mount})
    from unitelabs.opentrons_flex.features.tip_controller import TipPresence

    if presence is TipPresence.PRESENT:
        pytest.skip(
            "A tip is currently attached; the operator must remove the tip from the declared "
            "pickup position to run the induced pickup-failure test"
        )

    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await tip.observable(
            "PickUpTip",
            {
                "mount": pipette_mount,
                "location": TipLocation(p.tip_pickup.x, p.tip_pickup.y, p.tip_pickup.z),
                "tip_length": p.tip_length,
                "prep_after": False,
            },
            timeout_s=120.0,
        )
    details = _error_details(excinfo.value)
    assert b"TipPickupError" in details or b"UnexpectedTipAttachError" in details, (
        f"unexpected error payload: {details[:300]}"
    )
    # The machine itself must be safe (no E-stop, door closed) after the
    # failed pickup; the connector quarantines further liquid/tip operations
    # on this mount until operator reconciliation.
    await _assert_machine_ok(motion, "after induced tip-pickup failure")
