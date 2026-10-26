"""Offline tests proving the adjustable-force path end-to-end on the simulator.

These exercise feature -> FlexGripperController -> OT3API simulator with a real
attached gripper (``simulated_gripper``), so the force argument is verified to
reach the hardware layer, not just the signature.
"""

import asyncio

import pytest
import pytest_asyncio
from opentrons.hardware_control.ot3api import OT3API
from opentrons.hardware_control.types import OT3Mount

from unitelabs.opentrons_flex.io import FlexGripperController


@pytest_asyncio.fixture
async def api_with_gripper() -> OT3API:
    api = await OT3API.build_hardware_simulator(
        attached_instruments={OT3Mount.GRIPPER: {"model": "gripperV1", "id": "GRIPPER-SIM-1"}}
    )
    yield api
    await api.clean_up()


@pytest_asyncio.fixture
async def gripper(api_with_gripper: OT3API) -> FlexGripperController:
    return FlexGripperController.from_api(api_with_gripper, lock=asyncio.Lock())


async def test_controller_grip_accepts_force(gripper: FlexGripperController):
    await gripper.grip(force_newtons=18.0)


async def test_controller_ungrip_accepts_force(gripper: FlexGripperController):
    await gripper.ungrip(force_newtons=18.0)


async def test_controller_ungrip_without_force_uses_default(gripper: FlexGripperController):
    """force_newtons=None stays valid — the default home force applies."""
    await gripper.ungrip()


async def test_controller_grip_ungrip_cycle_with_force(gripper: FlexGripperController):
    """A full heavy-plate pick/place cycle at elevated force completes."""
    await gripper.home_jaw()
    await gripper.ungrip(force_newtons=18.0)
    await gripper.grip(force_newtons=20.0)
    await gripper.ungrip(force_newtons=20.0)


async def test_force_below_minimum_rejected(gripper: FlexGripperController):
    """Out-of-envelope forces surface a hardware error, not a silent clamp."""
    with pytest.raises(Exception, match="out of bounds"):
        await gripper.grip(force_newtons=1.0)


async def test_force_above_maximum_rejected(gripper: FlexGripperController):
    with pytest.raises(Exception, match="out of bounds"):
        await gripper.grip(force_newtons=30.0)


# ── Feature-level (SiLA command surface) ─────────────────────────────────────


class _Status:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def update(self, **kwargs) -> None:
        self.updates.append(kwargs)


class _Intermediate:
    def __init__(self) -> None:
        self.messages: list = []

    def send(self, message) -> None:
        self.messages.append(message)


def _obs():
    return _Status(), _Intermediate()


@pytest_asyncio.fixture
async def gripper_feature(api_with_gripper: OT3API):
    from unitelabs.opentrons_flex.features.gripper import GripperFeature

    return GripperFeature(FlexGripperController.from_api(api_with_gripper, lock=asyncio.Lock()))


async def test_feature_grip_with_force_reports_progress(gripper_feature):
    status, intermediate = _obs()
    await gripper_feature.grip(force=18.0, status=status, intermediate=intermediate)
    phases = [m.phase for m in intermediate.messages]
    assert phases[0].name == "STARTING"
    assert phases[-1].name == "COMPLETED"


async def test_feature_ungrip_with_force_reports_progress(gripper_feature):
    status, intermediate = _obs()
    await gripper_feature.ungrip(force=18.0, status=status, intermediate=intermediate)
    phases = [m.phase for m in intermediate.messages]
    assert phases[0].name == "STARTING"
    assert phases[-1].name == "COMPLETED"


async def test_feature_ungrip_out_of_range_raises_defined_error(gripper_feature):
    """A sub-minimum force through the SiLA command surfaces GripActionError."""
    from unitelabs.opentrons_flex.io import GripActionError

    status, intermediate = _obs()
    with pytest.raises(GripActionError):
        await gripper_feature.ungrip(force=1.0, status=status, intermediate=intermediate)
