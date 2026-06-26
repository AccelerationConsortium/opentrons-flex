"""
IO wrapper for Opentrons Flex automatic calibration.

The OT-2 "calibration" surface wrote Smoothie config values (steps/mm, endstop
debounce, etc.). The Flex has no Smoothie and no such config — it calibrates
automatically by touching a conductive calibration probe to a known structure
and measuring the contact position. These routines live in
``opentrons.hardware_control.ot3_calibration`` and operate on the shared
``OT3API``.

This controller wraps those routines under the shared hardware lock and
translates expected failure modes into defined errors the SiLA feature exposes.
"""

import asyncio
import logging

from opentrons.hardware_control import HardwareControlAPI
from opentrons.hardware_control.types import OT3Mount

from .hardware_proxy import _TimedLock

log = logging.getLogger(__name__)

# Default Flex calibration slot (deck slot 5 / "C2") used by the Opentrons routines.
DEFAULT_CALIBRATION_SLOT = 5


class CalibrationProbeNotAttachedError(Exception):
    """The calibration probe is required but not attached.

    Attach the conductive calibration probe to the pipette/gripper before calibrating.
    """


class CalibrationFailedError(Exception):
    """An automatic calibration routine failed to find or verify a position.

    The message includes the underlying hardware/geometry error so the deviation
    and probe coordinates are available for troubleshooting.
    """


class FlexCalibrationController:
    """Wraps ``ot3_calibration`` routines for the Flex under a shared lock."""

    def __init__(
        self,
        api: HardwareControlAPI,
        lock: asyncio.Lock | None = None,
        lock_timeout_s: float | None = None,
    ) -> None:
        self._api = api
        raw_lock = lock if lock is not None else asyncio.Lock()
        self._lock: _TimedLock = _TimedLock(raw_lock, lock_timeout_s)

    @classmethod
    def from_api(
        cls,
        api: HardwareControlAPI,
        lock: asyncio.Lock,
        lock_timeout_s: float | None = None,
    ) -> "FlexCalibrationController":
        """Share an already-built API and lock (in-process robot-server mode)."""
        return cls(api=api, lock=lock, lock_timeout_s=lock_timeout_s)

    async def calibrate_pipette(self, mount: OT3Mount, slot: int = DEFAULT_CALIBRATION_SLOT) -> tuple[float, float, float]:
        """
        Run automatic pipette-offset calibration for ``mount``.

        Returns the measured pipette offset as an ``(x, y, z)`` tuple in mm.
        """
        from opentrons.hardware_control import ot3_calibration as cal

        async with self._lock:
            try:
                offset = await cal.calibrate_pipette(self._api, mount=mount, slot=slot)
            except Exception as exc:  # noqa: BLE001 — re-raised as a defined error below
                raise self._translate(exc) from exc
        return (offset.x, offset.y, offset.z)

    async def calibrate_gripper_jaw(self, probe, slot: int = DEFAULT_CALIBRATION_SLOT) -> tuple[float, float, float]:
        """
        Run gripper-jaw calibration for one jaw probe.

        ``probe`` is an ``opentrons.hardware_control.types.GripperProbe`` member.
        Returns the measured jaw offset as ``(x, y, z)`` mm.
        """
        from opentrons.hardware_control import ot3_calibration as cal

        async with self._lock:
            try:
                offset = await cal.calibrate_gripper_jaw(self._api, probe=probe, slot=slot)
            except Exception as exc:  # noqa: BLE001
                raise self._translate(exc) from exc
        return (offset.x, offset.y, offset.z)

    async def calibrate_deck(self, mount: OT3Mount, pipette_id: str) -> None:
        """Run automatic deck (belt) calibration using the pipette on ``mount``."""
        from opentrons.hardware_control import ot3_calibration as cal

        async with self._lock:
            try:
                await cal.calibrate_belts(self._api, mount=mount, pipette_id=pipette_id)
            except Exception as exc:  # noqa: BLE001
                raise self._translate(exc) from exc

    @staticmethod
    def _translate(exc: Exception) -> Exception:
        """Map opentrons calibration exceptions to this module's defined errors."""
        name = type(exc).__name__.lower()
        text = str(exc).lower()
        if "probe" in name or "probe" in text and "not" in text:
            return CalibrationProbeNotAttachedError(str(exc))
        return CalibrationFailedError(str(exc))


__all__ = [
    "CalibrationFailedError",
    "CalibrationProbeNotAttachedError",
    "DEFAULT_CALIBRATION_SLOT",
    "FlexCalibrationController",
]
