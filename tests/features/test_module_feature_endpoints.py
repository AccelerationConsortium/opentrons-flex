"""Direct endpoint tests for module SiLA features.

These use lightweight fake controllers rather than hardware simulators. The goal
is endpoint coverage: every public module feature command should call the
expected controller method and return structured data rather than a boolean
success sentinel.
"""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from unitelabs.opentrons_flex.features.absorbance_reader import (
    AbsorbanceReaderFeature,
    MeasurementMode,
    ReaderStatus,
)
from unitelabs.opentrons_flex.features.flex_stacker import (
    AxisState,
    FlexStackerFeature,
    HopperDoorState,
    LatchState,
    PlatformState,
    StackerAxisName,
    StackerDirection,
    StackerLedColor,
    StackerLedPattern,
    StackerStatus,
)
from unitelabs.opentrons_flex.features.heater_shaker import HeaterShakerFeature, HeaterShakerStatus, LatchStatus
from unitelabs.opentrons_flex.features.temperature import TemperatureModuleFeature
from unitelabs.opentrons_flex.features.thermocycler import (
    LidStatus,
    ThermocyclerFeature,
    ThermocyclerProfileStep,
    ThermocyclerStatus,
)
from unitelabs.opentrons_flex.io import (
    AbsorbanceMeasurement,
    AbsorbanceMeasurementRow,
    AbsorbanceReaderState,
    DeviceInfo,
    FlexStackerLimitSwitches,
    FlexStackerState,
    RPM,
    Temperature,
)


class _Status:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def update(self, **kwargs) -> None:
        self.updates.append(kwargs)


class _Intermediate:
    def __init__(self) -> None:
        self.messages: list[object] = []

    def send(self, message: object) -> None:
        self.messages.append(message)


def _obs() -> tuple[_Status, _Intermediate]:
    return _Status(), _Intermediate()


@dataclass
class _BaseController:
    calls: list[tuple]

    async def is_connected(self) -> bool:
        self.calls.append(("is_connected",))
        return True

    async def get_device_info(self) -> DeviceInfo:
        self.calls.append(("get_device_info",))
        return DeviceInfo(serial_number="SN123", model="module", firmware_version="1.2.3")


class _TemperatureController(_BaseController):
    async def set_temperature(self, temperature: float) -> None:
        self.calls.append(("set_temperature", temperature))

    async def wait_for_temperature(self, temperature: float) -> None:
        self.calls.append(("wait_for_temperature", temperature))

    async def get_temperature(self) -> Temperature:
        self.calls.append(("get_temperature",))
        return Temperature(current=24.5, target=37.0)

    async def deactivate(self) -> None:
        self.calls.append(("deactivate",))


class _HeaterShakerController(_BaseController):
    async def set_temperature(self, temperature: float) -> None:
        self.calls.append(("set_temperature", temperature))

    async def wait_for_temperature(self, temperature: float) -> None:
        self.calls.append(("wait_for_temperature", temperature))

    async def get_temperature(self) -> Temperature:
        self.calls.append(("get_temperature",))
        return Temperature(current=25.0, target=42.0)

    async def deactivate_heater(self) -> None:
        self.calls.append(("deactivate_heater",))

    async def set_rpm(self, rpm: int) -> None:
        self.calls.append(("set_rpm", rpm))

    async def get_rpm(self) -> RPM:
        self.calls.append(("get_rpm",))
        return RPM(current=500, target=1000)

    async def stop_shaking(self) -> None:
        self.calls.append(("stop_shaking",))

    async def open_latch(self) -> None:
        self.calls.append(("open_latch",))

    async def close_latch(self) -> None:
        self.calls.append(("close_latch",))

    async def get_latch_status(self) -> SimpleNamespace:
        self.calls.append(("get_latch_status",))
        return SimpleNamespace(value="idle_open")


