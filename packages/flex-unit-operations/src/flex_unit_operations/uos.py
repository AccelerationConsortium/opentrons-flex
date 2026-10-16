"""
Unit operations for the Opentrons Flex.

Each unit operation is a client-side orchestration of SiLA commands on a running
connector. None of them touch hardware directly: geometry is computed from the
installed Opentrons definitions and every physical action is a SiLA call.
"""

from __future__ import annotations

import dataclasses
import math

from . import geometry
from .errors import (
    DirectGripperUnavailableError,
    GripperNotReadyError,
    LabwareDefinitionError,
    MachineNotReadyError,
    PipetteSelectionError,
    SameSlotError,
    TipStateError,
    VolumeOutOfRangeError,
)
from .geometry import Point
from .transport import GRIPPER_MOUNT, FlexTransport

#: Height the gripper travels at between slots. Above the tallest deck fixture but
#: inside the gantry's Z envelope.
DEFAULT_TRAVEL_Z = 200.0

#: Jaw opening in millimetres commanded when releasing labware.
UNGRIP_FORCE_N = 15.0

#: Default heights relative to a well, in millimetres: just off the bottom for
#: aspirating and dispensing, and clear of the rim when travelling.
DEFAULT_ASPIRATE_CLEARANCE_MM = 2.0
DEFAULT_DISPENSE_CLEARANCE_MM = 2.0
DEFAULT_RETRACT_CLEARANCE_MM = 10.0

_DISABLED_MARKER = "Direct gripper operation"


@dataclasses.dataclass(frozen=True)
class MovePlateResult:
    """What a completed plate move did."""

    labware_uri: str
    source_slot: str
    destination_slot: str
    grip_point: Point
    release_point: Point
    force_newtons: float
    jaw_width_after_grip_mm: float


async def require_machine_ready(transport: FlexTransport) -> dict:
    """
    Refuse to command motion unless the robot is in a safe state.

    Checks the aggregate machine status rather than individual axes: an engaged E-stop,
    an open door or a latched error state all make motion either impossible or unsafe,
    and the connector would reject the move anyway with a less obvious message.
    """
    status = dict(await transport.machine_status())  # type: ignore[arg-type]
    estop = str(status.get("Estop", "")).upper()
    if estop != "DISENGAGED":
        msg = f"E-stop is {estop or 'unknown'}; release it before commanding motion"
        raise MachineNotReadyError(msg)
    if status.get("DoorOpen"):
        msg = "the robot door is open; close it before commanding motion"
        raise MachineNotReadyError(msg)
    if status.get("IsErrorState"):
        msg = f"the robot is in an error state: {status.get('Message') or 'no detail reported'}"
        raise MachineNotReadyError(msg)
    return status


async def require_gripper_ready(transport: FlexTransport, *, home_jaw_if_unhomed: bool = True) -> dict:
    """
    Ensure a gripper is attached and its jaw has a known position.

    A freshly started or restarted connector reports the jaw as ``UNHOMED``, and grip
    commands are refused until it is homed, so this homes it once rather than failing
    the whole operation for a recoverable reason.
    """
    status = dict(await transport.gripper_status())  # type: ignore[arg-type]
    if not status.get("Attached"):
        msg = "no gripper is attached; a plate move needs the Flex gripper installed"
        raise GripperNotReadyError(msg)
    if "UNHOMED" in str(status.get("State", "")).upper():
        if not home_jaw_if_unhomed:
            msg = f"gripper jaw is {status.get('State')}; home it before gripping"
            raise GripperNotReadyError(msg)
        await transport.home_jaw()
        status = dict(await transport.gripper_status())  # type: ignore[arg-type]
    return status


