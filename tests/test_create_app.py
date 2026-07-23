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
    FlexStackerMaintenanceFeature,
    GripperFeature,
    MotionControlFeature,
    PipetteFeature,
    HeaterShakerFeature,
    LabwareMovementController,
    LiquidHandlingController,
    TemperatureModuleFeature,
    ThermocyclerFeature,
    TipController,
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
    """Simulator mode registers every core feature in deterministic order."""
    config = OpentronsFlexConfig(use_simulator=True)
    async with _run_app(config) as registered:
        types = [type(f) for f in registered]
        assert types == [
            MotionControlFeature,
            LiquidHandlingController,
            LabwareMovementController,
            PipetteFeature,
            TipController,
            GripperFeature,
            CalibrationFeature,
        ]


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
        assert len(registered) == 7


@pytest.mark.asyncio
async def test_explicit_simulated_heater_shaker_registers_feature():
    """The opt-in OT3 module simulator registers a real Heater-Shaker feature."""
    config = OpentronsFlexConfig(use_simulator=True, simulated_heater_shaker=True)
    async with _run_app(config) as registered:
        heater_shakers = [feature for feature in registered if isinstance(feature, HeaterShakerFeature)]

    assert len(heater_shakers) == 1
    assert heater_shakers[0]._controller._module.device_info["serial"] == "HS-SIM-1"


@pytest.mark.asyncio
async def test_explicit_simulated_stacker_reader_and_temperature_register_features():
    """Opt-in Opentrons module simulators register all requested active accessories."""
    config = OpentronsFlexConfig(
        use_simulator=True,
        simulated_flex_stacker=True,
        simulated_absorbance_reader=True,
        simulated_temperature_module=True,
        simulated_thermocycler=True,
    )
    async with _run_app(config) as registered:
        stackers = [feature for feature in registered if isinstance(feature, FlexStackerFeature)]
        stacker_maintenance = [feature for feature in registered if isinstance(feature, FlexStackerMaintenanceFeature)]
        readers = [feature for feature in registered if isinstance(feature, AbsorbanceReaderFeature)]
        temperature_modules = [feature for feature in registered if isinstance(feature, TemperatureModuleFeature)]
        thermocyclers = [feature for feature in registered if isinstance(feature, ThermocyclerFeature)]

    assert len(stackers) == 1
    assert stackers[0]._controller.device_info.serial_number == "FS-SIM-1"
    assert len(stacker_maintenance) == 1
    assert stacker_maintenance[0]._controller is stackers[0]._controller
    assert len(readers) == 1
    assert readers[0]._controller.device_info.serial_number == "AR-SIM-1"
    assert len(temperature_modules) == 1
    assert temperature_modules[0]._controller.device_info.serial_number == "TM-SIM-1"
    assert temperature_modules[0]._controller.device_info.model
    assert len(thermocyclers) == 1
    assert thermocyclers[0]._controller._module.device_info["serial"] == "TC-SIM-1"
    assert thermocyclers[0]._controller._module.device_info["model"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setting",
    [
        "simulated_heater_shaker",
        "simulated_flex_stacker",
        "simulated_absorbance_reader",
        "simulated_temperature_module",
        "simulated_thermocycler",
        "simulated_gripper",
    ],
)
async def test_simulated_hardware_is_rejected_in_live_mode(setting: str):
    """A simulation setting must never substitute hardware in a live connector."""
    config = OpentronsFlexConfig(use_simulator=False, **{setting: True})
    gen = create_app(config)

    with pytest.raises(ValueError, match="require use_simulator=true"):
        await gen.__anext__()


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
    assert FlexStackerMaintenanceFeature in types
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


@pytest.mark.asyncio
async def test_runtime_preflight_fails_before_simulator_initialization():
    """A mismatched release must fail before even simulated hardware is built."""
    with (
        patch(
            "unitelabs.opentrons_flex.require_compatible_runtime",
            side_effect=RuntimeError("runtime preflight failed"),
        ),
        patch(
            "opentrons.hardware_control.ot3api.OT3API.build_hardware_simulator",
            new_callable=AsyncMock,
        ) as mock_build,
    ):
        gen = create_app(OpentronsFlexConfig(use_simulator=True))
        with pytest.raises(RuntimeError, match="runtime preflight failed"):
            await gen.__anext__()

    mock_build.assert_not_awaited()
