"""Tests for the lock-free status snapshot, its SiLA feature, and observable FAILED progress."""

import asyncio
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from opentrons.hardware_control.ot3api import OT3API
from sila.framework.fdl import Serializer
from unitelabs.cdk import Connector, SiLAServerConfig

from unitelabs.opentrons_flex import OpentronsFlexConfig

from unitelabs.opentrons_flex.features import RobotStatusProvider, TipController
from unitelabs.opentrons_flex.features._progress import (
    OperationPhase,
    OperationProgress,
    run_observable,
)
from unitelabs.opentrons_flex.features.robot_status import OverallState, to_robot_status
from unitelabs.opentrons_flex.io import FlexMotionController
from unitelabs.opentrons_flex.io.robot_status import HttpServerState, RobotState, RobotStatusSource


@pytest_asyncio.fixture
async def motion() -> FlexMotionController:
    api = await OT3API.build_hardware_simulator()
    await api.home()
    controller = FlexMotionController.from_api(api, lock=asyncio.Lock())
    yield controller
    await api.clean_up()


async def test_snapshot_is_ready_for_an_idle_simulator(motion: FlexMotionController) -> None:
    snapshot = RobotStatusSource(motion).snapshot()

    assert snapshot.state is RobotState.READY
    assert snapshot.simulating
    assert snapshot.recovery_guidance == ""
    assert snapshot.http_server is HttpServerState.NOT_CONFIGURED


async def test_snapshot_priorities(motion: FlexMotionController) -> None:
    connected = {"value": True}
    run = {"value": (None, "idle", False)}
    deck = {"valid": True}
    source = RobotStatusSource(
        motion,
        run_state=lambda: run["value"],
        deck_state=lambda: deck["valid"],
        registered_modules=[("HEATER_SHAKER", "HS-1", lambda: connected["value"])],
        attached_module_ids=lambda: [("HEATER_SHAKER", "HS-1"), ("TEMPERATURE", "TM-9")],
        http_server_state=lambda: HttpServerState.RUNNING,
    )

    snapshot = source.snapshot()
    assert snapshot.state is RobotState.READY
    assert snapshot.unregistered_modules == ("TEMPERATURE TM-9",)

    await motion._lock._lock.acquire()
    try:
        assert source.snapshot().state is RobotState.BUSY
    finally:
        motion._lock._lock.release()

    run["value"] = ("run-1", "running", True)
    assert source.snapshot().state is RobotState.PROTOCOL_RUN_ACTIVE
    assert source.snapshot().run.run_id == "run-1"

    deck["valid"] = False
    assert source.snapshot().state is RobotState.RECOVERY_REQUIRED

    connected["value"] = False
    assert source.snapshot().state is RobotState.HARDWARE_ERROR


async def test_unreadable_run_owner_is_reported_fail_closed(motion: FlexMotionController) -> None:
    def broken() -> tuple[str | None, str, bool]:
        raise RuntimeError("store unavailable")

    snapshot = RobotStatusSource(motion, run_state=broken).snapshot()

    assert snapshot.run.status == "unknown"
    assert not snapshot.run.direct_control_allowed


async def test_halt_is_reported_as_recovery_required_with_guidance(motion: FlexMotionController) -> None:
    await motion.stop()
    status = to_robot_status(RobotStatusSource(motion).snapshot())

    assert status.state is OverallState.RECOVERY_REQUIRED
    assert status.rehome_required
    assert "re-home" in status.recovery_guidance


async def test_status_provider_and_reconcile_command_generate_valid_definitions(
    motion: FlexMotionController,
) -> None:
    connector = Connector(
        OpentronsFlexConfig(
            sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0),
            cloud_server_endpoint=None,
            discovery=None,
        )
    )
    status_feature = RobotStatusProvider(RobotStatusSource(motion))
    tip_feature = TipController(motion)
    connector.register(status_feature)
    connector.register(tip_feature)

    status_fdl = Serializer.serialize(status_feature.serialize)
    tip_fdl = Serializer.serialize(tip_feature.serialize)

    assert "<Identifier>RobotStatusProvider</Identifier>" in status_fdl
    assert "<Identifier>Status</Identifier>" in status_fdl
    assert "<Observable>Yes</Observable>" in status_fdl
    assert "<Value>recovery required</Value>" in status_fdl
    assert "<Identifier>ReconcileTipState</Identifier>" in tip_fdl
    assert "<Identifier>HardwareBusyError</Identifier>" in tip_fdl


async def test_run_observable_reports_failed_phase_before_reraising() -> None:
    status = MagicMock()
    intermediate = MagicMock()

    async def action() -> None:
        raise RuntimeError("probe not found")

    with pytest.raises(RuntimeError):
        await run_observable(status, intermediate, "start", "done", "cancelled", action())

    sent = [call.args[0] for call in intermediate.send.call_args_list]
    assert [update.phase for update in sent] == [OperationPhase.STARTING, OperationPhase.FAILED]
    assert isinstance(sent[-1], OperationProgress)
    assert "probe not found" in sent[-1].message