async def move_plate(
    transport: FlexTransport,
    *,
    labware_uri: str,
    source_slot: str,
    destination_slot: str,
    travel_z: float = DEFAULT_TRAVEL_Z,
) -> MovePlateResult:
    """
    Move a well plate from one deck slot to another using the Flex gripper.

    Grip point, grip force and release height come from the labware and deck
    definitions, so any slot pair works without pre-provisioned movement plans. The
    move is: travel high above the source, descend, grip, lift, traverse at travel
    height, descend at the destination, release, and retract.

    Args:
        transport: A connected SiLA client for the Flex connector.
        labware_uri: Labware addressed as "namespace/load_name/version".
        source_slot: Deck slot the plate currently occupies, e.g. "D1".
        destination_slot: Deck slot to move it to, e.g. "C2".
        travel_z: Height to traverse at, clear of deck obstructions.

    Returns:
        A :class:`MovePlateResult` recording the geometry used and the jaw width
        observed while the plate was held.

    Raises:
        SameSlotError: Source and destination are the same slot.
        MachineNotReadyError: E-stop engaged, door open, or a latched error state.
        GripperNotReadyError: No gripper attached, or the grip closed on nothing.
        DirectGripperUnavailableError: The connector has a local plan registry
            configured, which disables raw gripper control.
        LabwareDefinitionError: The labware has no gripper parameters.
        UnknownSlotError: A slot was named that this deck does not have.
    """
    if source_slot == destination_slot:
        raise SameSlotError(source_slot)

    source = geometry.grip_geometry(labware_uri, source_slot, travel_z=travel_z)
    destination = geometry.grip_geometry(labware_uri, destination_slot, travel_z=travel_z)

    await require_machine_ready(transport)
    await require_gripper_ready(transport)

    # Home the gripper mount so the gantry has a known position to move from; the
    # connector rejects absolute moves while an axis position is unknown.
    await transport.home_mount(GRIPPER_MOUNT)

    above_source = source.grip_point.offset(z=travel_z - source.grip_point.z)
    above_destination = destination.grip_point.offset(z=travel_z - destination.grip_point.z)

    try:
        await transport.move_to(GRIPPER_MOUNT, above_source)
        await transport.move_to(GRIPPER_MOUNT, source.grip_point)
        await transport.grip(source.force_newtons)
    except Exception as exc:
        if _DISABLED_MARKER in str(exc):
            raise DirectGripperUnavailableError(str(exc)) from exc
        raise

    jaw_width = await transport.jaw_width()

    await transport.move_to(GRIPPER_MOUNT, above_source)
    await transport.move_to(GRIPPER_MOUNT, above_destination)
    await transport.move_to(GRIPPER_MOUNT, destination.release_point)
    await transport.ungrip(UNGRIP_FORCE_N)
    await transport.move_to(GRIPPER_MOUNT, above_destination)

    return MovePlateResult(
        labware_uri=labware_uri,
        source_slot=source_slot,
        destination_slot=destination_slot,
        grip_point=source.grip_point,
        release_point=destination.release_point,
        force_newtons=source.force_newtons,
        jaw_width_after_grip_mm=jaw_width,
    )


@dataclasses.dataclass(frozen=True)
class PipetteChoice:
    """The pipette selected for a volume, and how the volume is split."""

    mount: str
    model: str
    min_volume_ul: float
    max_volume_ul: float
    cycles: int
    volume_per_cycle_ul: float


@dataclasses.dataclass(frozen=True)
class MoveLiquidResult:
    """What a completed liquid move did."""

    pipette: PipetteChoice
    volume_ml: float
    volume_ul: float
    source_slot: str
    source_well: str
    destination_slot: str
    destination_well: str


def select_pipette(pipettes: list[dict], volume_ul: float) -> PipetteChoice:
    """
    Choose a pipette for ``volume_ul`` and split it into cycles.

    Prefers the smallest-capacity pipette that can do it in one aspirate, because a
    volume near a pipette's maximum is measured more accurately than the same volume on
    a much larger one. When the volume exceeds every pipette, the largest is used and
    the volume is divided into equal cycles rather than uneven ones, so no cycle sits
    near the minimum where accuracy is worst.

    Raises:
        PipetteSelectionError: No pipette is attached.
        VolumeOutOfRangeError: The volume is below every attached pipette's minimum.
    """
    usable = [p for p in pipettes if p.get("Attached") and float(p.get("MaxVolume") or 0.0) > 0.0]
    if not usable:
        msg = "no pipette is attached; a liquid move needs at least one"
        raise PipetteSelectionError(msg)

    if volume_ul <= 0.0:
        msg = f"volume must be greater than zero, got {volume_ul} uL"
        raise VolumeOutOfRangeError(msg)

    single = [p for p in usable if float(p["MinVolume"]) <= volume_ul <= float(p["MaxVolume"])]
    if single:
        chosen = min(single, key=lambda p: float(p["MaxVolume"]))
        cycles = 1
    else:
        largest = max(usable, key=lambda p: float(p["MaxVolume"]))
        if volume_ul < float(largest["MinVolume"]):
            smallest_min = min(float(p["MinVolume"]) for p in usable)
            msg = (
                f"{volume_ul} uL is below the minimum any attached pipette can measure "
                f"({smallest_min} uL)"
            )
            raise VolumeOutOfRangeError(msg)
        chosen = largest
        cycles = math.ceil(volume_ul / float(chosen["MaxVolume"]))

    per_cycle = volume_ul / cycles
    if per_cycle < float(chosen["MinVolume"]):
        msg = (
            f"splitting {volume_ul} uL into {cycles} cycles gives {per_cycle:.2f} uL per cycle, "
            f"below the {chosen['Model']} minimum of {chosen['MinVolume']} uL"
        )
        raise VolumeOutOfRangeError(msg)

    return PipetteChoice(
        mount=str(chosen["Mount"]),
        model=str(chosen["Model"]),
        min_volume_ul=float(chosen["MinVolume"]),
        max_volume_ul=float(chosen["MaxVolume"]),
        cycles=cycles,
        volume_per_cycle_ul=per_cycle,
    )


