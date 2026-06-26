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
from dataclasses import dataclass

from opentrons.hardware_control.types import OT3Mount
from unitelabs.cdk import sila

from ..io import (
    CalibrationFailedError,
    CalibrationProbeNotAttachedError,
    FlexCalibrationController,
)
from ..io.calibration import DEFAULT_CALIBRATION_SLOT


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

    @sila.UnobservableCommand(errors=[CalibrationProbeNotAttachedError, CalibrationFailedError])
    async def calibrate_pipette(self, mount: PipetteMount, slot: int = DEFAULT_CALIBRATION_SLOT) -> Offset:
        """
        Run automatic pipette-offset calibration.

        Requires the conductive calibration probe attached to the pipette nozzle.

        Args:
            mount: Pipette mount to calibrate.
            slot: Deck slot holding the calibration square (default: centre slot 5).

        Returns:
            The measured pipette offset (x, y, z) in mm.
        """
        x, y, z = await self._controller.calibrate_pipette(_ot3_pipette_mount(mount), slot=slot)
        return Offset(x=x, y=y, z=z)

    @sila.UnobservableCommand(errors=[CalibrationProbeNotAttachedError, CalibrationFailedError])
    async def calibrate_gripper_jaw(self, jaw: GripperJaw, slot: int = DEFAULT_CALIBRATION_SLOT) -> Offset:
        """
        Run automatic gripper-jaw calibration for one jaw.

        Requires the calibration probe attached to the named gripper jaw.

        Args:
            jaw: Which jaw (FRONT or REAR) carries the probe.
            slot: Deck slot holding the calibration square (default: centre slot 5).

        Returns:
            The measured jaw offset (x, y, z) in mm.
        """
        from opentrons.hardware_control.types import GripperProbe

        probe = GripperProbe.FRONT if jaw is GripperJaw.FRONT else GripperProbe.REAR
        x, y, z = await self._controller.calibrate_gripper_jaw(probe, slot=slot)
        return Offset(x=x, y=y, z=z)

    @sila.UnobservableCommand(errors=[CalibrationProbeNotAttachedError, CalibrationFailedError])
    async def calibrate_deck(self, mount: PipetteMount, pipette_id: str) -> None:
        """
        Run automatic deck (belt) calibration using the pipette on ``mount``.

        Args:
            mount: Pipette mount whose attached pipette drives the routine.
            pipette_id: Serial of the pipette on that mount (from PipetteFeature).
        """
        await self._controller.calibrate_deck(_ot3_pipette_mount(mount), pipette_id=pipette_id)
