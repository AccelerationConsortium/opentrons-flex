"""SiLA 2 controller for locally allowlisted Flex gripper movement plans."""

import asyncio
import enum
import typing
from dataclasses import dataclass

from unitelabs.cdk import sila
from unitelabs.cdk.sila import constraints

from ..io import (
    DestinationOccupiedError,
    FlexLabwareMovementController,
    GripActionError,
    GripperNotAttachedError,
    LabwareMovementNotAllowedError,
    LabwareNotPickedError,
    LabwareNotPlacedError,
    MachineErrorStateError,
    MovementOutOfBoundsError,
    NotHomedError,
    RunOwnershipError,
    StallDetectedError,
)
from ._progress import OperationPhase, OperationProgress, report_progress

_MILLIMETRE = constraints.Unit(
    "mm",
    [constraints.Unit.Component(constraints.Unit.SI.METER)],
    factor=0.001,
)
Millimetres = typing.Annotated[float, _MILLIMETRE]
PlanIdentifier = typing.Annotated[str, constraints.Pattern(r"^\S(?:.*\S)?$")]
LocationIdentifier = typing.Annotated[str, constraints.Pattern(r"^\S(?:.*\S)?$")]

_LABWARE_ERRORS = [
    RunOwnershipError,
    NotHomedError,
    MovementOutOfBoundsError,
    StallDetectedError,
    MachineErrorStateError,
    GripperNotAttachedError,
    GripActionError,
    LabwareMovementNotAllowedError,
    DestinationOccupiedError,
    LabwareNotPickedError,
    LabwareNotPlacedError,
]


class PlacementState(enum.Enum):
    """Terminal state of a completed movement command."""

    COMPLETED = "COMPLETED"


@dataclass
class LabwarePosition:
    """Final gripper critical-point coordinate in deck millimetres."""

    x: Millimetres
    y: Millimetres
    z: Millimetres


@dataclass
class LabwareMovementResult:
    """Terminal command state and physical gripper reading."""

    plan_identifier: PlanIdentifier
    labware_identifier: LocationIdentifier
    final_location: LocationIdentifier
    state: PlacementState
    final_gripper_position: LabwarePosition
    jaw_width: Millimetres


@dataclass
class LabwarePlanSummary:
    """Discoverable non-geometric metadata for one local movement plan."""

    plan_identifier: PlanIdentifier
    labware_identifier: LocationIdentifier
    source_location: LocationIdentifier
    destination_location: LocationIdentifier
    is_lid: bool


@dataclass
class OccupiedLocation:
    """One durable location-to-labware identity entry."""

    location_identifier: LocationIdentifier
    labware_identifier: LocationIdentifier


@dataclass
class LabwareDeckState:
    """Read-only durable deck-state snapshot."""

    valid: bool
    occupied_locations: list[OccupiedLocation]


class LabwareMovementController(sila.Feature):
    """Execute server-provisioned labware and lid plans with pickup verification."""

    def __init__(self, controller: FlexLabwareMovementController) -> None:
        super().__init__(originator="ca.accelerationconsortium", category="robots", version="1.0")
        self._controller = controller

    @sila.ObservableCommand(errors=_LABWARE_ERRORS)
    async def move_labware(
        self,
        plan_identifier: PlanIdentifier,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> LabwareMovementResult:
        """Execute one locally provisioned non-lid movement plan."""
        return await self._move(plan_identifier, expect_lid=False, status=status, intermediate=intermediate)

    @sila.ObservableCommand(errors=_LABWARE_ERRORS)
    async def move_lid(
        self,
        plan_identifier: PlanIdentifier,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> LabwareMovementResult:
        """Execute one locally provisioned lid movement plan."""
        return await self._move(plan_identifier, expect_lid=True, status=status, intermediate=intermediate)

    @sila.UnobservableProperty()
    def available_plans(self) -> list[LabwarePlanSummary]:
        """Return identifiers and routing metadata for locally provisioned plans."""
        return [
            LabwarePlanSummary(
                plan_identifier=plan.identifier,
                labware_identifier=plan.labware_identifier,
                source_location=plan.source_identifier,
                destination_location=plan.destination_identifier,
                is_lid=plan.is_lid,
            )
            for plan in self._controller.available_plans
        ]

    @sila.UnobservableProperty()
    def deck_state(self) -> LabwareDeckState:
        """Return durable state validity and location-to-labware identity."""
        valid, occupancy = self._controller.deck_state
        return LabwareDeckState(
            valid=valid,
            occupied_locations=[
                OccupiedLocation(location_identifier=location, labware_identifier=labware)
                for location, labware in sorted(occupancy.items())
            ],
        )

    async def _move(
        self,
        plan_identifier: str,
        *,
        expect_lid: bool,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> LabwareMovementResult:
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, "Validating allowlisted movement plan.")
        report_progress(status, intermediate, 0.1, OperationPhase.RUNNING, "Picking and placing with the gripper.")
        try:
            plan, result = await self._controller.move_labware(plan_identifier, expect_lid=expect_lid)
        except asyncio.CancelledError:
            report_progress(
                status,
                intermediate,
                1.0,
                OperationPhase.CANCELLED,
                "Labware movement cancelled; reconcile the deck, fully home, then run HomeJaw.",
            )
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, "Labware movement command completed.")
        return LabwareMovementResult(
            plan_identifier=plan.identifier,
            labware_identifier=plan.labware_identifier,
            final_location=plan.destination_identifier,
            state=PlacementState.COMPLETED,
            final_gripper_position=LabwarePosition(
                result.final_position.x,
                result.final_position.y,
                result.final_position.z,
            ),
            jaw_width=result.jaw_width,
        )


__all__ = [
    "LabwareDeckState",
    "LabwareMovementController",
    "LabwareMovementResult",
    "LabwarePlanSummary",
    "LabwarePosition",
    "OccupiedLocation",
    "PlacementState",
]