async def move_liquid(
    transport: FlexTransport,
    *,
    volume_ml: float,
    source_labware_uri: str,
    source_slot: str,
    source_well: str,
    destination_labware_uri: str,
    destination_slot: str,
    destination_well: str,
    aspirate_clearance_mm: float = DEFAULT_ASPIRATE_CLEARANCE_MM,
    dispense_clearance_mm: float = DEFAULT_DISPENSE_CLEARANCE_MM,
    retract_clearance_mm: float = DEFAULT_RETRACT_CLEARANCE_MM,
) -> MoveLiquidResult:
    """
    Move liquid from one well to another.

    Requires a tip to be attached already: tip handling is its own unit operation, so
    this neither picks up nor drops one. Volumes are given in millilitres for the
    caller's convenience and converted once to the microlitres the connector expects.
    A volume larger than the chosen pipette can hold is split into equal cycles, all
    using the same tip.

    Args:
        transport: A connected SiLA client for the Flex connector.
        volume_ml: Volume to move, in millilitres.
        source_labware_uri: Source labware as "namespace/load_name/version".
        source_slot: Deck slot holding the source labware.
        source_well: Well to aspirate from, e.g. "A1".
        destination_labware_uri: Destination labware URI.
        destination_slot: Deck slot holding the destination labware.
        destination_well: Well to dispense into.
        aspirate_clearance_mm: Height above the source well bottom to aspirate at.
        dispense_clearance_mm: Height above the destination well bottom to dispense at.
        retract_clearance_mm: Height above each well's top to retract to.

    Returns:
        A :class:`MoveLiquidResult` recording the pipette chosen and the cycles run.

    Raises:
        MachineNotReadyError: E-stop engaged, door open, or a latched error state.
        TipStateError: No tip is attached on the selected mount.
        PipetteSelectionError: No pipette is attached.
        VolumeOutOfRangeError: The volume cannot be delivered by any attached pipette.
        UnknownWellError: A well was named that the labware does not contain.
    """
    volume_ul = volume_ml * 1000.0

    await require_machine_ready(transport)

    pipettes = [dict(p) for p in (await transport.attached_pipettes()) or []]  # type: ignore[union-attr]
    choice = select_pipette(pipettes, volume_ul)

    if not any(p["Mount"] == choice.mount and p.get("HasTip") for p in pipettes):
        msg = (
            f"no tip on the {choice.mount} mount; run the pick_up_tip unit operation before "
            f"moving liquid"
        )
        raise TipStateError(msg)

    source = geometry.well_point(source_labware_uri, source_slot, source_well)
    destination = geometry.well_point(destination_labware_uri, destination_slot, destination_well)
    source_depth = geometry.well_top_z(source_labware_uri, source_well)
    destination_depth = geometry.well_top_z(destination_labware_uri, destination_well)

    aspirate_at = source.offset(z=aspirate_clearance_mm)
    dispense_at = destination.offset(z=dispense_clearance_mm)
    source_retract = source.offset(z=source_depth + retract_clearance_mm)
    destination_retract = destination.offset(z=destination_depth + retract_clearance_mm)

    for _ in range(choice.cycles):
        await transport.transfer(
            choice.mount,
            source=aspirate_at,
            source_retract=source_retract,
            destination=dispense_at,
            destination_retract=destination_retract,
            volume_ul=choice.volume_per_cycle_ul,
        )

    return MoveLiquidResult(
        pipette=choice,
        volume_ml=volume_ml,
        volume_ul=volume_ul,
        source_slot=source_slot,
        source_well=source_well,
        destination_slot=destination_slot,
        destination_well=destination_well,
    )


