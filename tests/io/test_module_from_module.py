"""Tests for the from_module() adapters on the module controllers.

In with_robot_server mode the controllers wrap the high-level module objects
that the shared HardwareControlAPI already owns, instead of opening the serial
port a second time. These tests use fake module objects (duck-typed to the
opentrons module-object API) to assert each controller method maps to the right
module call/property and that disconnect() is a no-op (the API owns the tty).
"""

import asyncio
from typing import ClassVar

import pytest

from unitelabs.opentrons_flex.io import (
    AbsorbanceReaderController,
    DeviceInfo,
    FlexStackerController,
    HeaterShakerController,
    TemperatureModuleController,
    ThermocyclerController,
)


class _Recorder:
    def __init__(self):
        self.calls = []

    def record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))


# ── Temperature module ────────────────────────────────────────────────────────


class FakeTempDeck(_Recorder):
    temperature = 25.0
    target = 37.0
    device_info: ClassVar[dict] = {"serial": "T1", "model": "temp_v2", "version": "1.0"}

    async def start_set_temperature(self, celsius):
        self.record("start_set_temperature", celsius)

    async def deactivate(self):
        self.record("deactivate")


@pytest.mark.asyncio
async def test_temperature_from_module_maps_calls():
    mod = FakeTempDeck()
    ctrl = TemperatureModuleController.from_module(mod)

    await ctrl.set_temperature(50.0)
    assert ("start_set_temperature", (50.0,), {}) in mod.calls

    t = await ctrl.get_temperature()
    assert (t.current, t.target) == (25.0, 37.0)

    await ctrl.deactivate()
    assert ("deactivate", (), {}) in mod.calls

    assert await ctrl.get_device_info() == DeviceInfo.from_dict(mod.device_info)
    assert await ctrl.is_connected() is True
    await ctrl.disconnect()  # no-op, must not raise


# ── Absorbance Reader ─────────────────────────────────────────────────────────


class _AbsConfig:
    measure_mode = type("Mode", (), {"value": "single"})()
    sample_wavelengths: ClassVar[list[int]] = [450]
    reference_wavelength = None


class _AbsStatus:
    value = "idle"


class _AbsValue:
    def __init__(self, value):
        self.value = value


class FakeAbsorbanceReader(_Recorder):
    status = _AbsStatus()
    lid_status = _AbsValue("on")
    plate_presence = _AbsValue("present")
    supported_wavelengths: ClassVar[list[int]] = [450, 600]
    measurement_config = _AbsConfig()
    device_info: ClassVar[dict] = {"serial": "AR1", "model": "absorbanceReaderV1", "version": "1.0"}

    async def set_sample_wavelength(self, mode, wavelengths, reference_wavelength):
        self.record("set_sample_wavelength", mode, wavelengths, reference_wavelength)

    async def start_measure(self):
        self.record("start_measure")
        return [[0.1, 0.2], [0.3, 0.4]]

    async def deactivate(self):
        self.record("deactivate")


@pytest.mark.asyncio
async def test_absorbance_reader_from_module_maps_calls():
    from opentrons.drivers.types import ABSMeasurementMode

    mod = FakeAbsorbanceReader()
    ctrl = AbsorbanceReaderController.from_module(mod)

    await ctrl.configure_measurement(ABSMeasurementMode.SINGLE, [450], None)
    assert ("set_sample_wavelength", (ABSMeasurementMode.SINGLE, [450], None), {}) in mod.calls

    measurement = await ctrl.start_measure()
    assert [row.values for row in measurement.rows] == [[0.1, 0.2], [0.3, 0.4]]
    assert ("start_measure", (), {}) in mod.calls

    state = await ctrl.get_state()
    assert state.status == "idle"
    assert state.lid_status == "on"
    assert state.supported_wavelengths == [450, 600]

    await ctrl.deactivate()
    assert ("deactivate", (), {}) in mod.calls

    assert await ctrl.get_device_info() == DeviceInfo.from_dict(mod.device_info)
    await ctrl.disconnect()


# ── Heater-Shaker ─────────────────────────────────────────────────────────────


