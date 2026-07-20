"""Transport-level tests for the Flex Stacker controller."""

from types import SimpleNamespace

import pytest
from opentrons.drivers.flex_stacker.types import Direction, LEDColor, LEDPattern, StackerAxis

from unitelabs.opentrons_flex.io import (
    FlexStackerAxis,
    FlexStackerController,
    FlexStackerDirection,
    FlexStackerLedColor,
    FlexStackerLedPattern,
    InvalidStackerConfigurationError,
    ModuleOperationError,
    StackerMovementOutOfRangeError,
    StackerNotReadyError,
)


class _StackerModule:
    def __init__(self) -> None:
        self.status = SimpleNamespace(value="idle")
        self.latch_state = SimpleNamespace(value="closed")
        self.platform_state = SimpleNamespace(value="retracted")
        self.hopper_door_state = SimpleNamespace(value="closed")
        self.install_detected = True
        self.initialized = True
        self.limit_switch_status = {
            StackerAxis.X: SimpleNamespace(value="extended"),
            StackerAxis.Z: SimpleNamespace(value="retracted"),
            StackerAxis.L: SimpleNamespace(value="retracted"),
        }
        self.live_data = {"status": "idle", "data": {"errorDetails": None}}
        self.device_info = {"serial": "FS-1", "model": "flexStackerModuleV1", "version": "1.2.3"}
        self.calls: list[tuple] = []
        self.move_success = True

    async def home_all(self, ignore_latch: bool) -> None:
        self.calls.append(("home_all", ignore_latch))

    async def home_axis(self, axis: StackerAxis, direction: Direction) -> bool:
        self.calls.append(("home_axis", axis, direction))
        return self.move_success

    async def move_axis(self, axis: StackerAxis, direction: Direction, distance: float) -> bool:
        self.calls.append(("move_axis", axis, direction, distance))
        return self.move_success

    async def open_latch(self) -> bool:
        self.calls.append(("open_latch",))
        return self.move_success

    async def close_latch(self) -> bool:
        self.calls.append(("close_latch",))
        return self.move_success

    async def dispense_labware(self, **kwargs: object) -> None:
        self.calls.append(("dispense_labware", kwargs))

    async def store_labware(self, **kwargs: object) -> None:
        self.calls.append(("store_labware", kwargs))

    async def set_led_state(self, **kwargs: object) -> None:
        self.calls.append(("set_led_state", kwargs))

    async def deactivate(self) -> None:
        self.calls.append(("deactivate",))


@pytest.mark.asyncio
async def test_retrieve_store_and_led_forward_workflow_parameters() -> None:
    module = _StackerModule()
    controller = FlexStackerController.from_module(module)

    await controller.retrieve_labware(14.4, True, True)
    await controller.store_labware(14.4, True)
    await controller.set_led(0.5, FlexStackerLedColor.GREEN, FlexStackerLedPattern.FLASH, 0.25, 3)

    assert (
        "dispense_labware",
        {
            "labware_height": 14.4,
            "enforce_hopper_lw_sensing": True,
            "enforce_shuttle_lw_sensing": True,
        },
    ) in module.calls
    assert (
        "store_labware",
        {"labware_height": 14.4, "enforce_shuttle_lw_sensing": True},
    ) in module.calls
    assert (
        "set_led_state",
        {
            "power": 0.5,
            "color": LEDColor.GREEN,
            "pattern": LEDPattern.FLASH,
            "duration": 250,
            "reps": 3,
        },
    ) in module.calls


@pytest.mark.asyncio
async def test_false_motion_result_becomes_defined_error() -> None:
    module = _StackerModule()
    module.move_success = False
    controller = FlexStackerController.from_module(module)

    with pytest.raises(ModuleOperationError, match="did not complete"):
        await controller.move_axis(FlexStackerAxis.X, FlexStackerDirection.EXTEND, 10.0)

    with pytest.raises(StackerNotReadyError, match="HomeAll"):
        await controller.retrieve_labware(14.4, True, True)

    module.move_success = True
    await controller.home_all(ignore_latch=False)
    await controller.retrieve_labware(14.4, True, True)


