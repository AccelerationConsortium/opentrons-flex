"""
IO wrapper for the Opentrons Flex gripper.

The gripper is a Flex-only instrument (the OT-2 has no equivalent). It is driven
through the same ``HardwareControlAPI`` (``OT3API``) as motion, so this thin
controller shares the API and lock with :class:`FlexMotionController` rather than
opening its own transport.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from opentrons.hardware_control import HardwareControlAPI

from ._errors import GripActionError, GripperNotAttachedError, NotHomedError
from .hardware_proxy import _TimedLock
from .recovery_state import HardwareRecoveryState, recovery_state_for

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .labware_state import LabwareMovementState


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
        self._recovery_state: HardwareRecoveryState = recovery_state_for(api)
        self._labware_state: LabwareMovementState | None = None

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

    @property
    def jaw_width(self) -> float:
        """Current sensor-estimated jaw width in millimetres."""
        self._require_attached()
        hardware_gripper = self._api.hardware_gripper
        if hardware_gripper is None:  # pragma: no cover - guarded by _require_attached
            msg = "No gripper attached."
            raise GripperNotAttachedError(msg)
        return float(hardware_gripper.jaw_width)

    def _require_attached(self) -> None:
        if not self.attached:
            msg = "No gripper attached. Attach the Flex gripper and re-scan instruments."
            raise GripperNotAttachedError(msg)

    def attach_labware_state(self, state: "LabwareMovementState") -> None:
        """Gate raw jaw actuation behind the durable allowlisted movement controller."""
        self._labware_state = state

    def _assert_direct_control_allowed(self, operation: str) -> None:
        if self._labware_state is not None:
            self._labware_state.assert_direct_gripper_control_allowed(operation)

    def _assert_operation_ready(self, *, jaw_recovery: bool = False) -> None:
        ready = self._recovery_state.gantry_recovered if jaw_recovery else self._recovery_state.operation_ready
        if not ready:
            msg = f"The robot is recovery-gated. {self._recovery_state.operator_guidance} before continuing."
            raise NotHomedError(msg)

    def _assert_generation(self, generation: int) -> None:
        if generation != self._recovery_state.generation:
            self._recovery_state.require_gripper_home()
            raise asyncio.CancelledError

    async def _recover_cancelled_action(self) -> None:
        """Finish a physical halt despite repeated client cancellation."""
        self._recovery_state.mark_halted()
        self._recovery_state.require_gripper_home()
        halt = asyncio.create_task(self._api.halt())
        while not halt.done():
            try:
                await asyncio.shield(halt)
            except asyncio.CancelledError:
                continue
        try:
            await halt
        except Exception:
            log.exception("Failed to halt after gripper-operation cancellation")

    async def grip(self, force_newtons: float | None = None) -> None:
        """Close the jaw to grip labware (optional ``force_newtons``)."""
        self._require_attached()
        async with self._lock:
            self._assert_direct_control_allowed("Grip")
            self._assert_operation_ready()
            generation = self._recovery_state.generation
            task = self._recovery_state.register_current_operation()
            try:
                await self._api.grip(force_newtons=force_newtons)
                self._assert_generation(generation)
            except asyncio.CancelledError:
                self._recovery_state.unregister_operation(task)
                await self._recover_cancelled_action()
                raise
            except Exception as exc:  # surface the hardware error to the operator
                msg = f"Grip failed: {exc}"
                raise GripActionError(msg) from exc
            finally:
                self._recovery_state.unregister_operation(task)

    async def ungrip(self, force_newtons: float | None = None) -> None:
        """Open the jaw to release labware."""
        self._require_attached()
        async with self._lock:
            self._assert_direct_control_allowed("Ungrip")
            self._assert_operation_ready()
            generation = self._recovery_state.generation
            task = self._recovery_state.register_current_operation()
            try:
                await self._api.ungrip(force_newtons=force_newtons)
                self._assert_generation(generation)
            except asyncio.CancelledError:
                self._recovery_state.unregister_operation(task)
                await self._recover_cancelled_action()
                raise
            except Exception as exc:
                msg = f"Ungrip failed: {exc}"
                raise GripActionError(msg) from exc
            finally:
                self._recovery_state.unregister_operation(task)

    async def home_jaw(self, recalibrate_jaw_width: bool = False) -> None:
        """Home the gripper jaw to its reference position."""
        self._require_attached()
        async with self._lock:
            self._assert_operation_ready(jaw_recovery=True)
            generation = self._recovery_state.generation
            task = self._recovery_state.register_current_operation()
            try:
                await self._api.home_gripper_jaw(recalibrate_jaw_width=recalibrate_jaw_width)
                self._assert_generation(generation)
                self._recovery_state.mark_gripper_homed(generation)
            except asyncio.CancelledError:
                self._recovery_state.unregister_operation(task)
                await self._recover_cancelled_action()
                raise
            except Exception as exc:
                msg = f"Home jaw failed: {exc}"
                raise GripActionError(msg) from exc
            finally:
                self._recovery_state.unregister_operation(task)


__all__ = ["FlexGripperController", "GripActionError", "GripperNotAttachedError", "NotHomedError"]