class _ThermocyclerController(_BaseController):
    async def open_lid(self) -> None:
        self.calls.append(("open_lid",))

    async def close_lid(self) -> None:
        self.calls.append(("close_lid",))

    async def get_lid_status(self) -> str:
        self.calls.append(("get_lid_status",))
        return "open"

    async def set_lid_temperature(self, temperature: float) -> None:
        self.calls.append(("set_lid_temperature", temperature))

    async def wait_for_lid_temperature(self) -> None:
        self.calls.append(("wait_for_lid_temperature",))

    async def get_lid_temperature(self) -> Temperature:
        self.calls.append(("get_lid_temperature",))
        return Temperature(current=45.0, target=95.0)

    async def set_plate_temperature(
        self,
        temperature: float,
        hold_time: float | None,
        volume: float | None,
        ramp_rate: float | None,
    ) -> None:
        self.calls.append(("set_plate_temperature", temperature, hold_time, volume, ramp_rate))

    async def wait_for_plate_temperature(self) -> None:
        self.calls.append(("wait_for_plate_temperature",))

    async def get_plate_temperature(self) -> Temperature:
        self.calls.append(("get_plate_temperature",))
        return Temperature(current=25.0, target=60.0)

    async def execute_profile(self, steps: list[dict], repetitions: int, volume: float | None) -> None:
        self.calls.append(("execute_profile", steps, repetitions, volume))

    async def deactivate_lid(self) -> None:
        self.calls.append(("deactivate_lid",))

    async def deactivate_block(self) -> None:
        self.calls.append(("deactivate_block",))

    async def deactivate_all(self) -> None:
        self.calls.append(("deactivate_all",))


class _AbsorbanceController(_BaseController):
    async def configure_measurement(
        self,
        mode: object,
        wavelengths: list[int],
        reference_wavelength: int | None,
    ) -> None:
        self.calls.append(("configure_measurement", mode.name, wavelengths, reference_wavelength))

    async def start_measure(self) -> AbsorbanceMeasurement:
        self.calls.append(("start_measure",))
        return AbsorbanceMeasurement(rows=[AbsorbanceMeasurementRow(values=[0.1, 0.2])])

    async def deactivate(self) -> None:
        self.calls.append(("deactivate",))

    async def get_state(self) -> AbsorbanceReaderState:
        self.calls.append(("get_state",))
        return AbsorbanceReaderState(
            status="idle",
            lid_status="on",
            plate_presence="present",
            supported_wavelengths=[450, 600],
            measurement_mode="single",
            sample_wavelengths=[450],
            reference_wavelength=None,
        )


class _FlexStackerController(_BaseController):
    async def home_all(self, ignore_latch: bool) -> None:
        self.calls.append(("home_all", ignore_latch))

    async def home_axis(self, axis: object, direction: object) -> bool:
        self.calls.append(("home_axis", axis.name, direction.name))
        return True

    async def move_axis(self, axis: object, direction: object, distance: float) -> bool:
        self.calls.append(("move_axis", axis.name, direction.name, distance))
        return True

    async def open_latch(self) -> None:
        self.calls.append(("open_latch",))

    async def close_latch(self) -> None:
        self.calls.append(("close_latch",))

    async def dispense_labware(
        self,
        labware_height: float,
        enforce_hopper_labware_sensing: bool,
        enforce_shuttle_labware_sensing: bool,
    ) -> None:
        self.calls.append(
            (
                "dispense_labware",
                labware_height,
                enforce_hopper_labware_sensing,
                enforce_shuttle_labware_sensing,
            )
        )

    async def store_labware(self, labware_height: float, enforce_shuttle_labware_sensing: bool) -> None:
        self.calls.append(("store_labware", labware_height, enforce_shuttle_labware_sensing))

    async def set_led(
        self,
        power: float,
        color: object,
        pattern: object,
        duration_ms: int,
        repetitions: int,
    ) -> None:
        self.calls.append(("set_led", power, color.name, pattern.name, duration_ms, repetitions))

    async def deactivate(self) -> None:
        self.calls.append(("deactivate",))

    async def get_limit_switches(self) -> FlexStackerLimitSwitches:
        self.calls.append(("get_limit_switches",))
        return FlexStackerLimitSwitches(x="extended", z="retracted", latch="unknown")

    async def get_state(self) -> FlexStackerState:
        self.calls.append(("get_state",))
        return FlexStackerState(
            status="idle",
            latch_state="closed",
            platform_state="retracted",
            hopper_door_state="opened",
            install_detected=True,
            initialized=True,
            error_details="",
        )


@pytest.mark.asyncio
async def test_temperature_module_feature_endpoints() -> None:
    controller = _TemperatureController(calls=[])
    feature = TemperatureModuleFeature(controller)

    status, intermediate = _obs()
    assert await feature.set_temperature(37.0, status=status, intermediate=intermediate) == Temperature(24.5, 37.0)
    status, intermediate = _obs()
    assert await feature.wait_for_temperature(37.0, status=status, intermediate=intermediate) == Temperature(24.5, 37.0)
    status, intermediate = _obs()
    assert await feature.get_temperature(status=status, intermediate=intermediate) == Temperature(24.5, 37.0)
    status, intermediate = _obs()
    assert await feature.deactivate(status=status, intermediate=intermediate) == Temperature(24.5, 37.0)
    status, intermediate = _obs()
    assert (await feature.get_device_info(status=status, intermediate=intermediate)).serial_number == "SN123"

    assert ("set_temperature", 37.0) in controller.calls
    assert ("wait_for_temperature", 37.0) in controller.calls
    assert ("deactivate",) in controller.calls


