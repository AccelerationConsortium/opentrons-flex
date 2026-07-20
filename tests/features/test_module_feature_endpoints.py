"""Direct endpoint tests for module SiLA features.

These use lightweight fake controllers rather than hardware simulators. The goal
is endpoint coverage: every public module feature command should call the
expected controller method and return structured data rather than a boolean
success sentinel.
"""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from unitelabs.opentrons_flex.features.absorbance_reader import (
    AbsorbanceReaderFeature,
    PlateMeasurement,
    ReaderStatus,
)
from unitelabs.opentrons_flex.features.flex_stacker import (
    AxisState,
    FlexStackerFeature,
    FlexStackerMaintenanceFeature,
    HopperDoorState,
    LatchState,
    PlatformState,
    StackerAxisName,
    StackerDirection,
    StackerLedColor,
    StackerLedPattern,
    StackerStatus,
)
from unitelabs.opentrons_flex.features.heater_shaker import (
    HeaterShakerFeature,
    HeaterShakerSpeed,
    HeaterShakerStatus,
    HeaterShakerTemperature,
    LatchStatus,
)
from unitelabs.opentrons_flex.features._progress import OperationPhase
from unitelabs.opentrons_flex.features.temperature import TemperatureModuleFeature
from unitelabs.opentrons_flex.features.thermocycler import (
    LidStatus,
    ThermocyclerFeature,
    ThermocyclerProfileStep,
    ThermocyclerStatus,
    ThermocyclerTemperature,
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
    TemperatureModuleState,
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


async def _first(stream):
    """Read the first observable-property value and close the subscription."""
    try:
        return await anext(stream)
    finally:
        await stream.aclose()


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

    async def set_temperature_and_wait(self, temperature: float) -> None:
        self.calls.append(("set_temperature_and_wait", temperature))

    async def get_temperature(self) -> Temperature:
        self.calls.append(("get_temperature",))
        return Temperature(current=24.5, target=37.0)

    async def deactivate(self) -> None:
        self.calls.append(("deactivate",))

    @property
    def state(self) -> TemperatureModuleState:
        target = None if ("deactivate",) in self.calls else 37.0
        return TemperatureModuleState(
            status="idle" if target is None else "holding at target",
            current_temperature=24.5,
            target_temperature=target,
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(serial_number="SN123", model="temperatureModuleV2", firmware_version="1.2.3")


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
    async def initialize_measurement(
        self,
        mode: object,
        wavelengths: list[int],
        reference_wavelength: int | None,
    ) -> None:
        self.calls.append(("initialize_measurement", mode.name, wavelengths, reference_wavelength))

    async def read_plate(self) -> AbsorbanceMeasurement:
        self.calls.append(("read_plate",))
        return AbsorbanceMeasurement(rows=[AbsorbanceMeasurementRow(wavelength=450, values=[0.1, 0.2])])

    async def deactivate(self) -> None:
        self.calls.append(("deactivate",))

    @property
    def state(self) -> AbsorbanceReaderState:
        return AbsorbanceReaderState(
            status="idle",
            lid_status="on",
            plate_presence="present",
            supported_wavelengths=[450, 600],
            measurement_mode="single",
            sample_wavelengths=[450],
            reference_wavelength=None,
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(serial_number="SN123", model="module", firmware_version="1.2.3")


class _FlexStackerController(_BaseController):
    async def home_all(self, ignore_latch: bool) -> None:
        self.calls.append(("home_all", ignore_latch))

    async def home_axis(self, axis: object, direction: object) -> None:
        self.calls.append(("home_axis", axis.name, direction.name))

    async def move_axis(self, axis: object, direction: object, distance: float) -> None:
        self.calls.append(("move_axis", axis.name, direction.name, distance))

    async def open_latch(self) -> None:
        self.calls.append(("open_latch",))

    async def close_latch(self) -> None:
        self.calls.append(("close_latch",))

    async def retrieve_labware(
        self,
        labware_height: float,
        enforce_hopper_labware_sensing: bool,
        enforce_shuttle_labware_sensing: bool,
    ) -> None:
        self.calls.append(
            (
                "retrieve_labware",
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
        duration: float,
        repetitions: int,
    ) -> None:
        self.calls.append(("set_led", power, color.name, pattern.name, duration, repetitions))

    async def deactivate(self) -> None:
        self.calls.append(("deactivate",))

    async def halt_for_cancellation(self) -> None:
        self.calls.append(("halt_for_cancellation",))

    @property
    def limit_switches(self) -> FlexStackerLimitSwitches:
        return FlexStackerLimitSwitches(x="extended", z="retracted", latch="unknown")

    @property
    def state(self) -> FlexStackerState:
        return FlexStackerState(
            status="idle",
            latch_state="closed",
            platform_state="retracted",
            hopper_door_state="opened",
            install_detected=True,
            initialized=True,
            recovery_required=False,
            error_details="",
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(serial_number="SN123", model="module", firmware_version="1.2.3")


@pytest.mark.asyncio
async def test_temperature_module_feature_endpoints() -> None:
    controller = _TemperatureController(calls=[])
    feature = TemperatureModuleFeature(controller)

    status, intermediate = _obs()
    assert (await feature.set_temperature(37.0, status=status, intermediate=intermediate)).target_active is True
    status, intermediate = _obs()
    assert (
        await feature.set_temperature_and_wait(37.0, status=status, intermediate=intermediate)
    ).target_temperature == 37.0
    assert (await _first(feature.subscribe_status())).target_active is True
    assert feature.device_info().serial_number == "SN123"
    status, intermediate = _obs()
    assert (await feature.deactivate(status=status, intermediate=intermediate)).target_active is False

    assert ("set_temperature", 37.0) in controller.calls
    assert ("set_temperature_and_wait", 37.0) in controller.calls
    assert ("deactivate",) in controller.calls


@pytest.mark.asyncio
async def test_temperature_module_wait_cancellation_deactivates_control() -> None:
    class _BlockingTemperatureController(_TemperatureController):
        def __init__(self) -> None:
            super().__init__(calls=[])
            self.started = asyncio.Event()

        async def set_temperature_and_wait(self, temperature: float) -> None:
            self.started.set()
            await asyncio.Event().wait()

    controller = _BlockingTemperatureController()
    feature = TemperatureModuleFeature(controller)
    status, intermediate = _obs()
    task = asyncio.create_task(feature.set_temperature_and_wait(20.0, status=status, intermediate=intermediate))
    await controller.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ("deactivate",) in controller.calls
    assert intermediate.messages[-1].phase is OperationPhase.CANCELLED


@pytest.mark.asyncio
async def test_heater_shaker_feature_endpoints() -> None:
    controller = _HeaterShakerController(calls=[])
    feature = HeaterShakerFeature(controller)

    status, intermediate = _obs()
    assert await feature.set_temperature(42.0, status=status, intermediate=intermediate) == HeaterShakerTemperature(
        25.0, 42.0, True
    )
    status, intermediate = _obs()
    assert await feature.wait_for_temperature(
        42.0, status=status, intermediate=intermediate
    ) == HeaterShakerTemperature(25.0, 42.0, True)
    status, intermediate = _obs()
    assert await feature.get_temperature(status=status, intermediate=intermediate) == HeaterShakerTemperature(
        25.0, 42.0, True
    )
    status, intermediate = _obs()
    assert await feature.deactivate_heater(status=status, intermediate=intermediate) == HeaterShakerTemperature(
        25.0, 42.0, True
    )
    status, intermediate = _obs()
    assert await feature.set_speed(1000, status=status, intermediate=intermediate) == HeaterShakerSpeed(500, 1000, True)
    status, intermediate = _obs()
    assert await feature.get_speed(status=status, intermediate=intermediate) == HeaterShakerSpeed(500, 1000, True)
    status, intermediate = _obs()
    assert await feature.stop_shaking(status=status, intermediate=intermediate) == HeaterShakerSpeed(500, 1000, True)
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
async def test_heater_shaker_motion_can_be_cancelled() -> None:
    """Cancellation reaches the controller action and emits a terminal update."""

    class _BlockingHeaterShaker(_HeaterShakerController):
        started: asyncio.Event

        def __init__(self) -> None:
            super().__init__(calls=[])
            self.started = asyncio.Event()

        async def set_rpm(self, rpm: int) -> None:
            self.calls.append(("set_rpm", rpm))
            self.started.set()
            await asyncio.Event().wait()

    controller = _BlockingHeaterShaker()
    feature = HeaterShakerFeature(controller)
    status, intermediate = _obs()
    task = asyncio.create_task(feature.set_speed(500, status=status, intermediate=intermediate))

    await controller.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert intermediate.messages[-1].phase is OperationPhase.CANCELLED
    assert status.updates[-1]["progress"] == 1.0


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
    expected_lid_temperature = ThermocyclerTemperature(45.0, 95.0, True)
    expected_plate_temperature = ThermocyclerTemperature(25.0, 60.0, True)
    assert await feature.set_lid_temperature(95.0, status=status, intermediate=intermediate) == expected_lid_temperature
    status, intermediate = _obs()
    assert await feature.wait_for_lid_temperature(status=status, intermediate=intermediate) == expected_lid_temperature
    status, intermediate = _obs()
    assert await feature.get_lid_temperature(status=status, intermediate=intermediate) == expected_lid_temperature
    status, intermediate = _obs()
    assert (
        await feature.set_plate_temperature(
            60.0,
            hold_time=0.0,
            volume=0.0,
            ramp_rate=0.0,
            status=status,
            intermediate=intermediate,
        )
        == expected_plate_temperature
    )
    status, intermediate = _obs()
    assert (
        await feature.wait_for_plate_temperature(status=status, intermediate=intermediate) == expected_plate_temperature
    )
    status, intermediate = _obs()
    assert await feature.get_plate_temperature(status=status, intermediate=intermediate) == expected_plate_temperature
    status, intermediate = _obs()
    profile_status = await feature.execute_profile(
        [ThermocyclerProfileStep(temperature=55.0, hold_time=10.0, ramp_rate=0.0)],
        repetitions=2,
        volume=25.0,
        status=status,
        intermediate=intermediate,
    )
    assert isinstance(profile_status, ThermocyclerStatus)
    status, intermediate = _obs()
    assert await feature.deactivate_lid(status=status, intermediate=intermediate) == expected_lid_temperature
    status, intermediate = _obs()
    assert await feature.deactivate_block(status=status, intermediate=intermediate) == expected_plate_temperature
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
    configured = await feature.initialize_single(
        wavelength=450,
        status=status,
        intermediate=intermediate,
    )
    assert configured.status is ReaderStatus.IDLE
    status, intermediate = _obs()
    referenced = await feature.initialize_single_with_reference(
        wavelength=450,
        reference_wavelength=600,
        status=status,
        intermediate=intermediate,
    )
    assert referenced.reference_active is False
    status, intermediate = _obs()
    await feature.initialize_multiple(wavelengths=[450, 600], status=status, intermediate=intermediate)
    status, intermediate = _obs()
    measurement = await feature.read_plate(status=status, intermediate=intermediate)
    assert isinstance(measurement, PlateMeasurement)
    assert measurement.measurements[0].wavelength == 450
    assert [well.well_identifier for well in measurement.measurements[0].wells] == ["A1", "A2"]
    assert [well.absorbance for well in measurement.measurements[0].wells] == [0.2, 0.1]
    status, intermediate = _obs()
    assert (await feature.deactivate(status=status, intermediate=intermediate)).status is ReaderStatus.IDLE
    assert (await _first(feature.subscribe_status())).sample_wavelengths == [450]
    assert feature.device_info().serial_number == "SN123"

    assert ("initialize_measurement", "SINGLE", [450], None) in controller.calls
    assert ("initialize_measurement", "SINGLE", [450], 600) in controller.calls
    assert ("initialize_measurement", "MULTI", [450, 600], None) in controller.calls
    assert ("read_plate",) in controller.calls
    assert ("deactivate",) in controller.calls


@pytest.mark.asyncio
async def test_flex_stacker_feature_endpoints() -> None:
    controller = _FlexStackerController(calls=[])
    feature = FlexStackerFeature(controller)
    maintenance = FlexStackerMaintenanceFeature(controller)

    status, intermediate = _obs()
    assert (await maintenance.home_all(True, status=status, intermediate=intermediate)).status is StackerStatus.IDLE
    status, intermediate = _obs()
    assert (
        await maintenance.home_axis(
            StackerAxisName.LATCH,
            StackerDirection.RETRACT,
            status=status,
            intermediate=intermediate,
        )
    ).status is StackerStatus.IDLE
    status, intermediate = _obs()
    assert (
        await maintenance.move_axis(
            StackerAxisName.X,
            StackerDirection.EXTEND,
            distance=12.5,
            status=status,
            intermediate=intermediate,
        )
    ).status is StackerStatus.IDLE
    status, intermediate = _obs()
    assert (await maintenance.open_latch(status=status, intermediate=intermediate)).latch_state is LatchState.CLOSED
    status, intermediate = _obs()
    closed = await maintenance.close_latch(status=status, intermediate=intermediate)
    assert closed.platform_state is PlatformState.RETRACTED
    status, intermediate = _obs()
    assert (
        await feature.retrieve_labware(
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
        await maintenance.set_led(
            power=0.5,
            color=StackerLedColor.GREEN,
            pattern=StackerLedPattern.FLASH,
            duration=0.1,
            repetitions=2,
            status=status,
            intermediate=intermediate,
        )
    ).install_detected
    status, intermediate = _obs()
    assert (await maintenance.deactivate(status=status, intermediate=intermediate)).status is StackerStatus.IDLE
    switches = await _first(maintenance.subscribe_limit_switch_status())
    assert switches.x is AxisState.EXTENDED
    assert (await _first(feature.subscribe_status())).error_details == ""
    assert feature.device_info().model == "module"

    assert ("home_axis", "LATCH", "RETRACT") in controller.calls
    assert ("move_axis", "X", "EXTEND", 12.5) in controller.calls
    assert ("set_led", 0.5, "GREEN", "FLASH", 0.1, 2) in controller.calls


@pytest.mark.asyncio
async def test_flex_stacker_cancellation_stops_motors() -> None:
    class _BlockingStackerController(_FlexStackerController):
        def __init__(self) -> None:
            super().__init__(calls=[])
            self.started = asyncio.Event()

        async def move_axis(self, axis: object, direction: object, distance: float) -> None:
            self.started.set()
            await asyncio.Event().wait()

    controller = _BlockingStackerController()
    feature = FlexStackerMaintenanceFeature(controller)
    status, intermediate = _obs()
    task = asyncio.create_task(
        feature.move_axis(
            StackerAxisName.X,
            StackerDirection.EXTEND,
            10.0,
            status=status,
            intermediate=intermediate,
        )
    )
    await controller.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ("halt_for_cancellation",) in controller.calls
    assert intermediate.messages[-1].phase is OperationPhase.CANCELLED
