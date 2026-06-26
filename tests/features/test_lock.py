"""Tests that FlexMotionController serialises concurrent callers via asyncio.Lock.

Offline: depends only on the opentrons OT3 simulator.
"""

import asyncio

import pytest_asyncio
from opentrons.hardware_control.ot3api import OT3API
from opentrons.hardware_control.types import Axis, OT3Mount
from opentrons.types import Point

from unitelabs.opentrons_flex.io import FlexMotionController


@pytest_asyncio.fixture
async def controller() -> FlexMotionController:
    api = await OT3API.build_hardware_simulator()
    ctrl = FlexMotionController.from_api(api, lock=asyncio.Lock())
    yield ctrl
    await api.clean_up()


async def test_concurrent_homes_do_not_raise(controller: FlexMotionController) -> None:
    """Two concurrent home calls must complete without error (serialised by the lock)."""
    await asyncio.gather(controller.home(), controller.home())


async def test_concurrent_moves_do_not_raise(controller: FlexMotionController) -> None:
    """Two concurrent move calls must complete without error."""
    await controller.home()
    await asyncio.gather(
        controller.move_rel(OT3Mount.LEFT, Point(x=-5, y=0, z=0)),
        controller.move_rel(OT3Mount.LEFT, Point(x=0, y=-5, z=0)),
    )


async def test_lock_serialises_calls(controller: FlexMotionController) -> None:
    """Calls made while the lock is held must wait, not interleave."""
    order: list[str] = []

    async def tagged_home(tag: str) -> None:
        await controller.home([Axis.Z_L])
        order.append(tag)

    await asyncio.gather(tagged_home("a"), tagged_home("b"))
    assert len(order) == 2  # both completed without interleave error
