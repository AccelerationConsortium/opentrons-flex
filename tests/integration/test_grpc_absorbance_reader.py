"""End-to-end SiLA gRPC workflow tests for the simulated Absorbance Plate Reader."""

import base64

import grpc
import pytest
import pytest_asyncio
from sila.framework.protobuf import ConversionError

from unitelabs.opentrons_flex.features.absorbance_reader import PlateMeasurement, ReaderStatus

from .absorbance_reader_client import AbsorbanceReaderClient


@pytest_asyncio.fixture
async def reader(sila_channel) -> AbsorbanceReaderClient:
    channel, protobuf = sila_channel
    return AbsorbanceReaderClient(channel, protobuf)


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_absorbance_reader_multi_wavelength_workflow(reader: AbsorbanceReaderClient) -> None:
    """Initialize, read, identify, and deactivate through the actual SiLA wire surface."""
    info = await reader.get_device_info()
    assert info.serial_number == "AR-SIM-1"

    status = await reader.get_status()
    assert status.status is ReaderStatus.IDLE
    assert {450, 600}.issubset(status.supported_wavelengths)

    initialized = await reader.initialize_multiple([450, 600])
    assert initialized.sample_wavelengths == [450, 600]
    measurement = await reader.read_plate()
    assert isinstance(measurement, PlateMeasurement)
    assert [item.wavelength for item in measurement.measurements] == [450, 600]
    for item in measurement.measurements:
        assert len(item.wells) == 96
        assert item.wells[0].well_identifier == "A1"
        assert item.wells[-1].well_identifier == "H12"
        assert all(well.absorbance == 0.0 for well in item.wells)

    assert (await reader.deactivate()).status is ReaderStatus.IDLE


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_absorbance_reader_reference_initialization(reader: AbsorbanceReaderClient) -> None:
    """Reference wavelength reaches the transport and remains visible in status."""
    initialized = await reader.initialize_single_with_reference(450, 600)
    assert initialized.sample_wavelengths == [450]
    assert initialized.reference_wavelength == 600
    assert initialized.reference_active is True


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_absorbance_reader_rejects_module_unsupported_wavelength(reader: AbsorbanceReaderClient) -> None:
    """Dynamic module capabilities surface as a defined execution error."""
    with pytest.raises(grpc.aio.AioRpcError) as excinfo:
        await reader.initialize_single(562)
    assert excinfo.value.code() is grpc.StatusCode.ABORTED
    assert b"InvalidWavelengthError" in base64.b64decode(excinfo.value.details() or "")


@pytest.mark.asyncio
@pytest.mark.simulator_only
@pytest.mark.parametrize("wavelength", [349, 1001])
async def test_absorbance_reader_rejects_out_of_range_wavelength(
    reader: AbsorbanceReaderClient,
    wavelength: int,
) -> None:
    """Static FDL constraints reject invalid wavelengths before command execution."""
    with pytest.raises(ConversionError):
        await reader.initialize_single(wavelength)
