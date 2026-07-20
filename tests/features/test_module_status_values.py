"""Value-level tests for module feature status reporting.

Covers the UNKNOWN fallback of the status enums (an unrecognized value from a
newer opentrons version must not surface as an undefined SiLA error). The
magnetic module is not supported on the Flex, so its status tests are gone.
"""

from unitelabs.opentrons_flex.features.absorbance_reader import AbsorbanceReaderFeature, MeasurementMode
from unitelabs.opentrons_flex.features.heater_shaker import LatchStatus
from unitelabs.opentrons_flex.features.temperature import TemperatureControlStatus
from unitelabs.opentrons_flex.features.thermocycler import LidStatus
from unitelabs.opentrons_flex.io import AbsorbanceReaderState


def test_latch_status_falls_back_to_unknown() -> None:
    assert LatchStatus("not-a-real-status") is LatchStatus.UNKNOWN


def test_lid_status_falls_back_to_unknown() -> None:
    assert LidStatus("not-a-real-status") is LidStatus.UNKNOWN


def test_temperature_control_status_falls_back_to_unknown() -> None:
    assert TemperatureControlStatus("not-a-real-status") is TemperatureControlStatus.UNKNOWN


async def test_uninitialized_reader_reports_unconfigured_measurement_mode() -> None:
    """An absent configuration must not be misreported as single-wavelength mode."""
    state = AbsorbanceReaderState(
        status="idle",
        lid_status="on",
        plate_presence="absent",
        supported_wavelengths=[450],
        measurement_mode="",
        sample_wavelengths=[],
        reference_wavelength=None,
    )
    controller = type("ReaderController", (), {"state": state})()

    subscription = AbsorbanceReaderFeature(controller).subscribe_status()
    try:
        assert (await anext(subscription)).measurement_mode is MeasurementMode.UNCONFIGURED
    finally:
        await subscription.aclose()


def test_measurement_mode_falls_back_to_unknown() -> None:
    assert MeasurementMode("not-a-real-mode") is MeasurementMode.UNKNOWN