@pytest.mark.asyncio
async def test_cancellation_halt_requires_full_home_before_labware_motion() -> None:
    module = _StackerModule()
    controller = FlexStackerController.from_module(module)

    await controller.halt_for_cancellation()
    assert controller.state.recovery_required is True
    with pytest.raises(StackerNotReadyError, match="HomeAll"):
        await controller.store_labware(14.4, True)

    await controller.home_all(ignore_latch=True)
    with pytest.raises(StackerNotReadyError, match="HomeAll"):
        await controller.store_labware(14.4, True)

    await controller.home_all(ignore_latch=False)
    assert controller.state.recovery_required is False
    await controller.store_labware(14.4, True)


@pytest.mark.asyncio
async def test_explicit_simulator_uses_successful_full_home_as_recovery_authority() -> None:
    module = _StackerModule()
    module.is_simulated = True
    module.platform_state = SimpleNamespace(value="unknown")
    module.limit_switch_status = {axis: SimpleNamespace(value="unknown") for axis in StackerAxis}
    controller = FlexStackerController.from_module(module)

    await controller.halt_for_cancellation()
    assert controller.state.recovery_required is True

    await controller.home_all(ignore_latch=False)
    assert controller.state.recovery_required is False
    await controller.retrieve_labware(14.4, False, False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "axis,distance,maximum",
    [
        (FlexStackerAxis.X, 194.1, 194.0),
        (FlexStackerAxis.Z, 139.6, 139.5),
        (FlexStackerAxis.LATCH, 22.1, 22.0),
    ],
)
async def test_axis_specific_travel_limits_are_defined_errors(
    axis: FlexStackerAxis,
    distance: float,
    maximum: float,
) -> None:
    controller = FlexStackerController.from_module(_StackerModule())

    with pytest.raises(StackerMovementOutOfRangeError, match=str(maximum)):
        await controller.move_axis(axis, FlexStackerDirection.EXTEND, distance)


@pytest.mark.asyncio
@pytest.mark.parametrize("height", [3.9, 102.6, float("nan")])
async def test_invalid_labware_height_is_a_defined_error(height: float) -> None:
    controller = FlexStackerController.from_module(_StackerModule())

    with pytest.raises(InvalidStackerConfigurationError, match="Labware height"):
        await controller.retrieve_labware(height, True, True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "power,duration,repetitions",
    [
        (1.1, 1.0, 1),
        (0.5, 0.01, 1),
        (0.5, 10.1, 1),
        (0.5, 1.0, 11),
    ],
)
async def test_invalid_led_settings_are_defined_errors(
    power: float,
    duration: float,
    repetitions: int,
) -> None:
    controller = FlexStackerController.from_module(_StackerModule())

    with pytest.raises(InvalidStackerConfigurationError):
        await controller.set_led(
            power,
            FlexStackerLedColor.GREEN,
            FlexStackerLedPattern.FLASH,
            duration,
            repetitions,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("attribute,value", [("install_detected", False), ("initialized", False)])
async def test_labware_motion_requires_ready_stacker(attribute: str, value: bool) -> None:
    module = _StackerModule()
    setattr(module, attribute, value)
    controller = FlexStackerController.from_module(module)

    with pytest.raises(StackerNotReadyError):
        await controller.retrieve_labware(14.4, True, True)


@pytest.mark.asyncio
async def test_labware_motion_requires_closed_hopper_door() -> None:
    module = _StackerModule()
    module.hopper_door_state.value = "opened"
    controller = FlexStackerController.from_module(module)

    with pytest.raises(StackerNotReadyError, match="hopper door"):
        await controller.store_labware(14.4, True)


def test_state_limit_switches_and_identity_are_structured() -> None:
    controller = FlexStackerController.from_module(_StackerModule())

    assert controller.state.initialized is True
    assert controller.limit_switches.x == "extended"
    assert controller.device_info.serial_number == "FS-1"
