"""Contract checks for the Opentrons Flex APIs this connector calls.

These are intentionally narrow signature tests. The mocked wiring tests can
prove our code calls a method, but they cannot catch an upstream opentrons API
rename before the connector is started on a robot.
"""

import inspect

from opentrons.hardware_control import ot3_calibration
from opentrons.hardware_control.ot3api import OT3API


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
