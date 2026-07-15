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
import typing

from opentrons.hardware_control import HardwareControlAPI
from opentrons.hardware_control.types import GripperProbe, OT3Mount

from ._errors import CalibrationFailedError, CalibrationProbeNotAttachedError, NotHomedError
from .hardware_proxy import _TimedLock
from .recovery_state import HardwareRecoveryState, recovery_state_for

log = logging.getLogger(__name__)

# Default Flex calibration slot (deck slot 5 / "C2") used by the Opentrons routines.
DEFAULT_CALIBRATION_SLOT = 5
_T = typing.TypeVar("_T")


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
        self._recovery_state: HardwareRecoveryState = recovery_state_for(api)

    @classmethod
    def from_api(
        cls,
        api: HardwareControlAPI,
        lock: asyncio.Lock,
        lock_timeout_s: float | None = None,
    ) -> "FlexCalibrationController":
        """Share an already-built API and lock (in-process robot-server mode)."""
        return cls(api=api, lock=lock, lock_timeout_s=lock_timeout_s)

    def _assert_operation_ready(self) -> None:
        if not self._recovery_state.operation_ready:
            msg = f"The robot is recovery-gated. {self._recovery_state.operator_guidance} before calibration."
            raise NotHomedError(msg)

    async def _recover_cancelled_calibration(self) -> None:
        """Finish a physical halt despite repeated client cancellation."""
        self._recovery_state.mark_halted()
        halt = asyncio.create_task(self._api.halt())
        while not halt.done():
            try:
                await asyncio.shield(halt)
            except asyncio.CancelledError:
                continue
        try:
            await halt
        except Exception:
            log.exception("Failed to halt after calibration cancellation")

    async def _execute(self, action: typing.Callable[[], typing.Awaitable[_T]]) -> _T:
        """Run one calibration under the shared lock and halt/recover on cancellation."""
        async with self._lock:
            self._assert_operation_ready()
            generation = self._recovery_state.generation
            task = self._recovery_state.register_current_operation()
            try:
                result = await action()
                if generation != self._recovery_state.generation:
                    raise asyncio.CancelledError
                return result
            except asyncio.CancelledError:
                self._recovery_state.unregister_operation(task)
                await self._recover_cancelled_calibration()
                raise
            except Exception as exc:
                raise self._translate(exc) from exc
            finally:
                self._recovery_state.unregister_operation(task)

    async def calibrate_pipette(
        self, mount: OT3Mount, slot: int = DEFAULT_CALIBRATION_SLOT
    ) -> tuple[float, float, float]:
        """
        Run automatic pipette-offset calibration for ``mount``.

        Returns the measured pipette offset as an ``(x, y, z)`` tuple in mm.
        """
        from opentrons.hardware_control import ot3_calibration as cal

        offset = await self._execute(lambda: cal.calibrate_pipette(self._api, mount=mount, slot=slot))
        return (offset.x, offset.y, offset.z)

    async def calibrate_gripper_jaw(
        self, probe: GripperProbe, slot: int = DEFAULT_CALIBRATION_SLOT
    ) -> tuple[float, float, float]:
        """
        Run gripper-jaw calibration for one jaw probe.

        ``probe`` selects which gripper jaw (FRONT/REAR) carries the calibration probe.
        Returns the measured jaw offset as ``(x, y, z)`` mm.
        """
        from opentrons.hardware_control import ot3_calibration as cal

        offset = await self._execute(lambda: cal.calibrate_gripper_jaw(self._api, probe=probe, slot=slot))
        return (offset.x, offset.y, offset.z)

    async def calibrate_deck(self, mount: OT3Mount, pipette_id: str) -> None:
        """Run automatic deck (belt) calibration using the pipette on ``mount``."""
        from opentrons.hardware_control import ot3_calibration as cal

        await self._execute(lambda: cal.calibrate_belts(self._api, mount=mount, pipette_id=pipette_id))

    @staticmethod
    def _translate(exc: Exception) -> Exception:
        """Map opentrons calibration exceptions to this module's defined errors."""
        haystack = f"{type(exc).__name__} {exc}".lower()
        probe_missing = "probe" in haystack and ("not" in haystack or "attach" in haystack or "missing" in haystack)
        if probe_missing:
            return CalibrationProbeNotAttachedError(str(exc))
        return CalibrationFailedError(str(exc))


__all__ = [
    "DEFAULT_CALIBRATION_SLOT",
    "CalibrationFailedError",
    "CalibrationProbeNotAttachedError",
    "FlexCalibrationController",
    "NotHomedError",
]