@pytest.mark.asyncio
async def test_heater_shaker_feature_endpoints() -> None:
    controller = _HeaterShakerController(calls=[])
    feature = HeaterShakerFeature(controller)

    status, intermediate = _obs()
    assert await feature.set_temperature(42.0, status=status, intermediate=intermediate) == Temperature(25.0, 42.0)
    status, intermediate = _obs()
    assert await feature.wait_for_temperature(42.0, status=status, intermediate=intermediate) == Temperature(25.0, 42.0)
    status, intermediate = _obs()
    assert await feature.get_temperature(status=status, intermediate=intermediate) == Temperature(25.0, 42.0)
    status, intermediate = _obs()
    assert await feature.deactivate_heater(status=status, intermediate=intermediate) == Temperature(25.0, 42.0)
    status, intermediate = _obs()
    assert await feature.set_rpm(1000, status=status, intermediate=intermediate) == RPM(500, 1000)
    status, intermediate = _obs()
    assert await feature.get_rpm(status=status, intermediate=intermediate) == RPM(500, 1000)
    status, intermediate = _obs()
    assert await feature.stop_shaking(status=status, intermediate=intermediate) == RPM(500, 1000)
    status, intermediate = _obs()
    assert await feature.open_latch(status=status, intermediate=intermediate) is LatchStatus.IDLE_OPEN
    status, intermediate = _obs()
    assert await feature.close_latch(status=status, intermediate=intermediate) is LatchStatus.IDLE_OPEN
    status, intermediate = _obs()
    assert await feature.get_latch_status(status=status, intermediate=intermediate) is LatchStatus.IDLE_OPEN
    status, intermediate = _obs()
    assert isinstance(await feature.get_status(status=status, intermediate=intermediate), HeaterShakerStatus)
    status, intermediate = _obs()
    assert (await feature.get_device_info(status=status, intermediate=intermediate)).model == "module"

    assert ("set_rpm", 1000) in controller.calls
    assert ("open_latch",) in controller.calls
    assert ("close_latch",) in controller.calls


@pytest.mark.asyncio
async def test_thermocycler_feature_endpoints() -> None:
    controller = _ThermocyclerController(calls=[])
    feature = ThermocyclerFeature(controller)

    status, intermediate = _obs()
    assert await feature.open_lid(status=status, intermediate=intermediate) is LidStatus.OPEN
    status, intermediate = _obs()
    assert await feature.close_lid(status=status, intermediate=intermediate) is LidStatus.OPEN
    status, intermediate = _obs()
    assert await feature.get_lid_status(status=status, intermediate=intermediate) is LidStatus.OPEN
    status, intermediate = _obs()
    assert await feature.set_lid_temperature(95.0, status=status, intermediate=intermediate) == Temperature(45.0, 95.0)
    status, intermediate = _obs()
    assert await feature.wait_for_lid_temperature(status=status, intermediate=intermediate) == Temperature(45.0, 95.0)
    status, intermediate = _obs()
    assert await feature.get_lid_temperature(status=status, intermediate=intermediate) == Temperature(45.0, 95.0)
    status, intermediate = _obs()
    assert await feature.set_plate_temperature(
        60.0,
        hold_time_seconds=0.0,
        volume_ul=0.0,
        ramp_rate=0.0,
        status=status,
        intermediate=intermediate,
    ) == Temperature(25.0, 60.0)
    status, intermediate = _obs()
    assert await feature.wait_for_plate_temperature(status=status, intermediate=intermediate) == Temperature(25.0, 60.0)
    status, intermediate = _obs()
    assert await feature.get_plate_temperature(status=status, intermediate=intermediate) == Temperature(25.0, 60.0)
    status, intermediate = _obs()
    profile_status = await feature.execute_profile(
        [ThermocyclerProfileStep(temperature_celsius=55.0, hold_time_seconds=10.0, ramp_rate=0.0)],
        repetitions=2,
        volume_ul=25.0,
        status=status,
        intermediate=intermediate,
    )
    assert isinstance(profile_status, ThermocyclerStatus)
    status, intermediate = _obs()
    assert await feature.deactivate_lid(status=status, intermediate=intermediate) == Temperature(45.0, 95.0)
    status, intermediate = _obs()
    assert await feature.deactivate_block(status=status, intermediate=intermediate) == Temperature(25.0, 60.0)
    status, intermediate = _obs()
    assert isinstance(await feature.deactivate_all(status=status, intermediate=intermediate), ThermocyclerStatus)
    status, intermediate = _obs()
    assert isinstance(await feature.get_status(status=status, intermediate=intermediate), ThermocyclerStatus)
    status, intermediate = _obs()
    assert (await feature.get_device_info(status=status, intermediate=intermediate)).firmware_version == "1.2.3"

    assert ("set_plate_temperature", 60.0, None, None, None) in controller.calls
    assert ("execute_profile", [{"temperature": 55.0, "hold_time_seconds": 10.0, "ramp_rate": None}], 2, 25.0) in (
        controller.calls
    )


