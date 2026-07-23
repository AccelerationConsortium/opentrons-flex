"""Integration tests for create_app() with with_robot_server=True (Flex).

Exercises _create_app_with_robot_server() end-to-end with all hardware and
external server boundaries mocked. Catches import errors, wiring mistakes, and
shutdown regressions for the in-process robot-server mode.

Boundaries:
  robot_server  — stubbed in conftest.py (Opentrons-internal, not on PyPI)
  uvicorn       — real package (test dep); Server.serve mocked to avoid binding ports
  hardware      — mocked at OT3API.build_hardware_controller (CAN, no serial port)
"""

import asyncio
import contextlib
import dataclasses
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# These tests exercise the robot_server stub installed by tests/conftest.py.
# When the real robot_server package is installed the stub is absent and the
# mock-call assertions below would break, so skip the whole module.
_rs_app_mod = sys.modules.get("robot_server.app")
if _rs_app_mod is None or not isinstance(getattr(_rs_app_mod, "app", None), MagicMock):
    pytest.skip("real robot_server installed; stub-based tests skipped", allow_module_level=True)

from unitelabs.opentrons_flex import (
    OpentronsFlexConfig,
    _shared_hardware_robot_server_lifespan,
    create_app,
)
from unitelabs.opentrons_flex.features import (
    CalibrationFeature,
    GripperFeature,
    LabwareMovementController,
    LiquidHandlingController,
    MotionControlFeature,
    PipetteFeature,
    TipController,
)
from unitelabs.opentrons_flex.io import HardwareProxy

_CONFIG = OpentronsFlexConfig(use_simulator=False, with_robot_server=True, robot_server_uds="/run/aiohttp.sock")

# Where the deferred OT3API import resolves inside _create_app_with_robot_server.
_OT3API_BUILD = "opentrons.hardware_control.ot3api.OT3API.build_hardware_controller"


@pytest.fixture(autouse=True)
def _reset_robot_server_stubs():
    """Reset robot_server stub call counts between tests."""
    sys.modules["robot_server.hardware"]._hw_api_accessor.reset_mock()
    sys.modules["robot_server.hardware"]._init_task_accessor.reset_mock()
    sys.modules["robot_server.app"].app.reset_mock()
    sys.modules["robot_server.app"].app.dependency_overrides.clear()


def _make_api() -> AsyncMock:
    api = AsyncMock()
    api.attached_modules = []  # iterated by _register_modules
    return api


@contextlib.contextmanager
def _patches(mock_api, mock_connector, mock_uv_server):
    with (
        patch(_OT3API_BUILD, new_callable=AsyncMock, return_value=mock_api),
        patch("unitelabs.opentrons_flex.FlexMotionController.from_api", return_value=MagicMock()),
        patch("unitelabs.opentrons_flex.FlexGripperController.from_api", return_value=MagicMock()),
        patch("unitelabs.opentrons_flex.FlexCalibrationController.from_api", return_value=MagicMock()),
        patch("unitelabs.opentrons_flex.FlexLiquidHandlingController", return_value=MagicMock()),
        patch("unitelabs.opentrons_flex.FlexLabwareMovementController", return_value=MagicMock()),
        patch("unitelabs.opentrons_flex.Connector", return_value=mock_connector),
        patch("uvicorn.Server", return_value=mock_uv_server),
        patch("uvicorn.Config"),
    ):
        yield


@contextlib.asynccontextmanager
async def _run(config=_CONFIG):
    """Run create_app(with_robot_server=True) with all external deps mocked."""
    mock_api = _make_api()
    mock_uv_server = MagicMock()
    mock_uv_server.serve = AsyncMock()
    mock_connector = MagicMock()
    registered: list = []
    mock_connector.register.side_effect = registered.append

    with _patches(mock_api, mock_connector, mock_uv_server):
        gen = create_app(config)
        await gen.__anext__()
        await asyncio.sleep(0)  # let the uvicorn task be scheduled

        class _Result:
            pass

        result = _Result()
        result.api = mock_api
        result.registered = registered
        result.uv_server = mock_uv_server
        yield result

        with contextlib.suppress(StopAsyncIteration):
            await gen.__anext__()


