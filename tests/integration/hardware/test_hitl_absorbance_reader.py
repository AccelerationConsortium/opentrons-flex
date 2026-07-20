"""Guarded hardware acceptance tests for an attached Absorbance Plate Reader."""

import pytest
import pytest_asyncio

from unitelabs.opentrons_flex.features.absorbance_reader import (
    AbsorbanceReaderStatus,
    PlatePresence,
    ReaderLidStatus,
)

from ..absorbance_reader_client import AbsorbanceReaderClient

pytestmark = pytest.mark.hardware_only


@pytest_asyncio.fixture
async def reader(sila_channel) -> AbsorbanceReaderClient:
    channel, protobuf = sila_channel
    return AbsorbanceReaderClient(channel, protobuf)


@pytest.mark.asyncio
async def test_absorbance_reader_identity_and_status_are_readable(reader: AbsorbanceReaderClient) -> None:
    """Confirm the attached reader is registered without initiating a measurement."""
    info = await reader.get_device_info()
    status = await reader.get_status()

    assert info.serial_number, "Plate Reader serial number is empty; check USB power and restart the connector"
    assert info.model, "Plate Reader model is empty; check module discovery and firmware compatibility"
    assert isinstance(status, AbsorbanceReaderStatus)
    assert status.supported_wavelengths, "The Plate Reader has not reported supported wavelengths"


@pytest.mark.asyncio
@pytest.mark.plate_reader_actuation
async def test_absorbance_reader_guarded_empty_initialization(
    reader: AbsorbanceReaderClient,
    request: pytest.FixtureRequest,
) -> None:
    """Initialize only after the operator explicitly prepares an empty, covered reader."""
    wavelength = request.config.getoption("--plate-reader-wavelength")
    before = await reader.get_status()
    assert before.lid_status is ReaderLidStatus.ON, (
        "Place the Plate Reader lid with the Flex Gripper before enabling initialization"
    )
    assert before.plate_presence is PlatePresence.ABSENT, (
        "Remove the plate before initialization; the reader must take an empty blank reading"
    )
    assert wavelength in before.supported_wavelengths, (
        f"Configured wavelength {wavelength} nm is unsupported; choose from {before.supported_wavelengths}"
    )

    initialized = await reader.initialize_single(wavelength)

    assert initialized.sample_wavelengths == [wavelength]
    assert initialized.reference_active is False


@pytest.mark.asyncio
@pytest.mark.plate_reader_measurement
async def test_absorbance_reader_guarded_plate_measurement(reader: AbsorbanceReaderClient) -> None:
    """Read a prepared covered plate after a previous initialization step."""
    before = await reader.get_status()
    assert before.sample_wavelengths, "Initialize the empty covered reader before preparing the plate measurement"
    assert before.lid_status is ReaderLidStatus.ON, (
        "Place the Plate Reader lid with the Flex Gripper before enabling measurement"
    )
    assert before.plate_presence is PlatePresence.PRESENT, (
        "Place a compatible 96-well plate in the reader before enabling measurement"
    )

    result = await reader.read_plate()

    assert [measurement.wavelength for measurement in result.measurements] == before.sample_wavelengths
    assert all(len(measurement.wells) == 96 for measurement in result.measurements)
    assert all(measurement.wells[0].well_identifier == "A1" for measurement in result.measurements)
    assert all(measurement.wells[-1].well_identifier == "H12" for measurement in result.measurements)