@pytest.mark.asyncio
async def test_absorbance_reader_feature_endpoints() -> None:
    controller = _AbsorbanceController(calls=[])
    feature = AbsorbanceReaderFeature(controller)

    status, intermediate = _obs()
    configured = await feature.configure_measurement(
        MeasurementMode.SINGLE,
        wavelengths_nm=[450],
        reference_wavelength_nm=0,
        status=status,
        intermediate=intermediate,
    )
    assert configured.status is ReaderStatus.IDLE
    status, intermediate = _obs()
    assert await feature.start_measure(status=status, intermediate=intermediate) == AbsorbanceMeasurement(
        rows=[AbsorbanceMeasurementRow(values=[0.1, 0.2])]
    )
    status, intermediate = _obs()
    assert (await feature.deactivate(status=status, intermediate=intermediate)).status is ReaderStatus.IDLE
    status, intermediate = _obs()
    assert (await feature.get_status(status=status, intermediate=intermediate)).sample_wavelengths == [450]
    status, intermediate = _obs()
    assert (await feature.get_device_info(status=status, intermediate=intermediate)).serial_number == "SN123"

    assert ("configure_measurement", "SINGLE", [450], None) in controller.calls
    assert ("start_measure",) in controller.calls
    assert ("deactivate",) in controller.calls


@pytest.mark.asyncio
async def test_flex_stacker_feature_endpoints() -> None:
    controller = _FlexStackerController(calls=[])
    feature = FlexStackerFeature(controller)

    status, intermediate = _obs()
    assert (await feature.home_all(True, status=status, intermediate=intermediate)).status is StackerStatus.IDLE
    status, intermediate = _obs()
    assert await feature.home_axis(
        StackerAxisName.LATCH,
        StackerDirection.RETRACT,
        status=status,
        intermediate=intermediate,
    )
    status, intermediate = _obs()
    assert await feature.move_axis(
        StackerAxisName.X,
        StackerDirection.EXTEND,
        distance=12.5,
        status=status,
        intermediate=intermediate,
    )
    status, intermediate = _obs()
    assert (await feature.open_latch(status=status, intermediate=intermediate)).latch_state is LatchState.CLOSED
    status, intermediate = _obs()
    closed = await feature.close_latch(status=status, intermediate=intermediate)
    assert closed.platform_state is PlatformState.RETRACTED
    status, intermediate = _obs()
    assert (
        await feature.dispense_labware(
            labware_height=15.0,
            enforce_hopper_labware_sensing=True,
            enforce_shuttle_labware_sensing=False,
            status=status,
            intermediate=intermediate,
        )
    ).hopper_door_state is HopperDoorState.OPENED
    status, intermediate = _obs()
    assert (
        await feature.store_labware(
            labware_height=15.0,
            enforce_shuttle_labware_sensing=True,
            status=status,
            intermediate=intermediate,
        )
    ).initialized
    status, intermediate = _obs()
    assert (
        await feature.set_led(
            power=0.5,
            color=StackerLedColor.GREEN,
            pattern=StackerLedPattern.FLASH,
            duration_ms=100,
            repetitions=2,
            status=status,
            intermediate=intermediate,
        )
    ).install_detected
    status, intermediate = _obs()
    assert (await feature.deactivate(status=status, intermediate=intermediate)).status is StackerStatus.IDLE
    status, intermediate = _obs()
    switches = await feature.get_limit_switches(status=status, intermediate=intermediate)
    assert switches.x is AxisState.EXTENDED
    status, intermediate = _obs()
    assert (await feature.get_status(status=status, intermediate=intermediate)).error_details == ""
    status, intermediate = _obs()
    assert (await feature.get_device_info(status=status, intermediate=intermediate)).model == "module"

    assert ("home_axis", "L", "RETRACT") in controller.calls
    assert ("move_axis", "X", "EXTEND", 12.5) in controller.calls
    assert ("set_led", 0.5, "GREEN", "FLASH", 100, 2) in controller.calls
