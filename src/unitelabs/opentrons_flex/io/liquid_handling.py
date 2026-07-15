"""Advanced liquid-handling orchestration over the shared Flex hardware API."""

import asyncio
import math
from dataclasses import dataclass
from itertools import pairwise

from opentrons.hardware_control.types import OT3Mount
from opentrons.types import Point

from ._errors import (
    LiquidClassNotSupportedError,
    LiquidHandlingError,
    LiquidVolumeOutOfRangeError,
    translate_liquid_errors,
    translate_motion_errors,
)
from .flex_motion import FlexMotionController


@dataclass(frozen=True)
class LiquidTransferProfile:
    """Complete, non-optional settings for one explicit liquid transfer."""

    aspirate_rate: float
    dispense_rate: float
    push_out: float
    air_gap: float
    mix_before_cycles: int
    mix_before_volume: float
    mix_after_cycles: int
    mix_after_volume: float
    blow_out: bool


@dataclass(frozen=True)
class LiquidWellGeometry:
    """Well geometry in deck coordinates used to resolve liquid-class positions."""

    center_x: float
    center_y: float
    bottom_z: float
    top_z: float
    size_x: float
    size_y: float


@dataclass(frozen=True)
class VerifiedLiquidTransfer:
    """Resolved values from an installed Opentrons verified liquid class."""

    liquid_class: str
    pipette_model: str
    tiprack_uri: str
    profile: LiquidTransferProfile


