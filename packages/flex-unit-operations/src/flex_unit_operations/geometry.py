"""
Deck and labware geometry for Flex unit operations.

Every value here is read from the installed Opentrons definitions rather than
hardcoded, so a labware or deck revision is picked up by upgrading
``opentrons-shared-data`` instead of editing this module. Nothing in here touches
hardware: it computes the deck coordinates that the SiLA commands are then asked
to move to.
"""

from __future__ import annotations

import dataclasses
import functools
import re

from opentrons_shared_data.deck import load as load_deck
from opentrons_shared_data.labware import load_definition as load_labware_definition

from .errors import LabwareDefinitionError, UnknownSlotError, UnknownWellError

#: Deck model and schema of the Flex (OT-3 Standard). Slot origins and the gripper
#: drop offset are read from this definition.
DECK_NAME = "ot3_standard"
DECK_SCHEMA = 5

#: ``load_definition`` takes (load_name, version); labware is addressed here as
#: "namespace/load_name/version" the way the connector's plan config does.
_URI_PATTERN = re.compile(r"^(?P<namespace>[^/]+)/(?P<load_name>[^/]+)/(?P<version>\d+)$")


@dataclasses.dataclass(frozen=True)
class Point:
    """A deck coordinate in millimetres."""

    x: float
    y: float
    z: float

    def offset(self, *, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> Point:
        """Return a copy translated by the given deltas."""
        return Point(self.x + x, self.y + y, self.z + z)


@dataclasses.dataclass(frozen=True)
class GripGeometry:
    """Everything needed to grip one labware item in one slot."""

    #: Centre of the labware footprint, at the height the jaws should close.
    grip_point: Point
    #: Grip force in newtons, from the labware definition.
    force_newtons: float
    #: Release point, which the deck definition offsets slightly below the grip point.
    release_point: Point
    #: Height clear of all deck obstructions, used for travel between slots.
    travel_z: float


@functools.lru_cache(maxsize=1)
def _deck() -> dict:
    return load_deck(DECK_NAME, DECK_SCHEMA)


@functools.lru_cache(maxsize=1)
def slot_origins() -> dict[str, Point]:
    """
    Return the origin of every addressable deck slot.

    A slot's origin is its cutout position plus the addressable area's offset from
    that cutout, which is how the deck definition models the Flex's 164 x 107 mm grid.
    """
    deck = _deck()
    cutouts = {cutout["id"]: cutout["position"] for cutout in deck["locations"]["cutouts"]}
    origins: dict[str, Point] = {}
    for area in deck["locations"]["addressableAreas"]:
        if area.get("areaType") != "slot":
            continue
        cutout = cutouts.get(f"cutout{area['id']}")
        if cutout is None:
            continue
        offset = area["offsetFromCutoutFixture"]
        origins[area["id"]] = Point(cutout[0] + offset[0], cutout[1] + offset[1], cutout[2] + offset[2])
    return origins


def slot_origin(slot: str) -> Point:
    """Return one slot's origin, rejecting slots this deck does not have."""
    origins = slot_origins()
    try:
        return origins[slot]
    except KeyError:
        raise UnknownSlotError(slot, sorted(origins)) from None


@functools.lru_cache(maxsize=64)
def labware_definition(labware_uri: str) -> dict:
    """Load a labware definition addressed as "namespace/load_name/version"."""
    match = _URI_PATTERN.match(labware_uri)
    if match is None:
        msg = f"labware URI {labware_uri!r} is not of the form 'namespace/load_name/version'"
        raise LabwareDefinitionError(msg)
    try:
        return load_labware_definition(match["load_name"], int(match["version"]))
    except (OSError, ValueError, KeyError) as exc:
        msg = f"no installed labware definition for {labware_uri!r}: {exc}"
        raise LabwareDefinitionError(msg) from exc


def grip_geometry(labware_uri: str, slot: str, *, travel_z: float) -> GripGeometry:
    """
    Resolve where and how hard to grip ``labware_uri`` sitting in ``slot``.

    The jaws close on the centre of the labware footprint. Grip height comes from the
    definition's ``gripHeightFromLabwareBottom``, and force from its ``gripForce`` --
    both are per-labware values the Opentrons gripper is calibrated against, so they
    are never guessed here. Release happens at the deck definition's drop offset,
    which sits marginally below the grip point so the labware is set down rather
    than dropped.
    """
    definition = labware_definition(labware_uri)
    origin = slot_origin(slot)

    try:
        dimensions = definition["dimensions"]
        corner = definition["cornerOffsetFromSlot"]
        grip_height = float(definition["gripHeightFromLabwareBottom"])
        force = float(definition["gripForce"])
    except (KeyError, TypeError, ValueError) as exc:
        msg = (
            f"labware {labware_uri!r} does not carry the gripper parameters required to move it "
            f"(need dimensions, cornerOffsetFromSlot, gripHeightFromLabwareBottom, gripForce): {exc}"
        )
        raise LabwareDefinitionError(msg) from exc

    bottom_left = origin.offset(x=float(corner["x"]), y=float(corner["y"]), z=float(corner["z"]))
    centre = bottom_left.offset(
        x=float(dimensions["xDimension"]) / 2.0,
        y=float(dimensions["yDimension"]) / 2.0,
        z=grip_height,
    )

    drop = _deck()["gripperOffsets"]["default"]["dropOffset"]
    release = centre.offset(x=float(drop["x"]), y=float(drop["y"]), z=float(drop["z"]))

    return GripGeometry(grip_point=centre, force_newtons=force, release_point=release, travel_z=travel_z)


def well_point(labware_uri: str, slot: str, well: str) -> Point:
    """
    Return the deck coordinate of a well's bottom centre.

    Well coordinates in a labware definition are relative to the labware's own
    origin, so they are added to the slot origin and the labware's corner offset.
    """
    definition = labware_definition(labware_uri)
    wells = definition.get("wells", {})
    if well not in wells:
        raise UnknownWellError(well, labware_uri, sorted(wells))

    corner = definition["cornerOffsetFromSlot"]
    origin = slot_origin(slot)
    entry = wells[well]
    return origin.offset(
        x=float(corner["x"]) + float(entry["x"]),
        y=float(corner["y"]) + float(entry["y"]),
        z=float(corner["z"]) + float(entry["z"]),
    )


def well_top_z(labware_uri: str, well: str) -> float:
    """Return a well's depth above its bottom, for computing retract heights."""
    definition = labware_definition(labware_uri)
    wells = definition.get("wells", {})
    if well not in wells:
        raise UnknownWellError(well, labware_uri, sorted(wells))
    return float(wells[well]["depth"])
