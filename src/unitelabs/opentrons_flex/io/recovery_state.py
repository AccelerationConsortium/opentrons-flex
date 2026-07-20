"""Shared halt and recovery state for every connector view of one robot API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from opentrons.hardware_control import HardwareControlAPI

_STATE_ATTRIBUTE = "_unitelabs_hardware_recovery_state"
_STACKER_STATE_ATTRIBUTE = "_unitelabs_flex_stacker_recovery_state"


@dataclass
class HardwareRecoveryState:
    """Fail-safe state shared by the SiLA controller and HTTP hardware proxy."""

    generation: int = 0
    rehome_required: bool = False
    tip_reconciliation_required: set[str] = field(default_factory=set)
    gripper_home_required: bool = False
    _active_robot_tasks: set[asyncio.Task[object]] = field(default_factory=set, repr=False)

    @property
    def operation_ready(self) -> bool:
        """Whether a new sensor-verified operation may begin."""
        return self.gantry_recovered and not self.gripper_home_required

    @property
    def gantry_recovered(self) -> bool:
        """Whether full homing and every tip-state reconciliation completed."""
        return not self.rehome_required and not self.tip_reconciliation_required

    @property
    def operator_guidance(self) -> str:
        """Return the ordered recovery actions required by the current state."""
        actions: list[str] = []
        if self.rehome_required:
            actions.append("Fully re-home all robot axes")
        if self.tip_reconciliation_required:
            mounts = ", ".join(sorted(self.tip_reconciliation_required))
            actions.append(f"reconcile the physical and software tip state for {mounts} and restart the connector")
        if self.gripper_home_required:
            actions.append("run GripperController.HomeJaw")
        return ", then ".join(actions) or "No recovery action is required"

    def mark_halted(self) -> None:
        """Invalidate in-flight operation tokens and require a full robot home."""
        self.generation += 1
        self.rehome_required = True
        current = asyncio.current_task()
        for task in tuple(self._active_robot_tasks):
            if task is not current:
                task.cancel()

    def require_tip_reconciliation(self, mount_name: str) -> None:
        """Quarantine tip operations until sensor and software state agree."""
        self.tip_reconciliation_required.add(mount_name)

    def mark_tip_reconciled(self, mount_name: str) -> None:
        """Remove one mount from tip-state quarantine."""
        self.tip_reconciliation_required.discard(mount_name)

    def require_gripper_home(self) -> None:
        """Quarantine robot actuation until the interrupted jaw is homed."""
        self.gripper_home_required = True

    def mark_gripper_homed(self, expected_generation: int) -> bool:
        """Clear jaw recovery only if no new halt interleaved with jaw homing."""
        if self.generation != expected_generation or not self.gantry_recovered:
            return False
        self.gripper_home_required = False
        return True

    def register_current_operation(self) -> asyncio.Task[object]:
        """Register the current task so an emergency halt can cancel it."""
        task = asyncio.current_task()
        if task is None:
            msg = "A hardware operation must run inside an asyncio task."
            raise RuntimeError(msg)
        self._active_robot_tasks.add(task)
        return task

    def unregister_operation(self, task: asyncio.Task[object]) -> None:
        """Remove a completed or cancelled hardware operation task."""
        self._active_robot_tasks.discard(task)

    def mark_fully_homed(self, expected_generation: int) -> bool:
        """Clear gantry recovery only when no halt interleaved with homing."""
        if self.generation != expected_generation:
            return False
        self.rehome_required = False
        return True


@dataclass
class FlexStackerRecoveryState:
    """Recovery authority shared by SiLA and robot-server for one Stacker."""

    full_home_required: bool = False

    def require_full_home(self) -> None:
        """Fail closed after an interrupted, cancelled, or rejected movement."""
        self.full_home_required = True

    def mark_fully_homed(self) -> None:
        """Clear the explicit gate after a complete home including the latch."""
        self.full_home_required = False

    def recovery_required(self, module: object) -> bool:
        """Combine the shared gate with authoritative polled position state."""
        if self.full_home_required:
            return True
        if bool(getattr(module, "is_simulated", False)):
            # Opentrons' explicit Flex Stacker simulator does not mutate its
            # platform or limit-switch sensor values when an axis is homed.
            # A successful full HomeAll is therefore the simulator transport's
            # only available recovery authority. Real modules continue through
            # the fail-closed sensor checks below.
            return False
        try:
            limit_states = module.limit_switch_status.values()
            if any(getattr(state, "value", str(state)) == "unknown" for state in limit_states):
                return True
            platform = getattr(module.platform_state, "value", str(module.platform_state))
            return platform in {"unknown", "missing"}
        except (AttributeError, TypeError):
            # A backend that cannot prove a known position must not authorize
            # labware movement.
            return True


def recovery_state_for(api: HardwareControlAPI) -> HardwareRecoveryState:
    """Return the one recovery-state object associated with ``api``."""
    state = getattr(api, _STATE_ATTRIBUTE, None)
    if not isinstance(state, HardwareRecoveryState):
        state = HardwareRecoveryState()
        setattr(api, _STATE_ATTRIBUTE, state)
    return state


def stacker_recovery_state_for(module: object) -> FlexStackerRecoveryState:
    """Return the one recovery-state object associated with a Stacker module."""
    state = getattr(module, _STACKER_STATE_ATTRIBUTE, None)
    if not isinstance(state, FlexStackerRecoveryState):
        state = FlexStackerRecoveryState()
        setattr(module, _STACKER_STATE_ATTRIBUTE, state)
    return state


__all__ = [
    "FlexStackerRecoveryState",
    "HardwareRecoveryState",
    "recovery_state_for",
    "stacker_recovery_state_for",
]