@dataclasses.dataclass(frozen=True)
class TipResult:
    """The outcome of a tip operation."""

    mount: str
    tiprack_uri: str
    slot: str
    well: str
    tip_length_mm: float
    presence: str


def tip_length(tiprack_uri: str) -> float:
    """
    Return a tiprack's tip length in millimetres.

    Read from the definition's ``parameters.tipLength`` rather than passed in, so a
    1000 uL rack (95.6 mm) and a 50 uL rack (57.9 mm) are handled without the caller
    tracking the difference.

    Raises:
        LabwareDefinitionError: The labware is not a tiprack, or has no tip length.
    """
    definition = geometry.labware_definition(tiprack_uri)
    parameters = definition.get("parameters", {})
    if not parameters.get("isTiprack"):
        msg = f"labware {tiprack_uri!r} is not a tiprack"
        raise LabwareDefinitionError(msg)
    length = parameters.get("tipLength")
    if not length:
        msg = f"tiprack {tiprack_uri!r} has no parameters.tipLength"
        raise LabwareDefinitionError(msg)
    return float(length)


async def pick_up_tip(
    transport: FlexTransport,
    *,
    mount: str,
    tiprack_uri: str,
    slot: str,
    well: str,
) -> TipResult:
    """
    Pick up a tip from a tiprack well.

    A separate unit operation from the liquid move, so the caller decides when tips are
    changed rather than having a policy imposed per transfer.

    Raises:
        MachineNotReadyError: The robot is not in a state where motion is safe.
        TipStateError: A tip is already attached, or none was sensed afterwards.
    """
    await require_machine_ready(transport)

    presence = str(await transport.tip_presence(mount) or "")
    if "PRESENT" in presence.upper():
        msg = f"a tip is already attached on the {mount} mount; drop or return it first"
        raise TipStateError(msg)

    length = tip_length(tiprack_uri)
    location = geometry.well_point(tiprack_uri, slot, well)
    result = str(await transport.pick_up_tip(mount, location, length) or "")
    if "PRESENT" not in result.upper():
        msg = f"tip pickup on {mount} did not sense a tip afterwards (reported {result or 'nothing'})"
        raise TipStateError(msg)

    return TipResult(mount=mount, tiprack_uri=tiprack_uri, slot=slot, well=well, tip_length_mm=length, presence=result)


async def return_tip(
    transport: FlexTransport,
    *,
    mount: str,
    tiprack_uri: str,
    slot: str,
    well: str,
) -> TipResult:
    """
    Return the attached tip to a tiprack well so it can be reused.

    Separate from :func:`discard_tip` because reuse and disposal are different
    decisions with different contamination consequences; the caller picks.

    Raises:
        TipStateError: No tip is attached, or one is still sensed afterwards.
    """
    return await _release_tip(
        transport,
        mount=mount,
        labware_uri=tiprack_uri,
        slot=slot,
        well=well,
        z_offset=tip_length(tiprack_uri),
    )


async def discard_tip(
    transport: FlexTransport,
    *,
    mount: str,
    trash_uri: str,
    slot: str,
    well: str = "A1",
) -> TipResult:
    """
    Drop the attached tip into a trash container.

    Raises:
        TipStateError: No tip is attached, or one is still sensed afterwards.
    """
    definition = geometry.labware_definition(trash_uri)
    height = float(definition.get("dimensions", {}).get("zDimension") or 0.0)
    return await _release_tip(
        transport,
        mount=mount,
        labware_uri=trash_uri,
        slot=slot,
        well=well,
        z_offset=height,
    )


async def _release_tip(
    transport: FlexTransport,
    *,
    mount: str,
    labware_uri: str,
    slot: str,
    well: str,
    z_offset: float,
) -> TipResult:
    """Drop a tip at a well, verifying it was attached first and gone afterwards."""
    await require_machine_ready(transport)

    presence = str(await transport.tip_presence(mount) or "")
    if "PRESENT" not in presence.upper():
        msg = f"no tip attached on the {mount} mount to release (reported {presence or 'nothing'})"
        raise TipStateError(msg)

    location = geometry.well_point(labware_uri, slot, well).offset(z=z_offset)
    result = str(await transport.drop_tip(mount, location) or "")
    if "PRESENT" in result.upper():
        msg = f"tip is still sensed on {mount} after the drop (reported {result})"
        raise TipStateError(msg)

    return TipResult(
        mount=mount,
        tiprack_uri=labware_uri,
        slot=slot,
        well=well,
        tip_length_mm=z_offset,
        presence=result,
    )
