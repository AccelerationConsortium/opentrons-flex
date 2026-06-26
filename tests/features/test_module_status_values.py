"""Value-level tests for module feature status reporting.

Covers the UNKNOWN fallback of the status enums (an unrecognized value from a
newer opentrons version must not surface as an undefined SiLA error). The
magnetic module is not supported on the Flex, so its status tests are gone.
"""

from unitelabs.opentrons_flex.features.heater_shaker import LatchStatus
from unitelabs.opentrons_flex.features.thermocycler import LidStatus


def test_latch_status_falls_back_to_unknown() -> None:
    assert LatchStatus("not-a-real-status") is LatchStatus.UNKNOWN


def test_lid_status_falls_back_to_unknown() -> None:
    assert LidStatus("not-a-real-status") is LidStatus.UNKNOWN
