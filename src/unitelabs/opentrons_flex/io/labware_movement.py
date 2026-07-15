"""Allowlisted Flex labware movement using the official gripper waypoint planner."""

import asyncio
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from opentrons.hardware_control.modules.types import ModuleType
from opentrons.hardware_control.types import Axis, OT3Mount
from opentrons.motion_planning import get_gripper_labware_movement_waypoints
from opentrons.types import Point
from opentrons_shared_data.errors.exceptions import (
    FailedGripperPickupError,
    GripperNotPresentError,
    LabwareDroppedError,
)

from ._errors import (
    GripActionError,
    GripperNotAttachedError,
    LabwareMovementNotAllowedError,
    LabwareNotPickedError,
    LabwareNotPlacedError,
    translate_motion_errors,
)
from .flex_motion import FlexMotionController
from .gripper import FlexGripperController
from .labware_state import LabwareMovementState

_LOCATION_KINDS = {"DECK_SLOT", "MODULE", "LABWARE", "WASTE_CHUTE", "STAGING_AREA"}


@dataclass(frozen=True)
class LabwareGripGeometry:
    """Server-provisioned labware force and jaw-width verification limits."""

    force_newtons: float
    expected_width: float
    uncertainty_wider: float
    uncertainty_narrower: float


@dataclass(frozen=True)
class LabwareMovementPlan:
    """One locally provisioned, immutable gripper movement plan."""

    identifier: str
    labware_identifier: str
    is_lid: bool
    source_identifier: str
    source_kind: str
    source_grip_point: Point
    destination_identifier: str
    destination_kind: str
    destination_grip_point: Point
    geometry: LabwareGripGeometry
    post_drop_slide_offset: Point


@dataclass(frozen=True)
class LoadedLabwareMovementConfig:
    """Validated plan registry and initial server-owned deck occupancy."""

    plans: tuple[LabwareMovementPlan, ...]
    initial_occupancy: dict[str, str]
    state_file: Path | None


@dataclass(frozen=True)
class LabwareMoveResult:
    """Physical result of a completed gripper movement."""

    final_position: Point
    jaw_width: float


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        msg = f"{label} must be a JSON object."
        raise ValueError(msg)
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = f"{label} must be a non-empty string."
        raise ValueError(msg)
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        msg = f"{label} must be a finite number."
        raise ValueError(msg)
    return float(value)


def _point(value: object, label: str) -> Point:
    data = _mapping(value, label)
    return Point(
        _number(data.get("x"), f"{label}.x"),
        _number(data.get("y"), f"{label}.y"),
        _number(data.get("z"), f"{label}.z"),
    )


def _location(value: object, label: str) -> tuple[str, str, Point]:
    data = _mapping(value, label)
    identifier = _text(data.get("identifier"), f"{label}.identifier")
    kind = _text(data.get("kind"), f"{label}.kind")
    if kind not in _LOCATION_KINDS:
        msg = f"{label}.kind must be one of {sorted(_LOCATION_KINDS)}."
        raise ValueError(msg)
    return identifier, kind, _point(data.get("grip_point"), f"{label}.grip_point")


def _plan(value: object, index: int) -> LabwareMovementPlan:
    data = _mapping(value, f"plans[{index}]")
    source_identifier, source_kind, source_point = _location(data.get("source"), f"plans[{index}].source")
    destination_identifier, destination_kind, destination_point = _location(
        data.get("destination"), f"plans[{index}].destination"
    )
    grip = _mapping(data.get("grip"), f"plans[{index}].grip")
    is_lid = data.get("is_lid")
    if not isinstance(is_lid, bool):
        msg = f"plans[{index}].is_lid must be a boolean."
        raise ValueError(msg)
    return LabwareMovementPlan(
        identifier=_text(data.get("identifier"), f"plans[{index}].identifier"),
        labware_identifier=_text(data.get("labware_identifier"), f"plans[{index}].labware_identifier"),
        is_lid=is_lid,
        source_identifier=source_identifier,
        source_kind=source_kind,
        source_grip_point=source_point,
        destination_identifier=destination_identifier,
        destination_kind=destination_kind,
        destination_grip_point=destination_point,
        geometry=LabwareGripGeometry(
            force_newtons=_number(grip.get("force_newtons"), f"plans[{index}].grip.force_newtons"),
            expected_width=_number(grip.get("expected_width"), f"plans[{index}].grip.expected_width"),
            uncertainty_wider=_number(grip.get("uncertainty_wider"), f"plans[{index}].grip.uncertainty_wider"),
            uncertainty_narrower=_number(grip.get("uncertainty_narrower"), f"plans[{index}].grip.uncertainty_narrower"),
        ),
        post_drop_slide_offset=_point(data.get("post_drop_slide_offset"), f"plans[{index}].post_drop_slide_offset"),
    )


