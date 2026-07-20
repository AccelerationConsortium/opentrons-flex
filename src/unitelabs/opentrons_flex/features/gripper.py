"""
SiLA2 feature for the Opentrons Flex gripper.

Flex-only instrument: grips and releases labware for on-deck moves. Backed by
``FlexGripperController`` (a wrapper around ``OT3API`` gripper methods).

Grip and release actions are mechanical movements, so they are observable
commands with status updates and intermediate progress responses.
"""

import asyncio
import typing
from dataclasses import dataclass

from unitelabs.cdk import sila
from unitelabs.cdk.sila import constraints

from ..io import (
    DirectGripperControlDisabledError,
    FlexGripperController,
    GripActionError,
    GripperNotAttachedError,
    NotHomedError,
)
from ._progress import OperationPhase, OperationProgress, report_progress

# Flex gripper grip force range in Newtons (documented operating envelope).
_MIN_FORCE_N = 5.0
_MAX_FORCE_N = 25.0
_GripForce = typing.Annotated[
    float,
    constraints.MinimalInclusive(_MIN_FORCE_N),
    constraints.MaximalInclusive(_MAX_FORCE_N),
    constraints.Unit(
        "N",
        [
            constraints.Unit.Component(constraints.Unit.SI.KILOGRAM),
            constraints.Unit.Component(constraints.Unit.SI.METER),
            constraints.Unit.Component(constraints.Unit.SI.SECOND, exponent=-2),
        ],
    ),
]
_JawWidth = typing.Annotated[
    float,
    constraints.MinimalInclusive(0.0),
    constraints.Unit(
        "mm",
        [constraints.Unit.Component(constraints.Unit.SI.METER)],
        factor=0.001,
    ),
]


@dataclass
class GripperStatus:
    """Current gripper attachment and identity."""

    attached: bool
    model: str
    gripper_id: str
    state: str


class GripperFeature(sila.Feature):
    """SiLA2 feature for the Flex gripper: grip, ungrip, and home the jaw."""

    def __init__(self, controller: FlexGripperController):
        super().__init__(
            originator="ca.accelerationconsortium",
            category="robots",
            identifier="GripperController",
            name="Gripper Controller",
            version="1.2",
        )
        self._controller = controller

    @sila.ObservableCommand(
        errors=[GripperNotAttachedError, GripActionError, NotHomedError, DirectGripperControlDisabledError]
    )
    async def grip(
        self,
        force: _GripForce,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> None:
        """
        Close the jaw to grip labware.

        Args:
            force: Grip force in Newtons (5-25 N).

        Yields:
            Update: Current gripper progress update.
        """
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, "Starting gripper grip.")
        try:
            await self._controller.grip(force_newtons=force)
        except asyncio.CancelledError:
            report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, "Gripper grip cancelled.")
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, "Gripper grip completed.")

    @sila.ObservableCommand(
        errors=[GripperNotAttachedError, GripActionError, NotHomedError, DirectGripperControlDisabledError]
    )
    async def ungrip(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> None:
        """
        Open the jaw fully to release labware.

        Yields:
            Update: Current gripper progress update.
        """
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, "Starting gripper release.")
        try:
            await self._controller.ungrip()
        except asyncio.CancelledError:
            report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, "Gripper release cancelled.")
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, "Gripper release completed.")

    @sila.ObservableCommand(errors=[GripperNotAttachedError, GripActionError, NotHomedError])
    async def home_jaw(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> None:
        """
        Home the gripper jaw to its reference position.

        Yields:
            Update: Current gripper progress update.
        """
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, "Starting gripper jaw home.")
        try:
            await self._controller.home_jaw()
        except asyncio.CancelledError:
            report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, "Gripper jaw home cancelled.")
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, "Gripper jaw home completed.")

    @sila.UnobservableProperty()
    def status(self) -> GripperStatus:
        """Gripper attachment status and identity."""
        info = self._controller.info
        if not info:
            return GripperStatus(attached=False, model="", gripper_id="", state="")
        return GripperStatus(
            attached=True,
            model=str(info.get("model") or ""),
            gripper_id=str(info.get("gripper_id") or ""),
            state=str(info.get("state") or ""),
        )

    @sila.UnobservableProperty(errors=[GripperNotAttachedError])
    def jaw_width(self) -> _JawWidth:
        """Return the current sensor-estimated jaw width in millimetres."""
        return self._controller.jaw_width
