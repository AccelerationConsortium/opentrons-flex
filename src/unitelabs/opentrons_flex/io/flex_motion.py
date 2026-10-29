"""
IO wrapper for Opentrons Flex (OT-3) motion control.

Unlike the OT-2 connector, which drives the Smoothie board over a serial port,
the Flex talks to its motor-controller boards over CAN bus. There is no
``SmoothieDriver`` to wrap. Instead this controller wraps the high-level
``HardwareControlAPI`` (the ``OT3API`` implementation), which already exposes
mount/axis motion, instrument caching, lights and gripper control on top of the
CAN layer. We work at that API seam — we do not reimplement the CAN protocol or
modify ot3-firmware.

Concurrency: the SiLA2 gRPC server and the in-process opentrons robot-server
share a single ``HardwareControlAPI``. ``HardwareProxy`` serialises every call
from the robot-server through an ``asyncio.Lock``. This controller is handed the
*same* lock so SiLA motion commands cannot interleave with HTTP-driven motion.
"""

import asyncio
import logging
import math
from collections.abc import Callable, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, cast

from opentrons.hardware_control import HardwareControlAPI
from opentrons.hardware_control.motion_utilities import machine_from_deck, target_position_from_absolute
from opentrons.hardware_control.types import Axis, DoorState, EstopState, MotionChecks, OT3Mount, TipStateType
from opentrons.hardware_control.util import check_motion_bounds
from opentrons.types import Point
from opentrons_shared_data.robot.types import RobotType

from ._errors import (
    LiquidVolumeOutOfRangeError,
    MachineErrorStateError,
    halt_cancellation_pending,
    NozzleConfigurationError,
    PipetteConfigurationError,
    PipetteNotAttachedError,
    RecoveryRequiredError,
    TipNotAttachedError,
    TipPickupError,
    TipStateError,
    mark_task_halt_cancelled,
    translate_liquid_errors,
    translate_motion_errors,
    translate_system_errors,
    translate_tip_errors,
)
from .hardware_proxy import _TimedLock
from .recovery_state import HardwareRecoveryState, recovery_state_for

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .labware_state import LabwareMovementState

# Axes that belong to each mount, used when homing "just this mount".
_MOUNT_AXES: dict[OT3Mount, list[Axis]] = {
    OT3Mount.LEFT: [Axis.Z_L, Axis.P_L],
    OT3Mount.RIGHT: [Axis.Z_R, Axis.P_R],
    OT3Mount.GRIPPER: [Axis.Z_G, Axis.G],
}

# E-stop states that mean the robot is in a hardware error state. DISENGAGED is
# healthy; NOT_PRESENT is treated as healthy too (the simulator and some rigs
# report no E-stop hardware) so it is surfaced for information but never fails a
# move. Motor/encoder "status ok" is deliberately NOT used here: it reads False
# simply because an axis is not homed yet, which is a normal pre-home condition,
# not an error.
_ESTOP_ERROR_STATES: frozenset[EstopState] = frozenset({EstopState.PHYSICALLY_ENGAGED, EstopState.LOGICALLY_ENGAGED})
MAX_SUPPORTED_TIP_LENGTH_MM = 100.0


def position_untrusted_after(error: BaseException) -> bool:
    """
    Whether a failure during an actuation leaves the gantry position untrustworthy.

    Stalls, collisions, motor or CAN communication faults, an E-stop, and
    unexpected driver errors all mean the encoder and the software position may
    disagree, so a full home is required before the next move. Planning errors
    (an out-of-bounds target), a liquid probe that found no liquid (the pipette
    returns to its start first), tip-interaction failures, and module USB errors
    happen before or without losing gantry position and keep their own recovery paths.
    """
    from opentrons_shared_data.errors import exceptions

    return isinstance(error, (MachineErrorStateError, *_position_loss_errors(exceptions)))


_POSITION_LOSS_ERROR_NAMES = (
    # The gantry stopped somewhere other than commanded, or its motors lost track.
    "StallOrCollisionDetectedError",
    "MotionFailedError",
    "MoveConditionNotMetError",
    "MotorDriverError",
    "HomingFailedError",
    "MisalignedGantryError",
    "PositionEstimationInvalidError",
    "EStopActivatedError",
    # The controller lost contact with the motor boards mid-move.
    "CanbusCommunicationError",
    "CANBusBusError",
    "CommandTimedOutError",
    # An unexpected driver error while the actuation was running.
    "PythonException",
)


def _position_loss_errors(module: object) -> tuple[type[BaseException], ...]:
    return tuple(
        error
        for name in _POSITION_LOSS_ERROR_NAMES
        if isinstance(error := getattr(module, name, None), type) and issubclass(error, BaseException)
    )


@dataclass(frozen=True)
class MachineState:
    """
    Snapshot of the Flex safety/error state, independent of any single move.

    ``is_error_state`` is the single question a caller asks after a movement
    command: did the robot silently enter an error state even though the command
    returned? ``estop`` and ``door_open`` provide the underlying detail.
    """

    estop: str  # EstopState name: DISENGAGED | PHYSICALLY_ENGAGED | LOGICALLY_ENGAGED | NOT_PRESENT
    door_open: bool
    is_error_state: bool
    message: str  # operator-facing description + resolution hint; empty when healthy


