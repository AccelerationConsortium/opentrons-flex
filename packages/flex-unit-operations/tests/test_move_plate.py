"""Unit tests for move_plate against a recording fake transport."""

from __future__ import annotations

import pytest

from flex_unit_operations import geometry, uos
from flex_unit_operations.errors import (
    GripperNotReadyError,
    MachineNotReadyError,
    SameSlotError,
    UnknownSlotError,
)

PLATE = "opentrons/corning_96_wellplate_360ul_flat/2"


class FakeTransport:
    """Records the SiLA calls a unit operation makes, in order."""

    def __init__(self, *, estop="DISENGAGED", door=False, error=False, attached=True, state="IDLE", jaw=83.0):
        self.calls: list[tuple] = []
        self._status = {"Estop": estop, "DoorOpen": door, "IsErrorState": error, "Message": "boom" if error else ""}
        self._gripper = {"Attached": attached, "State": state, "Model": "gripperV1.2"}
        self._jaw = jaw

    async def machine_status(self):
        self.calls.append(("machine_status",))
        return dict(self._status)

    async def gripper_status(self):
        self.calls.append(("gripper_status",))
        return dict(self._gripper)

    async def home_jaw(self):
        self.calls.append(("home_jaw",))
        self._gripper["State"] = "IDLE"

    async def home_mount(self, mount):
        self.calls.append(("home_mount", mount))

    async def move_to(self, mount, point, *, speed=0.0):
        self.calls.append(("move_to", mount, round(point.x, 2), round(point.y, 2), round(point.z, 2)))

    async def grip(self, force):
        self.calls.append(("grip", force))

    async def ungrip(self, force):
        self.calls.append(("ungrip", force))

    async def jaw_width(self):
        self.calls.append(("jaw_width",))
        return self._jaw

    def names(self):
        return [c[0] for c in self.calls]


async def test_move_plate_call_sequence():
    t = FakeTransport()
    result = await uos.move_plate(t, labware_uri=PLATE, source_slot="D1", destination_slot="C2")

    # Grip before any lift, release only after arriving above the destination.
    assert t.names() == [
        "machine_status",
        "gripper_status",
        "home_mount",
        "move_to",  # above source
        "move_to",  # descend to grip height
        "grip",
        "jaw_width",
        "move_to",  # lift
        "move_to",  # traverse to above destination
        "move_to",  # descend to release height
        "ungrip",
        "move_to",  # retract
    ]
    assert result.source_slot == "D1"
    assert result.destination_slot == "C2"


async def test_grip_uses_force_and_height_from_the_labware_definition():
    t = FakeTransport()
    result = await uos.move_plate(t, labware_uri=PLATE, source_slot="D1", destination_slot="C2")
    expected = geometry.grip_geometry(PLATE, "D1", travel_z=uos.DEFAULT_TRAVEL_Z)

    grip = next(c for c in t.calls if c[0] == "grip")
    assert grip[1] == expected.force_newtons
    assert result.grip_point == expected.grip_point
    # Release sits below the grip height by the deck's drop offset, so labware is set
    # down rather than dropped.
    assert result.release_point.z < result.grip_point.z


async def test_traverse_happens_at_travel_height_not_deck_height():
    t = FakeTransport()
    await uos.move_plate(t, labware_uri=PLATE, source_slot="A1", destination_slot="D3", travel_z=180.0)
    moves = [c for c in t.calls if c[0] == "move_to"]
    # The two moves either side of the descent are the traverse; both must be clear.
    assert moves[0][4] == pytest.approx(180.0)
    assert moves[3][4] == pytest.approx(180.0)


async def test_unhomed_jaw_is_homed_rather_than_failing():
    t = FakeTransport(state="GripperJawState.UNHOMED")
    await uos.move_plate(t, labware_uri=PLATE, source_slot="D1", destination_slot="C2")
    assert "home_jaw" in t.names()


async def test_same_slot_is_refused_before_any_motion():
    t = FakeTransport()
    with pytest.raises(SameSlotError):
        await uos.move_plate(t, labware_uri=PLATE, source_slot="B2", destination_slot="B2")
    assert t.calls == []


async def test_unknown_slot_is_refused_before_any_motion():
    t = FakeTransport()
    with pytest.raises(UnknownSlotError):
        await uos.move_plate(t, labware_uri=PLATE, source_slot="D1", destination_slot="Z9")
    assert t.calls == []


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"estop": "ENGAGED"}, MachineNotReadyError),
        ({"door": True}, MachineNotReadyError),
        ({"error": True}, MachineNotReadyError),
        ({"attached": False}, GripperNotReadyError),
    ],
)
async def test_unsafe_states_refuse_to_move(kwargs, expected):
    t = FakeTransport(**kwargs)
    with pytest.raises(expected):
        await uos.move_plate(t, labware_uri=PLATE, source_slot="D1", destination_slot="C2")
    assert "move_to" not in t.names()
    assert "grip" not in t.names()