class FakeHeaterShaker(_Recorder):
    temperature = 30.0
    target_temperature = 60.0
    speed = 500
    target_speed = 1000
    labware_latch_status = "idle_closed"
    device_info: ClassVar[dict] = {"serial": "HS1", "model": "hs_v1", "version": "2.0"}

    async def start_set_temperature(self, celsius):
        self.record("start_set_temperature", celsius)

    async def deactivate_heater(self):
        self.record("deactivate_heater")

    async def set_speed(self, rpm):
        self.record("set_speed", rpm)

    async def deactivate_shaker(self):
        self.record("deactivate_shaker")

    async def open_labware_latch(self):
        self.record("open_labware_latch")

    async def close_labware_latch(self):
        self.record("close_labware_latch")


@pytest.mark.asyncio
async def test_heater_shaker_from_module_maps_calls():
    mod = FakeHeaterShaker()
    ctrl = HeaterShakerController.from_module(mod)

    await ctrl.set_temperature(55.0)
    assert ("start_set_temperature", (55.0,), {}) in mod.calls

    t = await ctrl.get_temperature()
    assert (t.current, t.target) == (30.0, 60.0)

    await ctrl.set_rpm(800)
    assert ("set_speed", (800,), {}) in mod.calls

    r = await ctrl.get_rpm()
    assert (r.current, r.target) == (500, 1000)

    await ctrl.stop_shaking()
    assert ("deactivate_shaker", (), {}) in mod.calls

    await ctrl.open_latch()
    await ctrl.close_latch()
    assert ("open_labware_latch", (), {}) in mod.calls
    assert ("close_labware_latch", (), {}) in mod.calls

    assert await ctrl.get_latch_status() == "idle_closed"
    assert await ctrl.get_device_info() == DeviceInfo.from_dict(mod.device_info)
    await ctrl.disconnect()  # no-op


@pytest.mark.asyncio
async def test_module_action_waits_for_shared_hardware_lock():
    mod = FakeHeaterShaker()
    shared_lock = asyncio.Lock()
    ctrl = HeaterShakerController.from_module(mod, lock=shared_lock)

    await shared_lock.acquire()
    task = asyncio.create_task(ctrl.close_latch())
    await asyncio.sleep(0)
    assert ("close_labware_latch", (), {}) not in mod.calls

    shared_lock.release()
    await task
    assert ("close_labware_latch", (), {}) in mod.calls


# ── Thermocycler ──────────────────────────────────────────────────────────────


class _LidStatus:
    name = "OPEN"


class FakeThermocycler(_Recorder):
    temperature = 70.0
    target = 95.0
    lid_temp = 100.0
    lid_target = 105.0
    lid_status = _LidStatus()
    device_info: ClassVar[dict] = {"serial": "TC1", "model": "tc_v2", "version": "3.0"}

    async def open(self):
        self.record("open")

    async def close(self):
        self.record("close")

    async def set_target_lid_temperature(self, celsius):
        self.record("set_target_lid_temperature", celsius)

    async def set_target_block_temperature(self, celsius, hold_time_seconds=None, volume=None):
        self.record("set_target_block_temperature", celsius, hold_time_seconds, volume)

    async def deactivate_lid(self):
        self.record("deactivate_lid")

    async def deactivate_block(self):
        self.record("deactivate_block")

    async def deactivate(self):
        self.record("deactivate")


@pytest.mark.asyncio
async def test_thermocycler_from_module_maps_calls():
    mod = FakeThermocycler()
    ctrl = ThermocyclerController.from_module(mod)

    await ctrl.open_lid()
    await ctrl.close_lid()
    assert ("open", (), {}) in mod.calls
    assert ("close", (), {}) in mod.calls

    assert await ctrl.get_lid_status() == "open"

    await ctrl.set_lid_temperature(105.0)
    assert ("set_target_lid_temperature", (105.0,), {}) in mod.calls

    await ctrl.set_plate_temperature(95.0, hold_time=30.0, volume=25.0)
    assert ("set_target_block_temperature", (95.0, 30.0, 25.0), {}) in mod.calls

    lid = await ctrl.get_lid_temperature()
    assert (lid.current, lid.target) == (100.0, 105.0)

    plate = await ctrl.get_plate_temperature()
    assert (plate.current, plate.target) == (70.0, 95.0)

    await ctrl.deactivate_lid()
    await ctrl.deactivate_block()
    await ctrl.deactivate_all()
    assert ("deactivate_lid", (), {}) in mod.calls
    assert ("deactivate_block", (), {}) in mod.calls
    assert ("deactivate", (), {}) in mod.calls

    assert await ctrl.get_device_info() == DeviceInfo.from_dict(mod.device_info)
    await ctrl.disconnect()  # no-op