class FlexLiquidHandlingController:
    """Run atomic liquid workflows without releasing the shared hardware lock."""

    def __init__(self, motion: FlexMotionController) -> None:
        self._motion = motion
        self._api = motion._api

    @translate_liquid_errors
    @translate_motion_errors
    async def mix(
        self,
        mount: OT3Mount,
        cycles: int,
        volume: float,
        aspirate_rate: float,
        dispense_rate: float,
    ) -> None:
        """Mix at the current location using repeated aspirate/dispense cycles."""
        self._validate_mix(cycles, volume)
        self._validate_rate(aspirate_rate, "aspirate_rate")
        self._validate_rate(dispense_rate, "dispense_rate")
        async with self._motion._lock:
            self._motion._assert_operation_ready()
            self._motion._assert_liquid_action_ready(mount, volume=volume, aspirating=True)
            async with self._motion._active_operation() as generation:
                await self._mix_locked(mount, cycles, volume, aspirate_rate, dispense_rate)
                self._motion._assert_operation_generation(generation)
                self._motion._assert_machine_ok()

    @translate_liquid_errors
    @translate_motion_errors
    async def touch_tip(
        self,
        mount: OT3Mount,
        well: LiquidWellGeometry,
        z_offset: float,
        distance_from_edge: float,
        speed: float,
    ) -> Point:
        """Trace the four inner walls of a rectangular or circular well."""
        self._validate_well(well)
        self._validate_rate(speed, "speed")
        points = self._touch_points(well, z_offset, distance_from_edge)
        async with self._motion._lock:
            self._motion._assert_operation_ready()
            self._motion._assert_liquid_action_ready(mount)
            async with self._motion._active_operation() as generation:
                await self._touch_locked(mount, points, speed)
                self._motion._assert_operation_generation(generation)
                self._motion._assert_machine_ok()
                return points[-1]

    @translate_liquid_errors
    @translate_motion_errors
    async def probe_liquid_level(self, mount: OT3Mount, maximum_distance: float) -> float:
        """Probe downward and return the detected liquid height in deck millimetres."""
        if not math.isfinite(maximum_distance) or maximum_distance <= 0:
            message = "Maximum liquid probing distance must be finite and greater than 0 millimetres."
            raise LiquidHandlingError(message)
        async with self._motion._lock:
            self._motion._assert_operation_ready()
            self._motion._assert_liquid_action_ready(mount)
            async with self._motion._active_operation() as generation:
                detected_height = await self._api.liquid_probe(mount=mount, max_z_dist=maximum_distance)
                self._motion._assert_operation_generation(generation)
                self._motion._assert_machine_ok()
                return float(detected_height)

    @translate_liquid_errors
    @translate_motion_errors
    async def aspirate_while_tracking(
        self,
        mount: OT3Mount,
        end_point: Point,
        volume: float,
        rate: float,
        movement_delay: float,
    ) -> Point:
        """Aspirate while moving the tip to a final point for liquid-height tracking."""
        self._validate_rate(rate, "rate")
        self._validate_delay(movement_delay)
        self._motion._validate_tip_location(end_point)
        async with self._motion._lock:
            self._motion._assert_operation_ready()
            self._motion._assert_liquid_action_ready(mount, volume=volume, aspirating=True)
            self._motion._validate_absolute_target(mount, end_point)
            async with self._motion._active_operation() as generation:
                await self._api.aspirate_while_tracking(
                    mount=mount,
                    end_point=end_point,
                    volume=volume,
                    rate=rate,
                    movement_delay=movement_delay,
                )
                self._motion._assert_operation_generation(generation)
                self._motion._assert_machine_ok()
                return await self._api.gantry_position(mount, refresh=True)

    @translate_liquid_errors
    @translate_motion_errors
    async def dispense_while_tracking(
        self,
        mount: OT3Mount,
        end_point: Point,
        volume: float,
        rate: float,
        push_out: float,
        movement_delay: float,
    ) -> Point:
        """Dispense while moving the tip to a final point for liquid-height tracking."""
        self._validate_rate(rate, "rate")
        self._validate_delay(movement_delay)
        if not math.isfinite(push_out) or push_out < 0:
            msg = "Push-out volume must be finite and at least 0 microlitres."
            raise LiquidVolumeOutOfRangeError(msg)
        self._motion._validate_tip_location(end_point)
        async with self._motion._lock:
            self._motion._assert_operation_ready()
            self._motion._assert_liquid_action_ready(mount, volume=volume, aspirating=False)
            self._motion._validate_absolute_target(mount, end_point)
            instrument = self._api.hardware_instruments[mount.to_mount()]
            async with self._motion._active_operation() as generation:
                await self._api.dispense_while_tracking(
                    mount=mount,
                    end_point=end_point,
                    volume=volume,
                    rate=rate,
                    push_out=push_out,
                    is_full_dispense=math.isclose(volume, float(instrument.current_volume)),
                    movement_delay=movement_delay,
                )
                self._motion._assert_operation_generation(generation)
                self._motion._assert_machine_ok()
                return await self._api.gantry_position(mount, refresh=True)

    @translate_liquid_errors
    @translate_motion_errors
    async def transfer(
        self,
        mount: OT3Mount,
        source: Point,
        source_retract: Point,
        destination: Point,
        destination_retract: Point,
        volume: float,
        profile: LiquidTransferProfile,
    ) -> None:
        """Run one atomic transfer using an explicit, fully specified profile."""
        self._validate_profile(profile)
        for point in (source, source_retract, destination, destination_retract):
            self._motion._validate_tip_location(point)
        async with self._motion._lock:
            self._motion._assert_operation_ready()
            self._assert_transfer_capacity(mount, volume, profile.air_gap)
            async with self._motion._active_operation() as generation:
                await self._transfer_locked(
                    mount,
                    source,
                    source_retract,
                    destination,
                    destination_retract,
                    volume,
                    profile,
                )
                self._motion._assert_operation_generation(generation)
                self._motion._assert_machine_ok()

    @translate_liquid_errors
    @translate_motion_errors
    async def transfer_with_verified_liquid_class(
        self,
        mount: OT3Mount,
        source_well: LiquidWellGeometry,
        destination_well: LiquidWellGeometry,
        volume: float,
        liquid_class: str,
        tiprack_uri: str,
    ) -> VerifiedLiquidTransfer:
        """Run one transfer from the installed Opentrons verified liquid-class definition."""
        self._validate_well(source_well)
        self._validate_well(destination_well)
        async with self._motion._lock:
            self._motion._assert_operation_ready()
            resolved, definition = self._resolve_liquid_class(mount, liquid_class, tiprack_uri, volume)
            self._assert_transfer_capacity(mount, volume, resolved.profile.air_gap)
            async with self._motion._active_operation() as generation:
                await self._verified_transfer_locked(
                    mount,
                    source_well,
                    destination_well,
                    volume,
                    resolved.profile,
                    definition,
                )
                self._motion._assert_operation_generation(generation)
                self._motion._assert_machine_ok()
                return resolved

    async def _transfer_locked(
        self,
        mount: OT3Mount,
        source: Point,
        source_retract: Point,
        destination: Point,
        destination_retract: Point,
        volume: float,
        profile: LiquidTransferProfile,
    ) -> None:
        await self._move_locked(mount, source, speed=None)
        await self._api.prepare_for_aspirate(mount=mount, rate=profile.aspirate_rate)
        if profile.mix_before_cycles:
            await self._mix_locked(
                mount,
                profile.mix_before_cycles,
                profile.mix_before_volume,
                profile.aspirate_rate,
                profile.dispense_rate,
            )
        await self._api.aspirate(mount=mount, volume=volume, rate=profile.aspirate_rate)
        await self._move_locked(mount, source_retract, speed=None)
        if profile.air_gap:
            await self._api.aspirate(mount=mount, volume=profile.air_gap, rate=profile.aspirate_rate)
        await self._move_locked(mount, destination, speed=None)
        await self._dispense_locked(
            mount,
            volume + profile.air_gap,
            profile.dispense_rate,
            profile.push_out,
            correction_volume=0.0,
        )
        if profile.mix_after_cycles:
            await self._mix_locked(
                mount,
                profile.mix_after_cycles,
                profile.mix_after_volume,
                profile.aspirate_rate,
                profile.dispense_rate,
            )
        if profile.blow_out:
            await self._api.blow_out(mount=mount)
        await self._move_locked(mount, destination_retract, speed=None)

    async def _verified_transfer_locked(
        self,
        mount: OT3Mount,
        source_well: LiquidWellGeometry,
        destination_well: LiquidWellGeometry,
        volume: float,
        profile: LiquidTransferProfile,
        definition: object,
    ) -> None:
        aspirate = definition.aspirate
        dispense = definition.singleDispense
        source_start = self._liquid_class_position(source_well, aspirate.submerge.startPosition)
        source = self._liquid_class_position(source_well, aspirate.aspiratePosition)
        source_retract = self._liquid_class_position(source_well, aspirate.retract.endPosition)
        destination_start = self._liquid_class_position(destination_well, dispense.submerge.startPosition)
        destination = self._liquid_class_position(destination_well, dispense.dispensePosition)
        destination_retract = self._liquid_class_position(destination_well, dispense.retract.endPosition)

        await self._move_locked(mount, source_start, speed=None)
        await self._delay_if_enabled(aspirate.submerge.delay)
        await self._move_locked(mount, source, speed=float(aspirate.submerge.speed))
        await self._api.prepare_for_aspirate(mount=mount, rate=profile.aspirate_rate)
        if profile.mix_before_cycles:
            await self._mix_locked(
                mount,
                profile.mix_before_cycles,
                profile.mix_before_volume,
                profile.aspirate_rate,
                profile.dispense_rate,
            )
        aspiration_correction = self._interpolate(aspirate.correctionByVolume, volume)
        await self._api.aspirate(
            mount=mount,
            volume=volume,
            rate=profile.aspirate_rate,
            correction_volume=aspiration_correction,
        )
        await self._delay_if_enabled(aspirate.delay)
        await self._move_locked(mount, source_retract, speed=float(aspirate.retract.speed))
        if profile.air_gap:
            await self._api.aspirate(mount=mount, volume=profile.air_gap, rate=profile.aspirate_rate)
        if aspirate.retract.touchTip.enable:
            touch = aspirate.retract.touchTip.params
            await self._touch_locked(
                mount,
                self._touch_points(source_well, float(touch.zOffset), float(touch.mmFromEdge)),
                float(touch.speed),
            )
        await self._delay_if_enabled(aspirate.retract.delay)

        await self._move_locked(mount, destination_start, speed=None)
        await self._delay_if_enabled(dispense.submerge.delay)
        await self._move_locked(mount, destination, speed=float(dispense.submerge.speed))
        dispense_correction = self._interpolate(dispense.correctionByVolume, volume)
        await self._dispense_locked(
            mount,
            volume + profile.air_gap,
            profile.dispense_rate,
            profile.push_out,
            correction_volume=dispense_correction,
        )
        if profile.mix_after_cycles:
            await self._mix_locked(
                mount,
                profile.mix_after_cycles,
                profile.mix_after_volume,
                profile.aspirate_rate,
                profile.dispense_rate,
            )
        await self._delay_if_enabled(dispense.delay)
        await self._move_locked(mount, destination_retract, speed=float(dispense.retract.speed))
        if dispense.retract.blowout.enable:
            await self._api.blow_out(mount=mount)
        if dispense.retract.touchTip.enable:
            touch = dispense.retract.touchTip.params
            await self._touch_locked(
                mount,
                self._touch_points(destination_well, float(touch.zOffset), float(touch.mmFromEdge)),
                float(touch.speed),
            )
        await self._delay_if_enabled(dispense.retract.delay)

    async def _mix_locked(
        self,
        mount: OT3Mount,
        cycles: int,
        volume: float,
        aspirate_rate: float,
        dispense_rate: float,
    ) -> None:
        await self._api.prepare_for_aspirate(mount=mount, rate=aspirate_rate)
        for _ in range(cycles):
            self._motion._assert_liquid_action_ready(mount, volume=volume, aspirating=True)
            await self._api.aspirate(mount=mount, volume=volume, rate=aspirate_rate)
            await self._dispense_locked(mount, volume, dispense_rate, push_out=0.0, correction_volume=0.0)

    async def _dispense_locked(
        self,
        mount: OT3Mount,
        volume: float,
        rate: float,
        push_out: float,
        correction_volume: float,
    ) -> None:
        instrument = self._api.hardware_instruments[mount.to_mount()]
        self._motion._assert_liquid_action_ready(mount, volume=volume, aspirating=False)
        await self._api.dispense(
            mount=mount,
            volume=volume,
            rate=rate,
            push_out=push_out,
            correction_volume=correction_volume,
            is_full_dispense=math.isclose(volume, float(instrument.current_volume)),
        )

    async def _move_locked(self, mount: OT3Mount, point: Point, speed: float | None) -> None:
        self._motion._validate_tip_location(point)
        self._motion._validate_absolute_target(mount, point)
        await self._api.move_to(mount=mount, abs_position=point, speed=speed)

    async def _touch_locked(self, mount: OT3Mount, points: list[Point], speed: float) -> None:
        for point in points:
            await self._move_locked(mount, point, speed=speed)

    def _assert_transfer_capacity(self, mount: OT3Mount, volume: float, air_gap: float) -> None:
        if not math.isfinite(volume) or volume <= 0:
            msg = "Transfer volume must be finite and greater than 0 microlitres."
            raise LiquidVolumeOutOfRangeError(msg)
        self._motion._assert_liquid_action_ready(mount, volume=volume + air_gap, aspirating=True)

    @staticmethod
    def _validate_rate(rate: float, name: str) -> None:
        if not math.isfinite(rate) or rate <= 0:
            msg = f"{name} must be finite and greater than 0."
            raise LiquidHandlingError(msg)

    @staticmethod
    def _validate_mix(cycles: int, volume: float) -> None:
        if cycles < 0 or cycles > 100:
            msg = "Mix cycles must be between 0 and 100."
            raise LiquidHandlingError(msg)
        if cycles and (not math.isfinite(volume) or volume <= 0):
            msg = "Mix volume must be finite and greater than 0 when cycles are requested."
            raise LiquidVolumeOutOfRangeError(msg)
        if not cycles and volume != 0:
            msg = "Mix volume must be 0 when mix cycles are 0."
            raise LiquidHandlingError(msg)

    def _validate_profile(self, profile: LiquidTransferProfile) -> None:
        self._validate_rate(profile.aspirate_rate, "aspirate_rate")
        self._validate_rate(profile.dispense_rate, "dispense_rate")
        if not math.isfinite(profile.push_out) or profile.push_out < 0:
            msg = "Push-out volume must be finite and at least 0 microlitres."
            raise LiquidVolumeOutOfRangeError(msg)
        if not math.isfinite(profile.air_gap) or profile.air_gap < 0:
            msg = "Air-gap volume must be finite and at least 0 microlitres."
            raise LiquidVolumeOutOfRangeError(msg)
        self._validate_mix(profile.mix_before_cycles, profile.mix_before_volume)
        self._validate_mix(profile.mix_after_cycles, profile.mix_after_volume)

    @staticmethod
    def _validate_delay(delay: float) -> None:
        if not math.isfinite(delay) or delay < 0:
            msg = "Movement delay must be finite and at least 0 seconds."
            raise LiquidHandlingError(msg)

    @staticmethod
    def _validate_well(well: LiquidWellGeometry) -> None:
        values = (well.center_x, well.center_y, well.bottom_z, well.top_z, well.size_x, well.size_y)
        if not all(math.isfinite(value) for value in values):
            msg = "Every well-geometry value must be finite."
            raise LiquidHandlingError(msg)
        if well.top_z <= well.bottom_z or well.size_x <= 0 or well.size_y <= 0:
            msg = "Well top must be above its bottom and both horizontal sizes must be positive."
            raise LiquidHandlingError(msg)

    @staticmethod
    def _touch_points(well: LiquidWellGeometry, z_offset: float, distance_from_edge: float) -> list[Point]:
        if not math.isfinite(z_offset) or not math.isfinite(distance_from_edge) or distance_from_edge < 0:
            msg = "Touch-tip offset and edge distance must be finite; edge distance cannot be negative."
            raise LiquidHandlingError(msg)
        x_radius = well.size_x / 2 - distance_from_edge
        y_radius = well.size_y / 2 - distance_from_edge
        if x_radius <= 0 or y_radius <= 0:
            msg = "Touch-tip edge distance must be smaller than half of each well dimension."
            raise LiquidHandlingError(msg)
        z = well.top_z + z_offset
        return [
            Point(well.center_x - x_radius, well.center_y, z),
            Point(well.center_x, well.center_y - y_radius, z),
            Point(well.center_x + x_radius, well.center_y, z),
            Point(well.center_x, well.center_y + y_radius, z),
        ]

    def _resolve_liquid_class(
        self,
        mount: OT3Mount,
        liquid_class: str,
        tiprack_uri: str,
        volume: float,
    ) -> tuple[VerifiedLiquidTransfer, object]:
        from opentrons_shared_data.liquid_classes import LiquidClassDefinitionDoesNotExist, load_definition

        supported = {"water", "ethanol_80", "glycerol_50"}
        if liquid_class not in supported:
            msg = f"Unknown verified liquid class {liquid_class!r}; choose WATER, ETHANOL_80, or GLYCEROL_50."
            raise LiquidClassNotSupportedError(msg)
        try:
            definition = load_definition(liquid_class, version=2)
        except LiquidClassDefinitionDoesNotExist as exc:
            raise LiquidClassNotSupportedError(str(exc)) from exc

        info = self._api.attached_instruments.get(mount.to_mount()) or {}
        pipette_model = self._liquid_class_pipette_model(info)
        pipette_definition = next(
            (entry for entry in definition.byPipette if entry.pipetteModel == pipette_model),
            None,
        )
        if pipette_definition is None:
            msg = f"{liquid_class} has no verified definition for attached model {pipette_model}."
            raise LiquidClassNotSupportedError(msg)
        tip_definition = next((entry for entry in pipette_definition.byTipType if entry.tiprack == tiprack_uri), None)
        if tip_definition is None:
            msg = f"{liquid_class} has no verified {pipette_model} profile for tip rack {tiprack_uri!r}."
            raise LiquidClassNotSupportedError(msg)

        aspirate = tip_definition.aspirate
        dispense = tip_definition.singleDispense
        aspirate_flow = self._interpolate(aspirate.flowRateByVolume, volume)
        dispense_flow = self._interpolate(dispense.flowRateByVolume, volume)
        aspirate_base = float(info.get("aspirate_flow_rate") or 0.0)
        dispense_base = float(info.get("dispense_flow_rate") or 0.0)
        if aspirate_base <= 0 or dispense_base <= 0:
            msg = "Attached pipette did not report usable base flow rates."
            raise LiquidClassNotSupportedError(msg)

        mix_before_cycles = int(aspirate.mix.params.repetitions) if aspirate.mix.enable else 0
        mix_before_volume = float(aspirate.mix.params.volume) if aspirate.mix.enable else 0.0
        mix_after_cycles = int(dispense.mix.params.repetitions) if dispense.mix.enable else 0
        mix_after_volume = float(dispense.mix.params.volume) if dispense.mix.enable else 0.0
        profile = LiquidTransferProfile(
            aspirate_rate=aspirate_flow / aspirate_base,
            dispense_rate=dispense_flow / dispense_base,
            push_out=self._interpolate(dispense.pushOutByVolume, volume),
            air_gap=self._interpolate(aspirate.retract.airGapByVolume, volume),
            mix_before_cycles=mix_before_cycles,
            mix_before_volume=mix_before_volume,
            mix_after_cycles=mix_after_cycles,
            mix_after_volume=mix_after_volume,
            blow_out=bool(dispense.retract.blowout.enable),
        )
        self._validate_profile(profile)
        return (
            VerifiedLiquidTransfer(
                liquid_class=liquid_class,
                pipette_model=pipette_model,
                tiprack_uri=tiprack_uri,
                profile=profile,
            ),
            tip_definition,
        )

    @staticmethod
    def _liquid_class_pipette_model(info: dict) -> str:
        channels = int(info.get("channels") or 0)
        maximum = round(float(info.get("max_volume") or info.get("working_volume") or 0))
        channel_names = {1: "1channel", 8: "8channel", 96: "96channel"}
        channel_name = channel_names.get(channels)
        if channel_name is None or maximum not in {50, 200, 1000}:
            msg = f"Attached pipette channels={channels}, maximum_volume={maximum} µL is not in the verified class set."
            raise LiquidClassNotSupportedError(msg)
        return f"flex_{channel_name}_{maximum}"

    @staticmethod
    def _interpolate(points: list[tuple[float, float]], volume: float) -> float:
        """Piecewise-linear interpolation matching Opentrons liquid-class tables."""
        if not points:
            return 0.0
        ordered = sorted((float(x), float(y)) for x, y in points)
        if volume <= ordered[0][0]:
            return ordered[0][1]
        if volume >= ordered[-1][0]:
            return ordered[-1][1]
        for (x0, y0), (x1, y1) in pairwise(ordered):
            if x0 <= volume <= x1:
                fraction = (volume - x0) / (x1 - x0)
                return y0 + fraction * (y1 - y0)
        msg = "interpolation interval not found"
        raise AssertionError(msg)  # pragma: no cover

    @staticmethod
    def _liquid_class_position(well: LiquidWellGeometry, position: object) -> Point:
        reference = position.positionReference.value
        offset = position.offset
        if reference == "well-bottom":
            z_reference = well.bottom_z
        elif reference == "well-top":
            z_reference = well.top_z
        else:
            msg = f"Unsupported liquid-class position reference {reference!r}."
            raise LiquidClassNotSupportedError(msg)
        return Point(
            well.center_x + float(offset.x),
            well.center_y + float(offset.y),
            z_reference + float(offset.z),
        )

    @staticmethod
    async def _delay_if_enabled(delay: object) -> None:
        if bool(delay.enable):
            duration = float(delay.params.duration)
            if duration > 0:
                await asyncio.sleep(duration)


__all__ = [
    "FlexLiquidHandlingController",
    "LiquidTransferProfile",
    "LiquidWellGeometry",
    "VerifiedLiquidTransfer",
]
