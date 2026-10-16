"""Contract checks for the Opentrons Flex APIs this connector calls.

These are intentionally narrow signature tests. The mocked wiring tests can
prove our code calls a method, but they cannot catch an upstream opentrons API
rename before the connector is started on a robot.
"""

import inspect

import pytest

from opentrons.hardware_control import ot3_calibration
from opentrons.hardware_control.modules.heater_shaker import HeaterShaker
from opentrons.hardware_control.modules.thermocycler import Thermocycler
from opentrons.hardware_control.ot3api import OT3API
from opentrons.protocol_api.module_validation_and_errors import (
    APIVersion,
    InvalidTargetTemperatureError,
    validate_heater_shaker_temperature,
)


def _params(fn: object) -> list[str]:
    return list(inspect.signature(fn).parameters)


def test_ot3api_motion_contract() -> None:
    assert _params(OT3API.build_hardware_controller)[:3] == [
        "attached_instruments",
        "attached_modules",
        "config",
    ]
    assert {"mount", "abs_position", "speed"}.issubset(_params(OT3API.move_to))
    assert {"mount", "delta", "speed"}.issubset(_params(OT3API.move_rel))
    assert {"mount", "refresh"}.issubset(_params(OT3API.gantry_position))
    assert {"mount", "volume", "rate"}.issubset(_params(OT3API.aspirate))
    assert {"mount", "volume", "rate", "push_out"}.issubset(_params(OT3API.dispense))
    assert {"mount", "volume"}.issubset(_params(OT3API.blow_out))


def test_ot3api_lights_and_gripper_contract() -> None:
    assert {"button", "rails"}.issubset(_params(OT3API.set_lights))
    assert _params(OT3API.get_lights) == ["self"]
    assert {"force_newtons"}.issubset(_params(OT3API.grip))
    assert {"force_newtons"}.issubset(_params(OT3API.ungrip))
    assert {"recalibrate_jaw_width"}.issubset(_params(OT3API.home_gripper_jaw))


def test_ot3_calibration_contract() -> None:
    assert {"hcapi", "mount", "slot"}.issubset(_params(ot3_calibration.calibrate_pipette))
    assert {"hcapi", "probe", "slot"}.issubset(_params(ot3_calibration.calibrate_gripper_jaw))
    assert {"hcapi", "mount", "pipette_id"}.issubset(_params(ot3_calibration.calibrate_belts))


def test_accessory_module_method_contracts() -> None:
    """Catch upstream changes to the high-level module methods used by the connector."""
    assert {"celsius"}.issubset(_params(HeaterShaker.start_set_temperature))
    assert {"celsius", "hold_time_seconds", "volume", "ramp_rate"}.issubset(
        _params(Thermocycler.set_target_block_temperature)
    )
    assert {"celsius"}.issubset(_params(Thermocycler.set_target_lid_temperature))


def test_heater_shaker_temperature_contract_matches_opentrons_api_2_25_plus() -> None:
    """Guard the API 2.25 removal of the former 37 °C lower limit."""
    api_version = APIVersion(2, 25)

    assert validate_heater_shaker_temperature(0.0, api_version) == 0.0
    assert validate_heater_shaker_temperature(36.9, api_version) == 36.9
    assert validate_heater_shaker_temperature(95.0, api_version) == 95.0
    with pytest.raises(InvalidTargetTemperatureError):
        validate_heater_shaker_temperature(-0.1, api_version)
    with pytest.raises(InvalidTargetTemperatureError):
        validate_heater_shaker_temperature(95.1, api_version)