def load_labware_movement_config(path: str | Path) -> LoadedLabwareMovementConfig:
    """Load a local JSON plan registry; no movement-plan data is accepted over SiLA."""
    config_path = Path(path).expanduser()
    try:
        data = _mapping(json.loads(config_path.read_text(encoding="utf-8")), "labware movement configuration")
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"Cannot load labware movement configuration {config_path}: {exc}"
        raise ValueError(msg) from exc

    raw_plans = data.get("plans")
    raw_occupancy = data.get("initial_occupancy")
    state_file = _text(data.get("state_file"), "labware movement configuration state_file")
    if not isinstance(raw_plans, list):
        msg = "labware movement configuration plans must be a JSON array."
        raise ValueError(msg)
    if not isinstance(raw_occupancy, Mapping):
        msg = "labware movement configuration initial_occupancy must be a location-to-labware JSON object."
        raise ValueError(msg)
    plans = tuple(_plan(value, index) for index, value in enumerate(raw_plans))
    occupancy = {
        _text(location, "initial_occupancy location"): _text(labware, f"initial_occupancy[{location!r}]")
        for location, labware in raw_occupancy.items()
    }
    state_path = Path(state_file).expanduser()
    if not state_path.is_absolute():
        state_path = config_path.parent / state_path
    return LoadedLabwareMovementConfig(plans=plans, initial_occupancy=occupancy, state_file=state_path)


