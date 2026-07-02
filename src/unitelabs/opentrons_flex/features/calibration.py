"""
SiLA2 feature for Opentrons Flex automatic calibration.

The OT-2 connector's calibration feature wrote Smoothie EEPROM values (steps/mm,
endstop debounce, ...). The Flex has none of that. Instead it calibrates
*automatically*: a conductive calibration probe touches a known deck structure
and the contact position is measured. This feature wraps those routines
(``FlexCalibrationController`` → ``opentrons.hardware_control.ot3_calibration``).

Returns measured offsets as structured data — never a boolean "success" — and
raises defined errors (probe missing, calibration failed) the operator can act on.
"""

import enum
import asyncio
from dataclasses import dataclass

from opentrons.hardware_control.types import OT3Mount
from unitelabs.cdk import sila

from ..io import (
    CalibrationFailedError,
    CalibrationProbeNotAttachedError,
    FlexCalibrationController,
)
from ._progress import OperationPhase, OperationProgress, report_progress


class PipetteMount(enum.Enum):
    """A pipette mount eligible for calibration (the gripper calibrates separately)."""

    LEFT = "LEFT"
    RIGHT = "RIGHT"


class GripperJaw(enum.Enum):
    """Which gripper jaw probe to calibrate."""

    FRONT = "FRONT"
    REAR = "REAR"


@dataclass
class Offset:
    """A measured calibration offset in millimetres."""

    x: float
    y: float
    z: float


def _ot3_pipette_mount(mount: PipetteMount) -> OT3Mount:
    return OT3Mount.LEFT if mount is PipetteMount.LEFT else OT3Mount.RIGHT


class CalibrationFeature(sila.Feature):
    """SiLA2 feature for Flex automatic pipette, gripper-jaw, and deck calibration."""

    def __init__(self, controller: FlexCalibrationController):
        super().__init__(originator="ca.accelerationconsortium", category="robots")
        self._controller = controller

    @sila.ObservableCommand(errors=[CalibrationProbeNotAttachedError, CalibrationFailedError])
    async def calibrate_pipette(
        self,
        mount: PipetteMount,
        slot: int,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Offset:
        """
        Run automatic pipette-offset calibration.

        Requires the conductive calibration probe attached to the pipette nozzle.

        Args:
            mount: Pipette mount to calibrate.
            slot: Deck slot holding the calibration square.

        Yields:
            Update: Current calibration progress update.

        Returns:
            The measured pipette offset (x, y, z) in mm.
        """
        report_progress(
            status,
            intermediate,
            0.0,
            OperationPhase.STARTING,
            f"Starting {mount.value} pipette calibration.",
        )
        try:
            x, y, z = await self._controller.calibrate_pipette(_ot3_pipette_mount(mount), slot=slot)
        except asyncio.CancelledError:
            report_progress(
                status,
                intermediate,
                1.0,
                OperationPhase.CANCELLED,
                f"{mount.value} pipette calibration cancelled.",
            )
            raise
        report_progress(
            status,
            intermediate,
            1.0,
            OperationPhase.COMPLETED,
            f"{mount.value} pipette calibration completed.",
        )
        return Offset(x=x, y=y, z=z)

    @sila.ObservableCommand(errors=[CalibrationProbeNotAttachedError, CalibrationFailedError])
    async def calibrate_gripper_jaw(
        self,
        jaw: GripperJaw,
        slot: int,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Offset:
        """
        Run automatic gripper-jaw calibration for one jaw.

        Requires the calibration probe attached to the named gripper jaw.

        Args:
            jaw: Which jaw (FRONT or REAR) carries the probe.
            slot: Deck slot holding the calibration square.

        Yields:
            Update: Current calibration progress update.

        Returns:
            The measured jaw offset (x, y, z) in mm.
        """
        from opentrons.hardware_control.types import GripperProbe

        probe = GripperProbe.FRONT if jaw is GripperJaw.FRONT else GripperProbe.REAR
        report_progress(
            status,
            intermediate,
            0.0,
            OperationPhase.STARTING,
            f"Starting {jaw.value} gripper jaw calibration.",
        )
        try:
            x, y, z = await self._controller.calibrate_gripper_jaw(probe, slot=slot)
        except asyncio.CancelledError:
            report_progress(
                status,
                intermediate,
                1.0,
                OperationPhase.CANCELLED,
                f"{jaw.value} gripper jaw calibration cancelled.",
            )
            raise
        report_progress(
            status,
            intermediate,
            1.0,
            OperationPhase.COMPLETED,
            f"{jaw.value} gripper jaw calibration completed.",
        )
        return Offset(x=x, y=y, z=z)

    @sila.ObservableCommand(errors=[CalibrationProbeNotAttachedError, CalibrationFailedError])
    async def calibrate_deck(
        self,
        mount: PipetteMount,
        pipette_id: str,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> None:
        """
        Run automatic deck (belt) calibration using the pipette on ``mount``.

        Args:
            mount: Pipette mount whose attached pipette drives the routine.
            pipette_id: Serial of the pipette on that mount (from PipetteFeature).

        Yields:
            Update: Current calibration progress update.
        """
        report_progress(
            status,
            intermediate,
            0.0,
            OperationPhase.STARTING,
            f"Starting deck calibration with {mount.value}.",
        )
        try:
            await self._controller.calibrate_deck(_ot3_pipette_mount(mount), pipette_id=pipette_id)
        except asyncio.CancelledError:
            report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, "Deck calibration cancelled.")
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, "Deck calibration completed.")
