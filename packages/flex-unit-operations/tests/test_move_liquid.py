"""Unit tests for move_liquid, pipette selection and the tip unit operations."""

from __future__ import annotations

import pytest

from flex_unit_operations import geometry, uos
from flex_unit_operations.errors import (
    PipetteSelectionError,
    TipStateError,
    UnknownWellError,
    VolumeOutOfRangeError,
)

PLATE = "opentrons/corning_96_wellplate_360ul_flat/2"
TIPRACK_1000 = "opentrons/opentrons_flex_96_tiprack_1000ul/1"

# The live robot's configuration: p1000 on the left, p50 on the right.
P1000 = {"Mount": "left", "Attached": True, "Model": "p1000_single_v3.5", "MinVolume": 5.0, "MaxVolume": 1000.0}
P50 = {"Mount": "right", "Attached": True, "Model": "p50_single_v3.5", "MinVolume": 5.0, "MaxVolume": 50.0}


def pipettes(*entries, with_tip_on=None):
    out = []
    for e in entries:
        item = dict(e)
        item["HasTip"] = item["Mount"] == with_tip_on
        out.append(item)
    return out


class FakeTransport:
    def __init__(self, attached, *, with_tip_on=None, presence="ABSENT"):
        self.calls: list[tuple] = []
        self._pipettes = pipettes(*attached, with_tip_on=with_tip_on)
        self._presence = presence

    async def machine_status(self):
        return {"Estop": "DISENGAGED", "DoorOpen": False, "IsErrorState": False, "Message": ""}

    async def attached_pipettes(self):
        return [dict(p) for p in self._pipettes]

    async def transfer(self, mount, *, source, source_retract, destination, destination_retract, volume_ul, profile=None):
        self.calls.append(("transfer", mount, round(volume_ul, 3), round(source.z, 2), round(destination.z, 2)))

    async def tip_presence(self, mount):
        self.calls.append(("tip_presence", mount))
        return self._presence

    async def pick_up_tip(self, mount, location, length, *, prep_after=False):
        self.calls.append(("pick_up_tip", mount, round(location.z, 2), length))
        self._presence = "PRESENT"
        return "PRESENT"

    async def drop_tip(self, mount, location, *, home_after=False):
        self.calls.append(("drop_tip", mount, round(location.z, 2)))
        self._presence = "ABSENT"
        return "ABSENT"


# ---------------- pipette selection ----------------

@pytest.mark.parametrize(
    ("volume_ul", "mount", "cycles"),
    [
        (30.0, "right", 1),      # fits the p50: smaller max is the more accurate choice
        (800.0, "left", 1),      # only the p1000 can do it in one go
        (50.0, "right", 1),      # exactly the p50 maximum
        (2500.0, "left", 3),     # 2.5 mL exceeds every pipette, so it is split
    ],
)
def test_pipette_and_cycles_chosen_for_volume(volume_ul, mount, cycles):
    choice = uos.select_pipette(pipettes(P1000, P50), volume_ul)
    assert choice.mount == mount
    assert choice.cycles == cycles
    assert choice.cycles * choice.volume_per_cycle_ul == pytest.approx(volume_ul)


def test_cycles_are_equal_so_none_sits_near_the_minimum():
    choice = uos.select_pipette(pipettes(P1000, P50), 2500.0)
    assert choice.volume_per_cycle_ul == pytest.approx(833.333, abs=0.01)
    assert choice.volume_per_cycle_ul <= choice.max_volume_ul
    assert choice.volume_per_cycle_ul >= choice.min_volume_ul


def test_volume_below_every_minimum_is_refused():
    with pytest.raises(VolumeOutOfRangeError):
        uos.select_pipette(pipettes(P1000, P50), 0.5)


def test_zero_volume_is_refused():
    with pytest.raises(VolumeOutOfRangeError):
        uos.select_pipette(pipettes(P1000, P50), 0.0)


def test_no_pipette_attached_is_refused():
    with pytest.raises(PipetteSelectionError):
        uos.select_pipette([], 100.0)


# ---------------- move_liquid ----------------

