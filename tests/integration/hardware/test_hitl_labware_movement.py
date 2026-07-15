"""Guarded real-Flex round trip through locally allowlisted labware plans."""

import grpc.aio
import pytest

from unitelabs.opentrons_flex.features import LabwareMovementResult

from ..observable import call_observable

_PKG = "sila2.ca.accelerationconsortium.robots.labwaremovementcontroller.v1"
_SERVICE = f"{_PKG}.LabwareMovementController"
_MOTION_PKG = "sila2.ca.accelerationconsortium.robots.motioncontrolfeature.v2"
_MOTION_SERVICE = f"{_MOTION_PKG}.MotionControlFeature"

pytestmark = [pytest.mark.hardware_only, pytest.mark.gripper_labware_actuation]


async def _move(
    channel: grpc.aio.Channel,
    protobuf: object,
    plan_identifier: str,
) -> LabwareMovementResult:
    decoded = await call_observable(
        channel,
        protobuf,
        _SERVICE,
        _PKG,
        "MoveLabware",
        {"plan_identifier": plan_identifier},
        timeout_s=180.0,
    )
    result = next(iter(decoded.values()))
    assert isinstance(result, LabwareMovementResult)
    assert result.plan_identifier == plan_identifier
    return result


async def _assert_machine_ok(channel: grpc.aio.Channel, protobuf: object, after: str) -> None:
    stub = channel.unary_unary(f"/{_MOTION_SERVICE}/Get_MachineStatus")
    decoded = await protobuf.decode(f"{_MOTION_PKG}.Get_MachineStatus_Responses", await stub(b""))
    status = next(iter(decoded.values()))
    assert status.is_error_state is False, f"robot entered an error state {after}: {status.message}"


async def test_prepared_labware_gripper_round_trip(sila_channel, request: pytest.FixtureRequest) -> None:
    """Run server-provisioned outbound and return plans against an operator-prepared deck."""
    channel, protobuf = sila_channel
    outbound_plan = request.config.getoption("--gripper-outbound-plan")
    return_plan = request.config.getoption("--gripper-return-plan")
    if not outbound_plan or not return_plan:
        pytest.fail("--gripper-outbound-plan and --gripper-return-plan are required for gripper actuation")

    moved = await _move(channel, protobuf, outbound_plan)
    await _assert_machine_ok(channel, protobuf, "after outbound labware movement")
    try:
        returned = await _move(channel, protobuf, return_plan)
        assert returned.labware_identifier == moved.labware_identifier
        await _assert_machine_ok(channel, protobuf, "after return labware movement")
    except Exception:
        pytest.fail(
            "Outbound movement completed but the allowlisted return failed; inspect the deck and reconcile state."
        )
