"""
IO wrapper for the Opentrons Flex gripper.

The gripper is a Flex-only instrument (the OT-2 has no equivalent). It is driven
through the same ``HardwareControlAPI`` (``OT3API``) as motion, so this thin
controller shares the API and lock with :class:`FlexMotionController` rather than
opening its own transport.
"""

import asyncio
import logging

from opentrons.hardware_control import HardwareControlAPI

from ._errors import GripActionError, GripperNotAttachedError
from .hardware_proxy import _TimedLock

log = logging.getLogger(__name__)


class FlexGripperController:
    """Thin gripper controller wrapping ``OT3API`` gripper methods under a shared lock."""

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
    ) -> "FlexGripperController":
        """Share an already-built API and lock (in-process robot-server mode)."""
        return cls(api=api, lock=lock, lock_timeout_s=lock_timeout_s)

    @property
    def attached(self) -> bool:
        """Whether a gripper is currently attached."""
        return self._api.attached_gripper is not None

    @property
    def info(self) -> dict | None:
        """Attached gripper dict (``model``, ``gripper_id``, ``state`` ...) or ``None``."""
        gripper = self._api.attached_gripper
        return dict(gripper) if gripper else None

    def _require_attached(self) -> None:
        if not self.attached:
            msg = "No gripper attached. Attach the Flex gripper and re-scan instruments."
            raise GripperNotAttachedError(msg)

    async def grip(self, force_newtons: float | None = None) -> None:
        """Close the jaw to grip labware (optional ``force_newtons``)."""
        self._require_attached()
        async with self._lock:
            try:
                await self._api.grip(force_newtons=force_newtons)
            except Exception as exc:  # surface the hardware error to the operator
                msg = f"Grip failed: {exc}"
                raise GripActionError(msg) from exc

    async def ungrip(self, force_newtons: float | None = None) -> None:
        """Open the jaw to release labware."""
        self._require_attached()
        async with self._lock:
            try:
                await self._api.ungrip(force_newtons=force_newtons)
            except Exception as exc:
                msg = f"Ungrip failed: {exc}"
                raise GripActionError(msg) from exc

    async def home_jaw(self, recalibrate_jaw_width: bool = False) -> None:
        """Home the gripper jaw to its reference position."""
        self._require_attached()
        async with self._lock:
            try:
                await self._api.home_gripper_jaw(recalibrate_jaw_width=recalibrate_jaw_width)
            except Exception as exc:
                msg = f"Home jaw failed: {exc}"
                raise GripActionError(msg) from exc


__all__ = ["FlexGripperController", "GripActionError", "GripperNotAttachedError"]
