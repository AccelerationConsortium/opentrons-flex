"""Tests for create_app() feature-registration wiring (Flex).

The Flex connector builds a single OT3API and registers the core features
(motion, pipette, gripper, calibration) plus a feature per attached module. The
Magnetic Module is not supported, so it must never be registered.

Hardware boundary: the OT3API simulator is real (fast, no mocks); the Connector
is patched to capture registrations without starting a gRPC server.
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from unitelabs.opentrons_flex import OpentronsFlexConfig, create_app
from unitelabs.opentrons_flex.features import (
    AbsorbanceReaderFeature,
    CalibrationFeature,
    FlexStackerFeature,
    GripperFeature,
    MotionControlFeature,
    PipetteFeature,
    HeaterShakerFeature,
    TemperatureModuleFeature,
    ThermocyclerFeature,
)


@contextlib.asynccontextmanager
async def _run_app(config: OpentronsFlexConfig):
    """Run create_app() with a capturing Connector; yield the registered feature list."""
    registered: list = []
    mock_connector = MagicMock()
    mock_connector.register.side_effect = registered.append

    with patch("unitelabs.opentrons_flex.Connector", return_value=mock_connector):
        gen = create_app(config)
        await gen.__anext__()
        yield registered
        with contextlib.suppress(StopAsyncIteration):
            await gen.__anext__()


@pytest.mark.asyncio
async def test_simulate_registers_core_features():
    """Simulator mode registers exactly the four core features, in order."""
    config = OpentronsFlexConfig(use_simulator=True)
    async with _run_app(config) as registered:
        types = [type(f) for f in registered]
        assert types == [MotionControlFeature, PipetteFeature, GripperFeature, CalibrationFeature]


@pytest.mark.asyncio
async def test_no_magnetic_feature_registered():
    """The Flex has no Magnetic Module — no registered feature may reference it."""
    config = OpentronsFlexConfig(use_simulator=True)
    async with _run_app(config) as registered:
        assert not any("Magnetic" in type(f).__name__ for f in registered)


@pytest.mark.asyncio
async def test_bare_simulator_registers_no_module_features():
    """With no attached modules, only the core features register."""
    config = OpentronsFlexConfig(use_simulator=True)
    async with _run_app(config) as registered:
        assert len(registered) == 4


@pytest.mark.asyncio
async def test_attached_flex_modules_register_features():
    """Every supported Flex active module type registers a matching SiLA feature."""
    from opentrons.hardware_control.modules.types import ModuleType

    class _Module:
        def __init__(self, module_type):
            self.MODULE_TYPE = module_type

    fake_api = AsyncMock()
    fake_api.attached_modules = [
        _Module(ModuleType.ABSORBANCE_READER),
        _Module(ModuleType.FLEX_STACKER),
        _Module(ModuleType.HEATER_SHAKER),
        _Module(ModuleType.TEMPERATURE),
        _Module(ModuleType.THERMOCYCLER),
    ]

    with (
        patch(
            "opentrons.hardware_control.ot3api.OT3API.build_hardware_simulator",
            AsyncMock(return_value=fake_api),
        ),
        patch("unitelabs.opentrons_flex.Connector", return_value=MagicMock()) as mock_connector_cls,
    ):
        registered = []
        mock_connector_cls.return_value.register.side_effect = registered.append
        gen = create_app(OpentronsFlexConfig(use_simulator=True))
        await gen.__anext__()
        with contextlib.suppress(StopAsyncIteration):
            await gen.__anext__()

    types = [type(f) for f in registered]
    assert AbsorbanceReaderFeature in types
    assert FlexStackerFeature in types
    assert HeaterShakerFeature in types
    assert TemperatureModuleFeature in types
    assert ThermocyclerFeature in types


@pytest.mark.asyncio
async def test_shared_api_cleaned_up_on_shutdown():
    """create_app must clean up the shared OT3API when the generator is closed."""
    fake_api = AsyncMock()
    fake_api.attached_modules = []
    fake_api.attached_instruments = {}

    with (
        patch(
            "opentrons.hardware_control.ot3api.OT3API.build_hardware_simulator",
            AsyncMock(return_value=fake_api),
        ),
        patch("unitelabs.opentrons_flex.Connector", return_value=MagicMock()),
    ):
        gen = create_app(OpentronsFlexConfig(use_simulator=True))
        await gen.__anext__()
        with contextlib.suppress(StopAsyncIteration):
            await gen.__anext__()

    fake_api.clean_up.assert_awaited_once()
