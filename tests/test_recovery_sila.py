"""RecoveryController and the extended RobotStatusProvider over a real SiLA server (simulator)."""

import contextlib

from unitelabs.cdk import SiLAServerConfig

from unitelabs.opentrons_flex import OpentronsFlexConfig, create_app
from unitelabs.opentrons_flex.client import FlexClient, SiLACallError, enum_name, field


@contextlib.asynccontextmanager
async def _simulator(**overrides: object):
    config = OpentronsFlexConfig(
        use_simulator=True,
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
        **overrides,
    )
    generator = create_app(config)
    connector = await generator.__anext__()
    await connector.start()
    try:
        async with FlexClient.connect(connector.sila_server._address) as client:
            yield client
    finally:
        await connector.stop()
        with contextlib.suppress(StopAsyncIteration):
            await generator.__anext__()


async def _status(client: FlexClient) -> object:
    return await client.subscribe_first("RobotStatusProvider", "Status")


async def test_emergency_stop_then_recover_returns_to_ready() -> None:
    async with _simulator(auto_recovery="off") as client:
        await client.run("MotionController", "Home", timeout_s=60.0)
        await client.run("MotionController", "EmergencyStop")

        status = await _status(client)
        assert enum_name(field(status, "state")) == "RECOVERY_REQUIRED"
        assert "EmergencyStop was requested" in field(status, "state_reason")
        pending = await client.subscribe_first("RecoveryController", "PendingRecovery")
        assert list(field(pending, "automatic_steps")) == ["fully home the robot"]

        result = await client.run("RecoveryController", "Recover", timeout_s=60.0)
        assert list(next(iter(result.values()))) == ["fully home the robot"]

        status = await _status(client)
        assert enum_name(field(status, "state")) == "READY"
        assert field(status, "state_reason") == ""
        events = await client.subscribe_first("RobotStatusProvider", "RecentEvents")
        categories = [enum_name(field(event, "category")) for event in events]
        assert "HALT" in categories
        assert "RECOVERY" in categories
        assert any("RECOVERY_REQUIRED -> READY" in field(event, "message") for event in events)


async def test_failed_command_is_reported_as_last_error() -> None:
    from unitelabs.opentrons_flex.features.motion_control import Mount

    async with _simulator(auto_recovery="off") as client:
        await client.run("MotionController", "EmergencyStop")
        params = {"mount": Mount.LEFT, "delta_x": 1.0, "delta_y": 0.0, "delta_z": 0.0, "speed": 10.0}
        try:
            await client.run("MotionController", "MoveRelative", params)
        except SiLACallError as exc:
            assert exc.identifier == "RecoveryRequiredError"
        status = await _status(client)
        assert "RecoveryRequiredError" in field(status, "last_error")


async def test_acknowledging_without_an_estop_is_harmless() -> None:
    async with _simulator() as client:
        response = await client.call("RecoveryController", "AcknowledgeEmergencyStop")
        assert enum_name(next(iter(response.values()))) in {"DISENGAGED", "NOT_PRESENT"}