async def test_millilitres_convert_to_microlitres():
    t = FakeTransport([P1000, P50], with_tip_on="right")
    result = await uos.move_liquid(
        t, volume_ml=0.03, source_labware_uri=PLATE, source_slot="D1", source_well="A1",
        destination_labware_uri=PLATE, destination_slot="C2", destination_well="B2",
    )
    assert result.volume_ul == pytest.approx(30.0)
    transfers = [c for c in t.calls if c[0] == "transfer"]
    assert len(transfers) == 1
    assert transfers[0][2] == pytest.approx(30.0)


async def test_large_volume_runs_one_transfer_per_cycle_on_one_tip():
    t = FakeTransport([P1000, P50], with_tip_on="left")
    result = await uos.move_liquid(
        t, volume_ml=2.5, source_labware_uri=PLATE, source_slot="D1", source_well="A1",
        destination_labware_uri=PLATE, destination_slot="C2", destination_well="A1",
    )
    transfers = [c for c in t.calls if c[0] == "transfer"]
    assert len(transfers) == result.pipette.cycles == 3
    assert all(c[2] == pytest.approx(833.333, abs=0.01) for c in transfers)
    # No tip handling inside move_liquid: that is a separate unit operation.
    assert not any(c[0] in {"pick_up_tip", "drop_tip"} for c in t.calls)


async def test_missing_tip_is_refused_before_any_transfer():
    t = FakeTransport([P1000, P50], with_tip_on=None)
    with pytest.raises(TipStateError):
        await uos.move_liquid(
            t, volume_ml=0.03, source_labware_uri=PLATE, source_slot="D1", source_well="A1",
            destination_labware_uri=PLATE, destination_slot="C2", destination_well="B2",
        )
    assert not any(c[0] == "transfer" for c in t.calls)


async def test_aspirate_sits_above_the_well_bottom_and_retract_above_the_rim():
    t = FakeTransport([P1000, P50], with_tip_on="right")
    await uos.move_liquid(
        t, volume_ml=0.03, source_labware_uri=PLATE, source_slot="D1", source_well="A1",
        destination_labware_uri=PLATE, destination_slot="C2", destination_well="B2",
    )
    bottom = geometry.well_point(PLATE, "D1", "A1")
    transfer = next(c for c in t.calls if c[0] == "transfer")
    assert transfer[3] == pytest.approx(bottom.z + uos.DEFAULT_ASPIRATE_CLEARANCE_MM, abs=0.01)


async def test_unknown_well_is_refused():
    t = FakeTransport([P1000, P50], with_tip_on="right")
    with pytest.raises(UnknownWellError):
        await uos.move_liquid(
            t, volume_ml=0.03, source_labware_uri=PLATE, source_slot="D1", source_well="Q99",
            destination_labware_uri=PLATE, destination_slot="C2", destination_well="B2",
        )


# ---------------- tips ----------------

def test_tip_length_comes_from_the_definition():
    assert uos.tip_length(TIPRACK_1000) == pytest.approx(95.6)
    assert uos.tip_length("opentrons/opentrons_flex_96_tiprack_50ul/1") == pytest.approx(57.9)


async def test_pick_up_tip_verifies_sensing():
    t = FakeTransport([P1000, P50], presence="ABSENT")
    result = await uos.pick_up_tip(t, mount="left", tiprack_uri=TIPRACK_1000, slot="A3", well="A1")
    assert result.tip_length_mm == pytest.approx(95.6)
    assert "pick_up_tip" in [c[0] for c in t.calls]


async def test_pick_up_tip_refuses_when_one_is_already_attached():
    t = FakeTransport([P1000, P50], presence="PRESENT")
    with pytest.raises(TipStateError):
        await uos.pick_up_tip(t, mount="left", tiprack_uri=TIPRACK_1000, slot="A3", well="A1")
    assert not any(c[0] == "pick_up_tip" for c in t.calls)


async def test_return_tip_requires_one_attached():
    t = FakeTransport([P1000, P50], presence="ABSENT")
    with pytest.raises(TipStateError):
        await uos.return_tip(t, mount="left", tiprack_uri=TIPRACK_1000, slot="A3", well="A1")
    assert not any(c[0] == "drop_tip" for c in t.calls)


async def test_return_tip_drops_and_confirms_gone():
    t = FakeTransport([P1000, P50], presence="PRESENT")
    result = await uos.return_tip(t, mount="left", tiprack_uri=TIPRACK_1000, slot="A3", well="A1")
    assert "PRESENT" not in result.presence
    assert any(c[0] == "drop_tip" for c in t.calls)
