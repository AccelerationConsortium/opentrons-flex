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

from ._errors import (
    GripActionError,
    GripperNotAttachedError,
    NotHomedError,
    RecoveryRequiredError,
    mark_task_halt_cancelled,
    translate_system_errors,
)
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
            raise RecoveryRequiredError(msg)

    def _assert_generation(self, generation: int) -> None:
        if generation != self._recovery_state.generation:
            self._recovery_state.require_gripper_home()
            task = asyncio.current_task()
            if task is not None:
                mark_task_halt_cancelled(task)
            raise asyncio.CancelledError

    async def _recover_cancelled_action(self) -> None:
        """Finish a physical halt despite repeated client cancellation."""
        self._recovery_state.mark_halted("A gripper action was cancelled while the jaw or gantry moved")
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

    @translate_system_errors
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

    @translate_system_errors
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

    @translate_system_errors
    async def lift_in_place(self) -> None:
        """
        Home only the gripper Z axis, straight up, ignoring the recovery gates; the jaw keeps its grip.

        After a stall the Z axis position is unknown, so homing is the one move that is both possible
        and safe: it only goes up. The gripper-home gate is set so a later Recover rehomes the jaw.
        """
        from opentrons.hardware_control.types import Axis

        self._require_attached()
        async with self._lock:
            self._recovery_state.require_gripper_home("The gripper was lifted in place")
            try:
                await self._api.home([Axis.Z_G])
            except Exception as exc:
                msg = f"Lifting the gripper in place failed: {exc}"
                raise GripActionError(msg) from exc

    @translate_system_errors
    async def release_in_place(self) -> None:
        """
        Open the jaw where it is, ignoring the recovery gates; no other axis moves.

        Uses the jaw-open motion of ``OT3API.ungrip`` directly: the public call refuses an
        unhomed jaw, which is the jaw state after a restart with a plate still held, and a jaw
        home would first close the jaw on the plate to calibrate its width. The gripper-home
        gate is set so a later Recover still homes and calibrates the jaw.
        """
        self._require_attached()
        async with self._lock:
            self._recovery_state.require_gripper_home("The jaw was released in place")
            try:
                gripper = self._api._gripper_handler.get_gripper()
                duty_cycle = self._api._gripper_handler.get_duty_cycle_by_grip_force(gripper.default_home_force)
                await self._api._ungrip(duty_cycle=duty_cycle)
            except Exception as exc:
                msg = f"Releasing the jaw in place failed: {exc}"
                raise GripActionError(msg) from exc

    def clear_jaw_gate_if_detached(self) -> bool:
        """Clear the jaw-home gate when no gripper is attached (nothing to home); return whether it was cleared."""
        if self.attached or not self._recovery_state.gripper_home_required:
            return False
        if not self._recovery_state.mark_gripper_homed(self._recovery_state.generation):
            return False
        log.info("Gripper jaw gate cleared: no gripper is attached")
        return True

    @translate_system_errors
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