# ── import coverage ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_import_errors():
    """All deferred imports in _create_app_with_robot_server resolve without error."""
    async with _run():
        pass


# ── hardware init ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hardware_built_via_ot3api():
    """OT3API.build_hardware_controller is awaited once (no serial port — CAN bus)."""
    mock_api = _make_api()
    with (
        patch(_OT3API_BUILD, new_callable=AsyncMock, return_value=mock_api) as mock_build,
        patch("unitelabs.opentrons_flex.FlexMotionController.from_api", return_value=MagicMock()),
        patch("unitelabs.opentrons_flex.FlexGripperController.from_api", return_value=MagicMock()),
        patch("unitelabs.opentrons_flex.FlexCalibrationController.from_api", return_value=MagicMock()),
        patch("unitelabs.opentrons_flex.FlexLiquidHandlingController", return_value=MagicMock()),
        patch("unitelabs.opentrons_flex.FlexLabwareMovementController", return_value=MagicMock()),
        patch("unitelabs.opentrons_flex.Connector", return_value=MagicMock()),
        patch("uvicorn.Server", return_value=MagicMock(serve=AsyncMock())),
        patch("uvicorn.Config"),
    ):
        gen = create_app(_CONFIG)
        await gen.__anext__()
        mock_build.assert_awaited_once_with()
        with contextlib.suppress(StopAsyncIteration):
            await gen.__anext__()


@pytest.mark.asyncio
async def test_required_mutation_configuration_fails_before_hardware(monkeypatch, tmp_path):
    """Full-workflow mode cannot silently start without its credential."""
    config = dataclasses.replace(
        _CONFIG,
        run_mutation_ledger_path=str(tmp_path / "mutations.jsonl"),
        run_mutation_required=True,
    )
    monkeypatch.delenv(config.run_mutation_token_env, raising=False)
    monkeypatch.delenv(config.run_mutation_actor_env, raising=False)
    mock_build = AsyncMock()

    with patch(_OT3API_BUILD, mock_build):
        gen = create_app(config)
        with pytest.raises(RuntimeError, match="Controlled run mutation preflight failed"):
            await gen.__anext__()

    mock_build.assert_not_awaited()


# ── app.state pre-population ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_app_state_receives_init_task():
    """_init_task_accessor.set_on is called once with a completed asyncio.Task."""
    rs_hw = sys.modules["robot_server.hardware"]
    async with _run():
        rs_hw._init_task_accessor.set_on.assert_called_once()
        _, task_arg = rs_hw._init_task_accessor.set_on.call_args[0]
        assert isinstance(task_arg, asyncio.Task)
        assert task_arg.done()


@pytest.mark.asyncio
async def test_app_state_receives_hardware_proxy():
    """_hw_api_accessor.set_on is called once with a HardwareProxy instance."""
    rs_hw = sys.modules["robot_server.hardware"]
    async with _run():
        rs_hw._hw_api_accessor.set_on.assert_called_once()
        _, proxy_arg = rs_hw._hw_api_accessor.set_on.call_args[0]
        assert isinstance(proxy_arg, HardwareProxy)


@pytest.mark.asyncio
async def test_robot_server_identity_matches_injected_flex_hardware():
    """Embedded routes must identify as Flex even when the host defaults to OT-2."""
    rs_hw = sys.modules["robot_server.hardware"]
    rs_app = sys.modules["robot_server.app"].app

    async with _run():
        assert await rs_app.dependency_overrides[rs_hw.get_robot_type]() == "OT-3 Standard"

        from opentrons.protocol_engine import DeckType
        from opentrons_shared_data.robot.types import RobotTypeEnum

        assert await rs_app.dependency_overrides[rs_hw.get_robot_type_enum]() is RobotTypeEnum.FLEX
        assert await rs_app.dependency_overrides[rs_hw.get_deck_type]() is DeckType.OT3_STANDARD

    assert rs_app.dependency_overrides == {}