@dataclass(frozen=True)
class NozzleConfigurationState:
    """Current active nozzle rectangle for one attached pipette."""

    starting_nozzle: str
    back_left_nozzle: str
    front_right_nozzle: str
    active_nozzles: int
    tiprack_diameter: float


MAX_FLOW_RATE_UL_PER_S = 1000.0
"""Upper bound accepted for any configured plunger flow rate."""


@dataclass(frozen=True)
class FlowRateState:
    """Active tip type and plunger flow rates of one pipette, in µL and µL/s."""

    tip_volume: float
    aspirate: float
    dispense: float
    blow_out: float


class FlexMotionController:
    """
    High-level motion controller for the Opentrons Flex, wrapping ``OT3API``.

    This is a thin, SiLA-agnostic wrapper that:
    1. Reuses the battle-tested ``OT3API`` implementation (CAN, calibration, etc.).
    2. Provides a focused surface for the SiLA2 features (motion, pipette, lights).
    3. Serialises hardware access through a lock shared with ``HardwareProxy``.

    Methods take Opentrons types (``OT3Mount``, ``Axis``, ``Point``); the SiLA
    features own the conversion to and from user-facing structures.
    """

    def __init__(
        self,
        api: HardwareControlAPI,
        lock: asyncio.Lock | None = None,
        lock_timeout_s: float | None = None,
    ) -> None:
        """
        Initialize with an existing ``HardwareControlAPI``.

        Use :meth:`build` for normal construction, or :meth:`from_api` when sharing
        the API and lock with the in-process robot-server.
        """
        self._api = api
        raw_lock = lock if lock is not None else asyncio.Lock()
        self._lock: _TimedLock = _TimedLock(raw_lock, lock_timeout_s)
        self._recovery_state: HardwareRecoveryState = recovery_state_for(api)
        self._labware_state: LabwareMovementState | None = None

    @classmethod
    async def build(
        cls,
        simulate: bool = True,
        lock_timeout_s: float | None = None,
    ) -> "FlexMotionController":
        """
        Build a standalone ``FlexMotionController`` (own ``OT3API``, own lock).

        Args:
            simulate: If True, build the OT3 hardware *simulator* backend rather than
                connecting to real CAN hardware. AGENTS.md mandates a simulation mode.
            lock_timeout_s: Seconds to wait for the hardware lock before raising
                ``TimeoutError``.
        """
        from opentrons.hardware_control.ot3api import OT3API

        if simulate:
            log.info("Building FlexMotionController in simulation mode")
            api = await OT3API.build_hardware_simulator()
        else:
            log.info("Building FlexMotionController for real Flex hardware (CAN)")
            api = await OT3API.build_hardware_controller()
        return cls(api=api, lock_timeout_s=lock_timeout_s)

    @classmethod
    def from_api(
        cls,
        api: HardwareControlAPI,
        lock: asyncio.Lock,
        lock_timeout_s: float | None = None,
    ) -> "FlexMotionController":
        """
        Wrap an already-built ``HardwareControlAPI``, sharing a lock with ``HardwareProxy``.

        Used in the in-process server mode where both the SiLA2 gRPC server and the
        opentrons HTTP server drive one ``OT3API``. The caller creates one
        ``asyncio.Lock`` and passes it to both this method and ``HardwareProxy`` so
        callers from both servers are serialised through the same lock.
        """
        return cls(api=api, lock=lock, lock_timeout_s=lock_timeout_s)

    # ------------------------------------------------------------------ status

    @property
    def is_simulating(self) -> bool:
        """Whether the underlying API is a simulator backend."""
        return bool(self._api.is_simulator)

    # ------------------------------------------------------- error / safety state

    def machine_status(self) -> MachineState:
        """
        Return the robot's current safety/error state (E-stop, door).

        Reads cached hardware state only (no CAN I/O and no lock), so it is safe
        to call while a move is holding the lock. This is the query a caller runs
        after a movement command to confirm the robot did not silently enter an
        error state even though the command returned successfully.
        """
        estop = self._api.get_estop_state()
        door_open = self._api.door_state == DoorState.OPEN
        is_error = estop in _ESTOP_ERROR_STATES
        if estop is EstopState.PHYSICALLY_ENGAGED:
            message = (
                "The E-stop is pressed. Make the robot safe, release (twist) the E-stop, then run "
                "RecoveryController.Recover."
            )
        elif estop is EstopState.LOGICALLY_ENGAGED:
            message = (
                "The E-stop was released but not acknowledged. Run RecoveryController.Recover (or "
                "RecoveryController.AcknowledgeEmergencyStop) to acknowledge it and re-home."
            )
        else:
            message = ""
        return MachineState(estop=estop.name, door_open=door_open, is_error_state=is_error, message=message)

    def _assert_machine_ok(self) -> None:
        """
        Raise ``MachineErrorStateError`` if the robot has entered a hardware error state.

        Called at the end of each motion command so a move that "succeeded" at the
        OT3API level but left the machine E-stopped is reported as a failure rather
        than a silent success.
        """
        state = self.machine_status()
        if state.is_error_state:
            raise MachineErrorStateError(state.message)

    async def _finish_cancelled_actuation(self) -> None:
        """Halt a cancelled multi-step actuation before its hardware lock is released."""
        self._recovery_state.mark_halted("A SiLA command was cancelled while the robot moved")
        halt = asyncio.create_task(self._api.halt())
        while not halt.done():
            try:
                await asyncio.shield(halt)
            except asyncio.CancelledError:
                continue
        try:
            await halt
        except Exception:
            log.exception("Failed to halt after hardware-operation cancellation")

    @asynccontextmanager
    async def _active_operation(self) -> AsyncIterator[int]:
        """Register an actuation for immediate cancellation by EmergencyStop."""
        generation = self._recovery_state.generation
        task = self._recovery_state.register_current_operation()
        try:
            yield generation
        except asyncio.CancelledError:
            self._recovery_state.unregister_operation(task)
            await self._finish_cancelled_actuation()
            raise
        except Exception as exc:
            if position_untrusted_after(exc) and not halt_cancellation_pending():
                self._recovery_state.require_rehome(f"{type(exc).__name__} while the robot moved: {exc}")
            raise
        finally:
            self._recovery_state.unregister_operation(task)

    # ------------------------------------------------------------------ motion

    @translate_motion_errors
    async def home(
        self,
        axes: list[Axis] | None = None,
        *,
        guard: Callable[[], bool] | None = None,
    ) -> None:
        """
        Home the given axes (all axes when ``None``).

        ``guard`` runs after the hardware lock is acquired and immediately before
        motion: it raises to abort, or returns False to skip a home that is no
        longer needed (recovery uses it to re-check the door, the E-stop, and that
        no halt happened while it waited for the lock).
        """
        async with self._lock:
            if axes is not None:
                self._assert_operation_ready()
            if guard is not None and not guard():
                return
            async with self._active_operation() as generation:
                await self._api.home(axes=axes)
                self._assert_operation_generation(generation)
                if axes is None:
                    self._recovery_state.mark_fully_homed(generation)
                self._assert_machine_ok()

    async def home_mount(self, mount: OT3Mount) -> None:
        """Home only the axes belonging to one mount."""
        await self.home(_MOUNT_AXES.get(mount))

    @translate_motion_errors
    async def move_to(self, mount: OT3Mount, point: Point, speed: float | None = None) -> Point:
        """Move ``mount`` to an absolute deck ``point`` and return the resulting position."""
        async with self._lock:
            self._assert_direct_gripper_control_allowed(mount, "MoveTo")
            self._assert_operation_ready()
            async with self._active_operation() as generation:
                self._validate_tip_location(point)
                self._validate_absolute_target(mount, point)
                await self._api.move_to(mount=mount, abs_position=point, speed=speed)
                self._assert_operation_generation(generation)
                result = await self._api.gantry_position(mount, refresh=True)
                self._assert_operation_generation(generation)
                self._assert_machine_ok()
                return result

    @translate_motion_errors
    async def move_rel(self, mount: OT3Mount, delta: Point, speed: float | None = None) -> Point:
        """Move ``mount`` by ``delta`` and return the resulting position."""
        async with self._lock:
            self._assert_direct_gripper_control_allowed(mount, "MoveRelative")
            self._assert_operation_ready()
            async with self._active_operation() as generation:
                self._validate_tip_location(delta)
                await self._api.move_rel(mount=mount, delta=delta, speed=speed, check_bounds=MotionChecks.BOTH)
                self._assert_operation_generation(generation)
                result = await self._api.gantry_position(mount, refresh=True)
                self._assert_operation_generation(generation)
                self._assert_machine_ok()
                return result

    @translate_system_errors
    async def gantry_position(self, mount: OT3Mount) -> Point:
        """Return the current position of ``mount``."""
        async with self._lock.observation():
            return await self._api.gantry_position(mount, refresh=True)

    @translate_system_errors
    async def stop(self) -> None:
        """Halt all motion without waiting for the operation lock."""
        self._recovery_state.mark_halted("EmergencyStop was requested")
        # Emergency stop must be able to pre-empt the command currently holding
        # the shared lock. All ordinary hardware operations remain serialized.
        halt = asyncio.create_task(self._api.halt())
        cancelled = False
        while not halt.done():
            try:
                await asyncio.shield(halt)
            except asyncio.CancelledError:
                cancelled = True
        await halt
        if cancelled:
            raise asyncio.CancelledError

    def pause(self) -> None:
        """Pause execution (does not touch the bus directly; no lock needed)."""
        from opentrons.hardware_control.types import PauseType

        self._lock.assert_direct_control_allowed()
        self._api.pause(PauseType.PAUSE)

    def resume(self) -> None:
        """Resume execution after a pause."""
        from opentrons.hardware_control.types import PauseType

        self._lock.assert_direct_control_allowed()
        self._assert_operation_ready()
        self._api.resume(PauseType.PAUSE)

    # --------------------------------------------------------------- pipettes

    @translate_system_errors
    async def cache_instruments(self) -> None:
        """Re-scan the mounts for attached pipettes."""
        async with self._lock:
            await self._api.cache_instruments()

    @property
    def attached_instruments(self) -> dict:
        """Mapping of mount -> attached pipette dict (empty dict when no pipette)."""
        return dict(self._api.attached_instruments)

    @translate_system_errors
    async def configure_nozzle_layout(
        self,
        mount: OT3Mount,
        back_left_nozzle: str | None,
        front_right_nozzle: str | None,
        starting_nozzle: str | None,
        tiprack_diameter: float,
    ) -> NozzleConfigurationState:
        """Configure a full, single, or rectangular partial-tip nozzle layout."""
        async with self._lock:
            self._assert_operation_ready()
            self._assert_pipette_attached(mount)
            instrument = self._api.hardware_instruments[mount.to_mount()]
            if instrument.has_tip:
                msg = "Remove attached tips before changing the active nozzle layout."
                raise NozzleConfigurationError(msg)
            if not math.isfinite(tiprack_diameter) or tiprack_diameter <= 0:
                msg = "Tip-rack diameter must be finite and greater than 0 millimetres."
                raise NozzleConfigurationError(msg)
            try:
                await self._api.update_nozzle_configuration_for_mount(
                    mount,
                    back_left_nozzle=back_left_nozzle,
                    front_right_nozzle=front_right_nozzle,
                    starting_nozzle=starting_nozzle,
                )
                self._api.set_current_tiprack_diameter(mount, tiprack_diameter)
            except (AssertionError, KeyError, ValueError) as exc:
                raise NozzleConfigurationError(str(exc)) from exc
            return self.nozzle_configuration(mount)

    async def configure_tip_type(self, mount: OT3Mount, tip_volume: float) -> FlowRateState:
        """Select the tip type by volume; this also restores that tip's default flow rates."""
        async with self._lock:
            self._assert_operation_ready()
            self._assert_pipette_attached(mount)
            instrument = self._api.hardware_instruments[mount.to_mount()]
            if instrument.has_tip:
                msg = "Remove the attached tip before changing the tip type."
                raise PipetteConfigurationError(msg)
            try:
                self._api.set_working_volume(mount, tip_volume)
            except Exception as exc:  # Opentrons raises several types for unsupported tips
                msg = f"The attached pipette does not support {tip_volume:g} µL tips: {exc}"
                raise PipetteConfigurationError(msg) from exc
            return self.flow_rates(mount)

    async def set_flow_rates(self, mount: OT3Mount, aspirate: float, dispense: float, blow_out: float) -> FlowRateState:
        """Set the aspirate, dispense, and blow-out flow rates in µL/s for later liquid commands."""
        for name, value in (("aspirate", aspirate), ("dispense", dispense), ("blow_out", blow_out)):
            if not math.isfinite(value) or not 0.0 < value <= MAX_FLOW_RATE_UL_PER_S:
                msg = f"{name} flow rate must be greater than 0 and at most {MAX_FLOW_RATE_UL_PER_S:g} µL/s."
                raise PipetteConfigurationError(msg)
        async with self._lock:
            self._assert_operation_ready()
            self._assert_pipette_attached(mount)
            self._api.set_flow_rate(mount, aspirate=aspirate, dispense=dispense, blow_out=blow_out)
            return self.flow_rates(mount)

    def flow_rates(self, mount: OT3Mount) -> FlowRateState:
        """Return the active tip type and flow rates without hardware I/O."""
        self._assert_pipette_attached(mount)
        instrument = self._api.hardware_instruments[mount.to_mount()]
        return FlowRateState(
            tip_volume=float(instrument.working_volume),
            aspirate=float(instrument.aspirate_flow_rate),
            dispense=float(instrument.dispense_flow_rate),
            blow_out=float(instrument.blow_out_flow_rate),
        )

    def nozzle_configuration(self, mount: OT3Mount) -> NozzleConfigurationState:
        """Return the cached active nozzle rectangle without hardware I/O."""
        self._assert_pipette_attached(mount)
        instrument = self._api.hardware_instruments[mount.to_mount()]
        info = self._api.attached_instruments.get(mount.to_mount()) or {}
        nozzle_map = info.get("current_nozzle_map")
        if nozzle_map is None:  # pragma: no cover - supported Flex pipettes always report it
            msg = "Attached pipette did not report its current nozzle map."
            raise NozzleConfigurationError(msg)
        names = list(nozzle_map.map_store)
        if not names:  # pragma: no cover - Opentrons always retains at least one nozzle
            msg = "Attached pipette reported an empty nozzle map."
            raise NozzleConfigurationError(msg)
        rows = [name[0] for name in names]
        columns = [int(name[1:]) for name in names]
        return NozzleConfigurationState(
            starting_nozzle=str(nozzle_map.starting_nozzle),
            back_left_nozzle=f"{min(rows)}{min(columns)}",
            front_right_nozzle=f"{max(rows)}{max(columns)}",
            active_nozzles=len(names),
            tiprack_diameter=float(instrument.current_tiprack_diameter),
        )

    @translate_liquid_errors
    async def prepare_for_aspirate(self, mount: OT3Mount, rate: float = 1.0) -> None:
        """Position the plunger at bottom, ready to aspirate."""
        async with self._lock:
            self._assert_operation_ready()
            self._assert_liquid_action_ready(mount)
            async with self._active_operation() as generation:
                await self._api.prepare_for_aspirate(mount=mount, rate=rate)
                self._assert_operation_generation(generation)
                self._assert_machine_ok()

    @translate_liquid_errors
    async def aspirate(self, mount: OT3Mount, volume: float, rate: float = 1.0) -> None:
        """Aspirate an explicit ``volume`` in µL on ``mount``."""
        async with self._lock:
            self._assert_operation_ready()
            self._assert_liquid_action_ready(mount, volume=volume, aspirating=True)
            async with self._active_operation() as generation:
                await self._api.aspirate(mount=mount, volume=volume, rate=rate)
                self._assert_operation_generation(generation)
                self._assert_machine_ok()

    @translate_liquid_errors
    async def dispense(self, mount: OT3Mount, volume: float, rate: float = 1.0, push_out: float | None = None) -> None:
        """Dispense an explicit ``volume`` in µL on ``mount``."""
        async with self._lock:
            self._assert_operation_ready()
            self._assert_liquid_action_ready(mount, volume=volume, aspirating=False)
            instrument = self._api.hardware_instruments[mount.to_mount()]
            is_full_dispense = math.isclose(volume, float(instrument.current_volume))
            async with self._active_operation() as generation:
                await self._api.dispense(
                    mount=mount,
                    volume=volume,
                    rate=rate,
                    push_out=push_out,
                    is_full_dispense=is_full_dispense,
                )
                self._assert_operation_generation(generation)
                self._assert_machine_ok()

    @translate_liquid_errors
    async def blow_out(self, mount: OT3Mount) -> None:
        """Blow out any residual liquid on ``mount``."""
        async with self._lock:
            self._assert_operation_ready()
            self._assert_liquid_action_ready(mount)
            async with self._active_operation() as generation:
                await self._api.blow_out(mount=mount)
                self._assert_operation_generation(generation)
                self._assert_machine_ok()

    def _assert_pipette_attached(self, mount: OT3Mount) -> None:
        """Raise a stable defined error before querying a missing sensor."""
        if self._api.hardware_instruments.get(mount.to_mount()) is None:
            msg = f"No pipette is attached to {mount.name}. Attach a Flex pipette, re-scan instruments, and retry."
            raise PipetteNotAttachedError(msg)

    def attach_labware_state(self, state: "LabwareMovementState") -> None:
        """Gate raw gripper motion behind the durable allowlisted labware controller."""
        self._labware_state = state

    def _assert_direct_gripper_control_allowed(self, mount: OT3Mount, operation: str) -> None:
        if mount is OT3Mount.GRIPPER and self._labware_state is not None:
            self._labware_state.assert_direct_gripper_control_allowed(operation)

    def _assert_liquid_action_ready(
        self,
        mount: OT3Mount,
        *,
        volume: float | None = None,
        aspirating: bool | None = None,
    ) -> None:
        """Validate pipette, tip, and dynamic volume bounds before liquid actuation."""
        self._assert_pipette_attached(mount)
        instrument = self._api.hardware_instruments.get(mount.to_mount())
        if instrument is None:  # pragma: no cover - guarded by _assert_pipette_attached
            msg = f"No pipette is attached to {mount.name}."
            raise PipetteNotAttachedError(msg)
        if not instrument.has_tip:
            msg = f"No tip is attached to {mount.name}. Perform a sensor-verified tip pickup before liquid handling."
            raise TipNotAttachedError(msg)
        if volume is None:
            return
        if not math.isfinite(volume) or volume <= 0:
            msg = "Liquid volume must be finite and greater than 0 microlitres."
            raise LiquidVolumeOutOfRangeError(msg)

        info = self._api.attached_instruments.get(mount) or self._api.attached_instruments.get(mount.to_mount()) or {}
        minimum = float(info.get("min_volume") or 0.0)
        working_volume = float(getattr(instrument, "working_volume", info.get("max_volume") or 0.0))
        current_volume = float(getattr(instrument, "current_volume", 0.0))
        available = max(0.0, working_volume - current_volume)
        limit = available if aspirating else current_volume
        action = "aspirate" if aspirating else "dispense"
        # Like Protocol Engine, the minimum bounds aspiration only: dispensing a small aliquot
        # of liquid already in the tip (multi-dispense) is allowed down to any positive volume.
        if aspirating and minimum > 0 and volume < minimum:
            msg = (
                f"Cannot {action} {volume:g} microlitres on {mount.name}; the attached pipette minimum is "
                f"{minimum:g} microlitres."
            )
            raise LiquidVolumeOutOfRangeError(msg)
        if volume > limit:
            msg = (
                f"Cannot {action} {volume:g} microlitres on {mount.name}; the current dynamic limit is "
                f"{limit:g} microlitres."
            )
            raise LiquidVolumeOutOfRangeError(msg)

    def _assert_operation_ready(self) -> None:
        if not self._recovery_state.operation_ready:
            msg = f"The robot is recovery-gated. {self._recovery_state.operator_guidance} before retrying."
            raise RecoveryRequiredError(msg)

    def _assert_operation_generation(self, generation: int) -> None:
        """Abort before the next actuation if a concurrent halt invalidated the operation."""
        if generation != self._recovery_state.generation:
            task = asyncio.current_task()
            if task is not None:
                mark_task_halt_cancelled(task)
            raise asyncio.CancelledError

    @staticmethod
    def _validate_tip_location(location: Point) -> None:
        if not all(math.isfinite(value) for value in (location.x, location.y, location.z)):
            msg = "tip operation location coordinates must all be finite"
            raise ValueError(msg)

    def _validate_absolute_target(self, mount: OT3Mount, location: Point) -> None:
        """Reject deck targets outside transformed Flex machine-axis bounds."""
        target_position = target_position_from_absolute(
            mount,
            location,
            partial(self._api.critical_point_for),
            Point(*self._api._config.left_mount_offset),
            Point(*self._api._config.right_mount_offset),
            Point(*self._api._config.gripper_mount_offset),
        )
        machine_position = machine_from_deck(
            deck_pos=target_position,
            attitude=self._api._robot_calibration.deck_calibration.attitude,
            offset=self._api._robot_calibration.carriage_offset,
            robot_type=cast(RobotType, "OT-3 Standard"),
        )
        machine_axes = {axis: machine_position[axis] for axis in target_position if axis in Axis.gantry_axes()}
        check_motion_bounds(
            machine_axes,
            target_position,
            self._api._backend.axis_bounds,
            MotionChecks.BOTH,
        )

    def _synchronize_tip_model(
        self,
        mount: OT3Mount,
        state: TipStateType,
        known_tip_length: float | None,
    ) -> None:
        """Make the Opentrons critical-point model match the physical tip sensor."""
        instrument = self._api.hardware_instruments.get(mount.to_mount())
        if instrument is None:
            msg = f"No pipette is attached to {mount.name} during reconciliation."
            raise PipetteNotAttachedError(msg)
        if state is TipStateType.PRESENT:
            reconciled_tip_length = known_tip_length or float(instrument.current_tip_length)
            if not math.isfinite(reconciled_tip_length) or reconciled_tip_length <= 0:
                msg = f"Cannot reconcile the attached tip length on {mount.name}."
                raise TipStateError(msg)
            if not instrument.has_tip:
                self._api.add_tip(mount, reconciled_tip_length)
        elif state is TipStateType.ABSENT:
            if instrument.has_tip:
                self._api.remove_tip(mount)
        else:
            msg = f"Tip sensor returned {state.name} on {mount.name} during reconciliation."
            raise TipStateError(msg)

    async def _halt_cancelled_tip_operation(self, mount: OT3Mount, known_tip_length: float | None) -> None:
        """Halt and reconcile physical/software tip state before releasing the lock."""
        self._recovery_state.mark_halted("A tip pickup or drop was cancelled")
        self._recovery_state.require_tip_reconciliation(mount.name, "A tip pickup or drop was cancelled")
        try:
            await self._api.halt()
        except Exception:
            log.exception("Failed to halt %s after tip-operation cancellation", mount.name)
        try:
            state = await self._api.get_tip_presence_status(mount)
            self._synchronize_tip_model(mount, state, known_tip_length)
        except Exception:
            log.exception("Failed to reconcile %s tip state after cancellation", mount.name)
        else:
            self._recovery_state.mark_tip_reconciled(mount.name)
            log.warning(
                "%s tip operation cancelled; sensor reports %s, software state was synchronized, "
                "and a full home is required",
                mount.name,
                state.name,
            )

    async def _finish_cancel_recovery(self, mount: OT3Mount, known_tip_length: float | None) -> None:
        """Finish safety recovery even if the client sends repeated cancellation."""
        recovery = asyncio.create_task(self._halt_cancelled_tip_operation(mount, known_tip_length))
        while not recovery.done():
            try:
                await asyncio.shield(recovery)
            except asyncio.CancelledError:
                continue
        await recovery

    async def _approach_vertically(self, mount: OT3Mount, location: Point, generation: int) -> None:
        """Lift every mount, travel in XY at the top of travel, then descend straight onto ``location``."""
        for axis in Axis.ot3_mount_axes():
            await self._api.retract_axis(axis)
            self._assert_operation_generation(generation)
        top = await self._api.gantry_position(mount, refresh=True)
        above = Point(x=location.x, y=location.y, z=top.z)
        self._validate_absolute_target(mount, above)
        await self._api.move_to(mount=mount, abs_position=above)
        self._assert_operation_generation(generation)
        self._assert_machine_ok()
        await self._api.move_to(mount=mount, abs_position=location)

    @translate_tip_errors
    @translate_motion_errors
    async def pick_up_tip(
        self,
        mount: OT3Mount,
        location: Point,
        tip_length: float,
        prep_after: bool = True,
    ) -> TipStateType:
        """
        Atomically approach ``location`` vertically, pick up a tip, lift it clear, and verify it.

        The approach lifts all mounts, travels in XY only, and descends straight down, so
        no diagonal move can sweep through labware; the mount is retracted after pickup.
        """
        if not math.isfinite(tip_length) or not 0.0 < tip_length <= MAX_SUPPORTED_TIP_LENGTH_MM:
            msg = f"tip_length must be finite, greater than 0 mm, and at most {MAX_SUPPORTED_TIP_LENGTH_MM:g} mm"
            raise ValueError(msg)
        self._validate_tip_location(location)
        async with self._lock:
            operation_task: asyncio.Task[object] | None = None
            try:
                self._assert_operation_ready()
                generation = self._recovery_state.generation
                operation_task = self._recovery_state.register_current_operation()
                self._assert_pipette_attached(mount)
                initial_state = await self._api.get_tip_presence_status(mount)
                self._assert_operation_generation(generation)
                if initial_state is not TipStateType.ABSENT:
                    msg = (
                        f"A tip is already attached to {mount.name}. Reconcile the physical tip state "
                        "before attempting another pickup."
                    )
                    raise TipPickupError(msg)
                self._validate_absolute_target(mount, location)
                await self._approach_vertically(mount, location, generation)
                self._assert_operation_generation(generation)
                self._assert_machine_ok()
                await self._api.pick_up_tip(
                    mount=mount,
                    tip_length=tip_length,
                    presses=None,
                    increment=None,
                    prep_after=prep_after,
                )
                self._assert_operation_generation(generation)
                await self._api.retract(mount)
                self._assert_operation_generation(generation)
                state = await self._api.get_tip_presence_status(mount)
                self._assert_operation_generation(generation)
                self._assert_machine_ok()
                if state is not TipStateType.PRESENT:
                    try:
                        self._synchronize_tip_model(mount, state, tip_length)
                    except Exception:
                        self._recovery_state.require_tip_reconciliation(
                            mount.name, f"The {mount.name} tip sensor disagreed after a pickup"
                        )
                        log.exception("Failed to reconcile %s after pickup verification failed", mount.name)
                    msg = f"Tip pickup completed but {mount.name} sensor reported {state.name}."
                    raise TipStateError(msg)
                return state
            except asyncio.CancelledError:
                if operation_task is not None:
                    self._recovery_state.unregister_operation(operation_task)
                await self._finish_cancel_recovery(mount, tip_length)
                raise
            finally:
                if operation_task is not None:
                    self._recovery_state.unregister_operation(operation_task)

    @translate_tip_errors
    @translate_motion_errors
    async def drop_tip(self, mount: OT3Mount, location: Point, home_after: bool = False) -> TipStateType:
        """Atomically approach ``location`` vertically, drop the tip, lift the mount, and verify it."""
        self._validate_tip_location(location)
        async with self._lock:
            known_tip_length: float | None = None
            operation_task: asyncio.Task[object] | None = None
            try:
                self._assert_operation_ready()
                generation = self._recovery_state.generation
                operation_task = self._recovery_state.register_current_operation()
                self._assert_pipette_attached(mount)
                initial_state = await self._api.get_tip_presence_status(mount)
                self._assert_operation_generation(generation)
                if initial_state is not TipStateType.PRESENT:
                    msg = f"No tip is attached to {mount.name}. Perform a verified pickup before retrying the drop."
                    raise TipNotAttachedError(msg)
                instrument = self._api.hardware_instruments[mount.to_mount()]
                known_tip_length = float(instrument.current_tip_length)
                if not math.isfinite(known_tip_length) or known_tip_length <= 0:
                    msg = f"{mount.name} sensor reports a tip but the software tip length is unavailable."
                    raise TipStateError(msg)
                self._validate_absolute_target(mount, location)
                await self._approach_vertically(mount, location, generation)
                self._assert_operation_generation(generation)
                self._assert_machine_ok()
                await self._api.drop_tip(mount=mount, home_after=home_after)
                self._assert_operation_generation(generation)
                await self._api.retract(mount)
                self._assert_operation_generation(generation)
                state = await self._api.get_tip_presence_status(mount)
                self._assert_operation_generation(generation)
                self._assert_machine_ok()
                if state is not TipStateType.ABSENT:
                    try:
                        self._synchronize_tip_model(mount, state, known_tip_length)
                    except Exception:
                        self._recovery_state.require_tip_reconciliation(
                            mount.name, f"The {mount.name} tip sensor disagreed after a drop"
                        )
                        log.exception("Failed to reconcile %s after drop verification failed", mount.name)
                    msg = f"Tip drop completed but {mount.name} sensor reported {state.name}."
                    raise TipStateError(msg)
                return state
            except asyncio.CancelledError:
                if operation_task is not None:
                    self._recovery_state.unregister_operation(operation_task)
                await self._finish_cancel_recovery(mount, known_tip_length)
                raise
            finally:
                if operation_task is not None:
                    self._recovery_state.unregister_operation(operation_task)

    @translate_tip_errors
    async def get_tip_presence(self, mount: OT3Mount) -> TipStateType:
        """Return the tip-presence sensor state for a pipette mount."""
        async with self._lock.observation():
            self._assert_pipette_attached(mount)
            return await self._api.get_tip_presence_status(mount)

    @translate_tip_errors
    async def reconcile_tip_state(self, mount: OT3Mount, tip_length: float) -> TipStateType:
        """
        Read the tip sensor, align the software tip model with it, and lift the tip quarantine.

        ``tip_length`` is used only when the sensor reports a tip that the
        software model does not know about; otherwise the recorded length is kept.
        Reconciliation never moves the robot, so it is permitted while recovery-gated.
        """
        if not math.isfinite(tip_length) or not 0.0 < tip_length <= MAX_SUPPORTED_TIP_LENGTH_MM:
            msg = f"Tip length must be finite, greater than 0 mm, and at most {MAX_SUPPORTED_TIP_LENGTH_MM:g} mm."
            raise TipStateError(msg)
        async with self._lock:
            self._assert_pipette_attached(mount)
            state = await self._api.get_tip_presence_status(mount)
            instrument = self._api.hardware_instruments[mount.to_mount()]
            known_length = None if instrument.has_tip else tip_length
            self._synchronize_tip_model(mount, state, known_length)
            self._recovery_state.mark_tip_reconciled(mount.name)
            log.info("%s tip state reconciled to %s by operator request", mount.name, state.name)
            return state

    @translate_tip_errors
    async def reconcile_tip_state_from_sensor(self, mount: OT3Mount) -> TipStateType:
        """
        Clear a tip quarantine when the sensor reading needs no operator input.

        The software model is aligned with the sensor when the sensor reports no
        tip, or reports a tip whose length the software already knows. A tip the
        software does not know about needs its length from the operator
        (``reconcile_tip_state``), so that case raises ``TipStateError``. A mount
        with no pipette has no tip state to reconcile and is cleared.
        """
        async with self._lock:
            if self._api.hardware_instruments.get(mount.to_mount()) is None:
                self._recovery_state.mark_tip_reconciled(mount.name)
                log.info("%s tip quarantine cleared: no pipette is attached", mount.name)
                return TipStateType.ABSENT
            state = await self._api.get_tip_presence_status(mount)
            instrument = self._api.hardware_instruments[mount.to_mount()]
            if state is TipStateType.PRESENT and not instrument.has_tip:
                msg = (
                    f"The {mount.name} pipette holds a tip the software did not record, so its length is unknown. "
                    "Remove the tip by hand and run Recover again, or run TipController.ReconcileTipState with "
                    "the tip length."
                )
                raise TipStateError(msg)
            self._synchronize_tip_model(mount, state, None)
            self._recovery_state.mark_tip_reconciled(mount.name)
            log.info("%s tip state reconciled to %s from the sensor", mount.name, state.name)
            return state

    def acknowledge_emergency_stop(self) -> str:
        """
        Acknowledge a released E-stop so motion can resume, and return the resulting state name.

        Raises:
            MachineErrorStateError: The E-stop is still pressed.
        """
        estop = self._api.get_estop_state()
        if estop is EstopState.PHYSICALLY_ENGAGED:
            raise MachineErrorStateError(self.machine_status().message)
        if estop is EstopState.LOGICALLY_ENGAGED:
            self._api.estop_acknowledge_and_clear()
            log.info("E-stop acknowledged; it now reports %s", self._api.get_estop_state().name)
        return self._api.get_estop_state().name

    async def cached_gantry_position(self, mount: OT3Mount) -> Point | None:
        """
        Return the last known position of ``mount`` without bus I/O or the hardware lock.

        Returns ``None`` when the position is unknown (for example before homing).
        Intended for status reporting while another operation holds the hardware.
        """
        try:
            return await self._api.gantry_position(mount, refresh=False)
        except Exception:  # noqa: BLE001 - an unknown position is reported, never raised
            return None

    @property
    def recovery_state(self) -> HardwareRecoveryState:
        """Shared halt/recovery gate for this robot API."""
        return self._recovery_state

    @property
    def hardware_busy(self) -> bool:
        """Whether an operation currently holds the shared hardware lock."""
        return self._lock.locked()

    # ------------------------------------------------------------------ lights

    @translate_system_errors
    async def set_lights(self, button: bool | None = None, rails: bool | None = None) -> None:
        """Set the status-bar (``button``) and/or deck (``rails``) lights."""
        async with self._lock:
            await self._api.set_lights(button=button, rails=rails)

    @translate_system_errors
    async def get_lights(self) -> dict[str, bool]:
        """Return the current light state, e.g. ``{"button": bool, "rails": bool}``."""
        async with self._lock.observation():
            return await self._api.get_lights()

    # ----------------------------------------------------------------- cleanup

    async def disconnect(self) -> None:
        """Release hardware resources owned by a standalone controller."""
        async with self._lock:
            await self._api.clean_up()


# Re-export for callers that map SiLA enums to Opentrons types.
__all__ = [
    "Axis",
    "FlexMotionController",
    "MachineState",
    "NozzleConfigurationState",
    "OT3Mount",
    "Point",
    "TipStateType",
]
