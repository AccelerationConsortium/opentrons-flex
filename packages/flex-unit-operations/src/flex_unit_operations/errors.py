"""
Errors raised by Flex unit operations.

These describe faults the caller can act on: bad addressing, an unsafe machine
state, or a request the hardware cannot satisfy. Faults raised by the connector
itself (pipette not attached, volume out of range, gripper missing) arrive as SiLA
execution errors and are re-raised as :class:`ConnectorError` with the connector's
own message preserved.
"""

from __future__ import annotations


class UnitOperationError(RuntimeError):
    """Base class for every unit-operation failure."""


class ConnectorError(UnitOperationError):
    """A SiLA command was rejected by the connector."""


class UnknownSlotError(UnitOperationError):
    """A deck slot was named that this deck does not have."""

    def __init__(self, slot: str, known: list[str]) -> None:
        super().__init__(f"unknown deck slot {slot!r}; this deck has {', '.join(known)}")
        self.slot = slot
        self.known = known


class UnknownWellError(UnitOperationError):
    """A well was named that the labware definition does not contain."""

    def __init__(self, well: str, labware_uri: str, known: list[str]) -> None:
        preview = ", ".join(known[:12])
        suffix = ", ..." if len(known) > 12 else ""
        super().__init__(f"labware {labware_uri!r} has no well {well!r}; it has {preview}{suffix}")
        self.well = well
        self.labware_uri = labware_uri


class LabwareDefinitionError(UnitOperationError):
    """A labware definition is missing, unreadable, or lacks gripper parameters."""


class SameSlotError(UnitOperationError):
    """A plate move was requested from a slot to itself."""

    def __init__(self, slot: str) -> None:
        super().__init__(f"source and destination are both {slot!r}; nothing to move")
        self.slot = slot


class MachineNotReadyError(UnitOperationError):
    """The robot is not in a state where motion may be commanded."""


class GripperNotReadyError(UnitOperationError):
    """The gripper is absent, or its jaw has not been homed."""


class DirectGripperUnavailableError(UnitOperationError):
    """
    The connector refuses direct gripper control.

    Raised when ``labware_movement_config`` is set on the connector: with a local plan
    registry configured, raw grip and ungrip are rejected so that every move goes
    through the allowlisted, ledger-tracked path instead. Either drive
    ``LabwareMovementController`` with a provisioned plan identifier, or run the
    connector with ``labware_movement_config`` null.
    """


class TipStateError(UnitOperationError):
    """A tip was attached when none was expected, or absent when one was required."""


class VolumeOutOfRangeError(UnitOperationError):
    """No attached pipette can deliver the requested volume."""


class PipetteSelectionError(UnitOperationError):
    """No attached pipette is usable for the requested operation."""