@pytest.mark.asyncio
async def test_shared_hardware_lifespan_runs_protocol_engine_callbacks():
    """Shared hardware still initializes the native Protocol Engine dependencies."""
    events: list[str] = []

    @contextlib.asynccontextmanager
    async def original_lifespan(_app):
        events.append("native-started")
        yield
        events.append("native-stopped")

    start_light_control = AsyncMock(side_effect=lambda *_: events.append("light-started"))
    mark_startup_finished = AsyncMock(side_effect=lambda *_: events.append("light-ready"))
    dependencies = MagicMock(
        start_light_control_task=start_light_control,
        mark_light_control_startup_finished=mark_startup_finished,
    )
    app = MagicMock()
    proxy = MagicMock(spec=HardwareProxy)

    with patch.dict(sys.modules, {"robot_server.runs.dependencies": dependencies}):
        lifespan = _shared_hardware_robot_server_lifespan(original_lifespan, proxy)
        async with lifespan(app):
            events.append("serving")

    assert events == ["native-started", "light-started", "light-ready", "serving", "native-stopped"]
    start_light_control.assert_awaited_once_with(app.state, proxy)
    mark_startup_finished.assert_awaited_once_with(app.state, proxy)


# ── uvicorn startup ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uvicorn_configured_on_unix_socket():
    """uvicorn.Config is constructed with the configured robot_server_uds socket path."""
    mock_api = _make_api()
    with (
        patch(_OT3API_BUILD, new_callable=AsyncMock, return_value=mock_api),
        patch("unitelabs.opentrons_flex.FlexMotionController.from_api", return_value=MagicMock()),
        patch("unitelabs.opentrons_flex.FlexGripperController.from_api", return_value=MagicMock()),
        patch("unitelabs.opentrons_flex.FlexCalibrationController.from_api", return_value=MagicMock()),
        patch("unitelabs.opentrons_flex.FlexLiquidHandlingController", return_value=MagicMock()),
        patch("unitelabs.opentrons_flex.FlexLabwareMovementController", return_value=MagicMock()),
        patch("unitelabs.opentrons_flex.Connector", return_value=MagicMock()),
        patch("uvicorn.Server", return_value=MagicMock(serve=AsyncMock())),
        patch("uvicorn.Config") as mock_cfg,
    ):
        gen = create_app(_CONFIG)
        await gen.__anext__()
        _, kwargs = mock_cfg.call_args
        assert kwargs["uds"] == _CONFIG.robot_server_uds
        with contextlib.suppress(StopAsyncIteration):
            await gen.__anext__()


@pytest.mark.asyncio
async def test_uvicorn_serve_task_started():
    """uvicorn.Server.serve is called to create the background task."""
    async with _run() as r:
        r.uv_server.serve.assert_called_once()


# ── feature registration ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_core_features_registered():
    """All core motion, liquid, labware, instrument, and calibration features register."""
    async with _run() as r:
        types_ = [type(f) for f in r.registered]
        assert MotionControlFeature in types_
        assert LiquidHandlingController in types_
        assert LabwareMovementController in types_
        assert PipetteFeature in types_
        assert TipController in types_
        assert GripperFeature in types_
        assert CalibrationFeature in types_


# ── shutdown ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_stops_uvicorn():
    """uv_server.should_exit is set to True on shutdown."""
    async with _run() as r:
        pass  # context exit triggers shutdown
    assert r.uv_server.should_exit is True


@pytest.mark.asyncio
async def test_shutdown_disconnects_hardware():
    """The shared OT3API clean_up is awaited on shutdown."""
    async with _run() as r:
        pass
    r.api.clean_up.assert_awaited_once()
