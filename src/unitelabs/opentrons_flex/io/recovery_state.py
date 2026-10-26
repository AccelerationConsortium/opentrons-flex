"""Shared halt and recovery state for every connector view of one robot API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from opentrons.hardware_control import HardwareControlAPI

from ._errors import mark_task_halt_cancelled

log = logging.getLogger(__name__)

_STATE_ATTRIBUTE = "_unitelabs_hardware_recovery_state"
_STACKER_STATE_ATTRIBUTE = "_unitelabs_flex_stacker_recovery_state"

# One writer thread keeps snapshots ordered and keeps disk I/O (including fsync)
# off the event loop, so recording a gate can never delay an emergency halt.
_PERSISTENCE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="flex-recovery-state")


@dataclass
class HardwareRecoveryState:
    """Fail-safe state shared by the SiLA controller and HTTP hardware proxy."""

    generation: int = 0
    rehome_required: bool = False
    tip_reconciliation_required: set[str] = field(default_factory=set)
    gripper_home_required: bool = False
    _active_robot_tasks: set[asyncio.Task[object]] = field(default_factory=set, repr=False)
    _path: Path | None = field(default=None, repr=False)

    def bind_persistence(self, path: str | Path) -> None:
        """
        Persist recovery gates to ``path`` so a connector restart cannot silently clear them.

        Previously persisted gates (re-home after a halt, tip quarantine, jaw home)
        are restored exactly as recorded, so a restart neither clears nor adds a
        gate. An unreadable file fails closed by requiring every recovery action
        it could have recorded.
        """
        self._path = Path(path).expanduser()
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                mounts = raw.get("tip_reconciliation_required", [])
                if not isinstance(mounts, list) or not all(isinstance(item, str) for item in mounts):
                    msg = "tip_reconciliation_required must be a list of mount names"
                    raise ValueError(msg)
                self.tip_reconciliation_required.update(mounts)
                self.gripper_home_required = self.gripper_home_required or bool(raw.get("gripper_home_required"))
                self.rehome_required = self.rehome_required or bool(raw.get("rehome_required"))
            except (OSError, ValueError, AttributeError) as exc:
                log.error("Recovery state %s is unreadable (%s); requiring full recovery.", self._path, exc)
                self.tip_reconciliation_required.update({"LEFT", "RIGHT"})
                self.gripper_home_required = True
                self.rehome_required = True
        if self.tip_reconciliation_required or self.gripper_home_required or self.rehome_required:
            log.warning("Restored recovery gates: %s.", self.operator_guidance)
        self._save()

    def _save(self) -> None:
        """Queue a snapshot of the gates for the background writer (never blocks the caller)."""
        if self._path is None:
            return
        payload = {
            "version": 1,
            "rehome_required": self.rehome_required,
            "tip_reconciliation_required": sorted(self.tip_reconciliation_required),
            "gripper_home_required": self.gripper_home_required,
        }
        _PERSISTENCE_EXECUTOR.submit(_write_snapshot, self._path, payload)

    def wait_for_persistence(self) -> None:
        """Block until every queued snapshot is on disk (shutdown and tests)."""
        _PERSISTENCE_EXECUTOR.submit(lambda: None).result()

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
            actions.append(f"inspect the tips on {mounts} and run TipController.ReconcileTipState for each mount")
        if self.gripper_home_required:
            actions.append("run GripperController.HomeJaw")
        return ", then ".join(actions) or "No recovery action is required"

    def mark_halted(self) -> None:
        """Invalidate in-flight operation tokens and require a full robot home."""
        self.generation += 1
        self.rehome_required = True
        self._save()
        current = asyncio.current_task()
        for task in tuple(self._active_robot_tasks):
            if task is not current:
                mark_task_halt_cancelled(task)
                task.cancel()

    def require_tip_reconciliation(self, mount_name: str) -> None:
        """Quarantine tip operations until sensor and software state agree."""
        self.tip_reconciliation_required.add(mount_name)
        self._save()

    def mark_tip_reconciled(self, mount_name: str) -> None:
        """Remove one mount from tip-state quarantine."""
        self.tip_reconciliation_required.discard(mount_name)
        self._save()

    def require_gripper_home(self) -> None:
        """Quarantine robot actuation until the interrupted jaw is homed."""
        self.gripper_home_required = True
        self._save()

    def mark_gripper_homed(self, expected_generation: int) -> bool:
        """Clear jaw recovery only if no new halt interleaved with jaw homing."""
        if self.generation != expected_generation or not self.gantry_recovered:
            return False
        self.gripper_home_required = False
        self._save()
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
        self._save()
        return True

    def mark_motion_preserved_after_stop(self, expected_generation: int) -> bool:
        """Clear the halt gate when an engaged stop preserved known position."""
        if self.generation != expected_generation:
            return False
        self.rehome_required = False
        self._save()
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


def _write_snapshot(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except OSError:
        # Persistence is defence in depth; the in-memory gate stays authoritative.
        log.exception("Failed to persist recovery state to %s", path)


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