# ── Flex Stacker ──────────────────────────────────────────────────────────────


class _StackerValue:
    def __init__(self, value):
        self.value = value


class FakeFlexStacker(_Recorder):
    status = _StackerValue("idle")
    latch_state = _StackerValue("closed")
    platform_state = _StackerValue("extended")
    hopper_door_state = _StackerValue("closed")
    install_detected = True
    initialized = True
    live_data: ClassVar[dict] = {"data": {"errorDetails": None}}
    device_info: ClassVar[dict] = {"serial": "FS1", "model": "flexStackerModuleV1", "version": "1.0"}

    def __init__(self):
        super().__init__()
        from opentrons.drivers.flex_stacker.types import StackerAxis
        from opentrons.hardware_control.modules.types import StackerAxisState

        self.limit_switch_status = {
            StackerAxis.X: StackerAxisState.EXTENDED,
            StackerAxis.Z: StackerAxisState.RETRACTED,
            StackerAxis.L: StackerAxisState.EXTENDED,
        }

    async def home_all(self, ignore_latch=False):
        self.record("home_all", ignore_latch)

    async def home_axis(self, axis, direction):
        self.record("home_axis", axis, direction)
        return True

    async def move_axis(self, axis, direction, distance):
        self.record("move_axis", axis, direction, distance)
        return True

    async def open_latch(self):
        self.record("open_latch")
        return True

    async def close_latch(self):
        self.record("close_latch")
        return True

    async def dispense_labware(self, labware_height, enforce_hopper_lw_sensing=True, enforce_shuttle_lw_sensing=True):
        self.record("dispense_labware", labware_height, enforce_hopper_lw_sensing, enforce_shuttle_lw_sensing)

    async def store_labware(self, labware_height, enforce_shuttle_lw_sensing=True):
        self.record("store_labware", labware_height, enforce_shuttle_lw_sensing)

    async def set_led_state(self, power, color=None, pattern=None, duration=None, reps=None):
        self.record("set_led_state", power, color, pattern, duration, reps)

    async def deactivate(self):
        self.record("deactivate")


@pytest.mark.asyncio
async def test_flex_stacker_from_module_maps_calls():
    from opentrons.drivers.flex_stacker.types import Direction, LEDColor, LEDPattern, StackerAxis

    mod = FakeFlexStacker()
    ctrl = FlexStackerController.from_module(mod)

    await ctrl.home_all(ignore_latch=True)
    assert ("home_all", (True,), {}) in mod.calls

    assert await ctrl.home_axis(StackerAxis.X, Direction.EXTEND) is True
    assert await ctrl.move_axis(StackerAxis.Z, Direction.RETRACT, 12.5) is True

    await ctrl.open_latch()
    await ctrl.close_latch()
    assert ("open_latch", (), {}) in mod.calls
    assert ("close_latch", (), {}) in mod.calls

    await ctrl.dispense_labware(14.0, True, False)
    assert ("dispense_labware", (14.0, True, False), {}) in mod.calls

    await ctrl.store_labware(14.0, True)
    assert ("store_labware", (14.0, True), {}) in mod.calls

    await ctrl.set_led(0.5, LEDColor.BLUE, LEDPattern.PULSE, 200, -1)
    assert ("set_led_state", (0.5, LEDColor.BLUE, LEDPattern.PULSE, 200, -1), {}) in mod.calls

    switches = await ctrl.get_limit_switches()
    assert (switches.x, switches.z, switches.latch) == ("extended", "retracted", "extended")

    state = await ctrl.get_state()
    assert state.status == "idle"
    assert state.install_detected is True

    await ctrl.deactivate()
    assert ("deactivate", (), {}) in mod.calls
    assert await ctrl.get_device_info() == DeviceInfo.from_dict(mod.device_info)
    await ctrl.disconnect()


# The Magnetic Module is not supported on the Flex, so there is no magnetic
# controller or from_module() adapter to test here.