class FlexLabwareMovementController:
    """Execute allowlisted pick/place plans while owning deck state and the hardware lock."""

    def __init__(
        self,
        motion: FlexMotionController,
        gripper: FlexGripperController,
        plans: Iterable[LabwareMovementPlan] = (),
        state: LabwareMovementState | None = None,
    ) -> None:
        self._motion = motion
        self._gripper = gripper
        self._api = motion._api
        if motion._api is not gripper._api or motion._lock._lock is not gripper._lock._lock:
            msg = "Labware movement requires motion and gripper controllers sharing one API and lock."
            raise ValueError(msg)
        plan_list = tuple(plans)
        self._plans = {plan.identifier: plan for plan in plan_list}
        if len(self._plans) != len(plan_list):
            msg = "Labware movement plan identifiers must be unique."
            raise ValueError(msg)
        self._state = state
        if plan_list and state is None:
            msg = "Configured labware movement plans require a durable LabwareMovementState ledger."
            raise ValueError(msg)
        if state is not None:
            motion.attach_labware_state(state)
            gripper.attach_labware_state(state)
        for plan in plan_list:
            self._validate_plan_definition(plan)

    @translate_motion_errors
    async def move_labware(
        self,
        plan_identifier: str,
        *,
        expect_lid: bool,
    ) -> tuple[LabwareMovementPlan, LabwareMoveResult]:
        """Execute one locally provisioned plan selected by its public identifier."""
        plan = self._configured_plan(plan_identifier, expect_lid=expect_lid)
        source_module = self._module_for_location(plan.source_identifier, plan.source_kind)
        destination_module = self._module_for_location(plan.destination_identifier, plan.destination_kind)

        async with self._motion._lock:
            self._gripper._require_attached()
            if not self._api.gripper_jaw_can_home():
                msg = (
                    "The gripper appears to be holding an object. Reconcile the deck and run HomeJaw before a new move."
                )
                raise LabwareMovementNotAllowedError(msg)
            self._gripper._assert_operation_ready()
            self._validate_current_occupancy(plan)
            self._assert_module_accessible(source_module, direction="source")
            self._assert_module_accessible(destination_module, direction="destination")
            try:
                async with self._motion._active_operation() as generation:
                    if self._state is None:  # pragma: no cover - guarded during construction
                        msg = "Durable labware state is unavailable."
                        raise LabwareMovementNotAllowedError(msg)
                    self._state.begin_move()
                    thermocycler = await self._prepare_thermocycler_source(source_module)
                    try:
                        result = await self._move_locked(plan)
                    finally:
                        if thermocycler is not None:
                            await thermocycler.return_from_raise_plate()
                    self._motion._assert_operation_generation(generation)
                    self._motion._assert_machine_ok()
                    self._state.complete_move(
                        plan.source_identifier,
                        plan.destination_identifier,
                        plan.labware_identifier,
                    )
                    return plan, result
            except asyncio.CancelledError:
                self._motion._recovery_state.require_gripper_home()
                raise
            except FailedGripperPickupError as exc:
                self._motion._recovery_state.require_gripper_home()
                raise LabwareNotPickedError(str(exc)) from exc
            except LabwareDroppedError as exc:
                self._motion._recovery_state.require_gripper_home()
                raise LabwareNotPlacedError(str(exc)) from exc
            except Exception:
                self._motion._recovery_state.require_gripper_home()
                raise

    async def _move_locked(self, plan: LabwareMovementPlan) -> LabwareMoveResult:
        await self._api.home(axes=[Axis.Z_L, Axis.Z_R, Axis.Z_G])
        gripper_home = await self._api.gantry_position(OT3Mount.GRIPPER, refresh=True)
        slide = None if plan.post_drop_slide_offset == Point(0, 0, 0) else plan.post_drop_slide_offset
        waypoints = get_gripper_labware_movement_waypoints(
            from_labware_center=plan.source_grip_point,
            to_labware_center=plan.destination_grip_point,
            gripper_home_z=gripper_home.z,
            post_drop_slide_offset=slide,
        )

        holding_labware = False
        placed = False
        for waypoint in waypoints:
            if waypoint.jaw_open:
                if waypoint.dropping:
                    await self._api.disengage_axes([Axis.Z_G])
                await self._ungrip_locked()
                holding_labware = True
                if waypoint.dropping:
                    placed = True
                    await self._api.home_z(OT3Mount.GRIPPER)
            else:
                await self._grip_locked(plan.geometry.force_newtons)
                if holding_labware:
                    self._api.raise_error_if_gripper_pickup_failed(
                        expected_grip_width=plan.geometry.expected_width,
                        grip_width_uncertainty_wider=plan.geometry.uncertainty_wider,
                        grip_width_uncertainty_narrower=plan.geometry.uncertainty_narrower,
                        disable_geometry_grip_check=False,
                    )
            await self._api.move_to(mount=OT3Mount.GRIPPER, abs_position=waypoint.position)

        if not placed:
            msg = "The movement plan ended before the gripper released at the destination."
            raise LabwareNotPlacedError(msg)
        await self._idle_gripper_locked()
        final_position = await self._api.gantry_position(OT3Mount.GRIPPER, refresh=True)
        return LabwareMoveResult(final_position=final_position, jaw_width=self._gripper.jaw_width)

    def _configured_plan(self, identifier: str, *, expect_lid: bool) -> LabwareMovementPlan:
        plan = self._plans.get(identifier)
        if plan is None:
            msg = (
                f"Labware movement plan {identifier!r} is not provisioned on this server. "
                "Add it to the local labware movement configuration and restart the connector."
            )
            raise LabwareMovementNotAllowedError(msg)
        if plan.is_lid is not expect_lid:
            endpoint = "MoveLid" if plan.is_lid else "MoveLabware"
            msg = f"Plan {identifier!r} must be executed through {endpoint}."
            raise LabwareMovementNotAllowedError(msg)
        return plan

    def _validate_current_occupancy(self, plan: LabwareMovementPlan) -> None:
        if self._state is None:  # pragma: no cover - guarded during construction
            msg = "Durable labware state is unavailable."
            raise LabwareMovementNotAllowedError(msg)
        self._state.validate_move(
            plan.source_identifier,
            plan.destination_identifier,
            plan.labware_identifier,
        )

    @property
    def available_plans(self) -> tuple[LabwareMovementPlan, ...]:
        """Return locally provisioned plan metadata for SiLA discovery."""
        return tuple(self._plans.values())

    @property
    def deck_state(self) -> tuple[bool, dict[str, str]]:
        """Return durable validity and a copy of location-to-labware identity."""
        if self._state is None:
            return False, {}
        return self._state.valid, self._state.occupancy

    async def _grip_locked(self, force_newtons: float) -> None:
        try:
            await self._api.grip(force_newtons=force_newtons)
        except (FailedGripperPickupError, LabwareDroppedError):
            raise
        except GripperNotPresentError as exc:
            raise GripperNotAttachedError(str(exc)) from exc
        except Exception as exc:
            msg = f"Grip failed during labware movement: {exc}"
            raise GripActionError(msg) from exc

    async def _ungrip_locked(self) -> None:
        try:
            await self._api.ungrip()
        except LabwareDroppedError:
            raise
        except GripperNotPresentError as exc:
            raise GripperNotAttachedError(str(exc)) from exc
        except Exception as exc:
            msg = f"Ungrip failed during labware movement: {exc}"
            raise GripActionError(msg) from exc

    async def _idle_gripper_locked(self) -> None:
        try:
            await self._api.idle_gripper()
        except GripperNotPresentError as exc:
            raise GripperNotAttachedError(str(exc)) from exc
        except Exception as exc:
            msg = f"Idling the gripper failed after labware movement: {exc}"
            raise GripActionError(msg) from exc

    def _module_for_location(self, identifier: str, kind: str) -> object | None:
        for module in self._api.attached_modules:
            info = getattr(module, "device_info", {})
            serial = str(info.get("serial") or info.get("serial_number") or getattr(module, "serial_number", ""))
            if serial == identifier:
                return module
        if kind != "MODULE":
            return None
        msg = f"No attached module matches configured location {identifier!r}; refresh module inventory and retry."
        raise LabwareMovementNotAllowedError(msg)

    @staticmethod
    def _assert_module_accessible(module: object | None, *, direction: str) -> None:
        if module is None:
            return
        module_type = getattr(module, "MODULE_TYPE", None)
        if module_type is ModuleType.HEATER_SHAKER:
            latch = FlexLabwareMovementController._state_text(getattr(module, "labware_latch_status", ""))
            if "open" not in latch or "opening" in latch:
                msg = f"Cannot move labware at the {direction} Heater-Shaker while its latch is not fully open."
                raise LabwareMovementNotAllowedError(msg)
        elif module_type in {ModuleType.THERMOCYCLER, ModuleType.ABSORBANCE_READER}:
            lid = FlexLabwareMovementController._state_text(getattr(module, "lid_status", ""))
            if "open" not in lid or "opening" in lid:
                msg = f"Cannot move labware at the {direction} module while its lid is not fully open."
                raise LabwareMovementNotAllowedError(msg)
        elif module_type is ModuleType.FLEX_STACKER:
            msg = "Use the FlexStacker feature to transfer labware through the stacker shuttle."
            raise LabwareMovementNotAllowedError(msg)

    @staticmethod
    async def _prepare_thermocycler_source(module: object | None) -> object | None:
        if module is None or getattr(module, "MODULE_TYPE", None) is not ModuleType.THERMOCYCLER:
            return None
        await module.lift_plate()
        await module.raise_plate()
        return module

    @staticmethod
    def _state_text(value: object) -> str:
        return str(getattr(value, "value", value)).lower()

    def _validate_plan_definition(self, plan: LabwareMovementPlan) -> None:
        identifiers = (plan.identifier, plan.labware_identifier, plan.source_identifier, plan.destination_identifier)
        if not all(identifier.strip() for identifier in identifiers):
            msg = "Plan, labware, source, and destination identifiers must not be empty."
            raise ValueError(msg)
        if plan.source_identifier == plan.destination_identifier:
            msg = f"Plan {plan.identifier!r} must use different source and destination locations."
            raise ValueError(msg)
        if plan.source_kind not in _LOCATION_KINDS or plan.destination_kind not in _LOCATION_KINDS:
            msg = f"Plan {plan.identifier!r} contains an unknown location kind."
            raise ValueError(msg)
        for point in (plan.source_grip_point, plan.destination_grip_point, plan.post_drop_slide_offset):
            if not all(math.isfinite(value) for value in (point.x, point.y, point.z)):
                msg = f"Plan {plan.identifier!r} contains non-finite coordinates."
                raise ValueError(msg)
        if not 5.0 <= plan.geometry.force_newtons <= 25.0:
            msg = f"Plan {plan.identifier!r} grip force must be between 5 and 25 newtons."
            raise ValueError(msg)
        widths = (
            plan.geometry.expected_width,
            plan.geometry.uncertainty_wider,
            plan.geometry.uncertainty_narrower,
        )
        if not all(math.isfinite(value) for value in widths):
            msg = f"Plan {plan.identifier!r} contains non-finite gripper-width values."
            raise ValueError(msg)
        if (
            plan.geometry.expected_width <= 0
            or plan.geometry.uncertainty_wider < 0
            or plan.geometry.uncertainty_narrower < 0
        ):
            msg = f"Plan {plan.identifier!r} requires positive width and non-negative uncertainties."
            raise ValueError(msg)
        self._motion._validate_absolute_target(OT3Mount.GRIPPER, plan.source_grip_point)
        self._motion._validate_absolute_target(OT3Mount.GRIPPER, plan.destination_grip_point)


__all__ = [
    "FlexLabwareMovementController",
    "LabwareGripGeometry",
    "LabwareMoveResult",
    "LabwareMovementPlan",
    "LoadedLabwareMovementConfig",
    "load_labware_movement_config",
]
