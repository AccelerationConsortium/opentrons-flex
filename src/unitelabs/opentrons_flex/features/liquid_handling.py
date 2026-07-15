"""SiLA 2 advanced liquid-handling feature for Opentrons Flex pipettes."""

import asyncio
import enum
import typing
from dataclasses import dataclass

from opentrons.types import Point
from unitelabs.cdk import sila
from unitelabs.cdk.sila import constraints

from ..io import (
    FlexLiquidHandlingController,
    LiquidClassNotSupportedError,
    LiquidHandlingError,
    LiquidNotFoundError,
    LiquidTransferProfile,
    LiquidVolumeOutOfRangeError,
    LiquidWellGeometry,
    MachineErrorStateError,
    MovementOutOfBoundsError,
    NotHomedError,
    PipetteNotAttachedError,
    StallDetectedError,
    TipNotAttachedError,
)
from ._pipette_types import PIPETTE_MOUNTS, PipetteMount
from ._progress import OperationPhase, OperationProgress, report_progress

_MILLIMETRE = constraints.Unit(
    "mm",
    [constraints.Unit.Component(constraints.Unit.SI.METER)],
    factor=0.001,
)
_MILLIMETRES_PER_SECOND = constraints.Unit(
    "mm/s",
    [
        constraints.Unit.Component(constraints.Unit.SI.METER),
        constraints.Unit.Component(constraints.Unit.SI.SECOND, exponent=-1),
    ],
    factor=0.001,
)
_MICROLITRE = constraints.Unit(
    "µL",
    [constraints.Unit.Component(constraints.Unit.SI.METER, exponent=3)],
    factor=1e-9,
)
_DIMENSIONLESS = constraints.Unit("1", [constraints.Unit.Component(constraints.Unit.SI.DIMENSIONLESS)])
_SECOND = constraints.Unit("s", [constraints.Unit.Component(constraints.Unit.SI.SECOND)])

Millimetres = typing.Annotated[float, _MILLIMETRE]
PositiveMillimetres = typing.Annotated[float, constraints.MinimalExclusive(0.0), _MILLIMETRE]
NonNegativeMillimetres = typing.Annotated[float, constraints.MinimalInclusive(0.0), _MILLIMETRE]
MovementSpeed = typing.Annotated[float, constraints.MinimalExclusive(0.0), _MILLIMETRES_PER_SECOND]
LiquidVolume = typing.Annotated[float, constraints.MinimalExclusive(0.0), _MICROLITRE]
NonNegativeLiquidVolume = typing.Annotated[float, constraints.MinimalInclusive(0.0), _MICROLITRE]
RateMultiplier = typing.Annotated[float, constraints.MinimalExclusive(0.0), _DIMENSIONLESS]
MixCycles = typing.Annotated[int, constraints.MinimalInclusive(1), constraints.MaximalInclusive(100)]
ProfileMixCycles = typing.Annotated[int, constraints.MinimalInclusive(0), constraints.MaximalInclusive(100)]
TiprackUri = typing.Annotated[str, constraints.Pattern(r"^[^/]+/[^/]+/[0-9]+$")]
TrackingDelay = typing.Annotated[float, constraints.MinimalInclusive(0.0), _SECOND]

_LIQUID_ERRORS = [
    NotHomedError,
    MovementOutOfBoundsError,
    StallDetectedError,
    MachineErrorStateError,
    PipetteNotAttachedError,
    TipNotAttachedError,
    LiquidVolumeOutOfRangeError,
    LiquidHandlingError,
    LiquidNotFoundError,
]


class VerifiedLiquidClass(enum.Enum):
    """Verified liquid-class definitions shipped by Opentrons."""

    WATER = "water"
    ETHANOL_80 = "ethanol_80"
    GLYCEROL_50 = "glycerol_50"


@dataclass
class LiquidPosition:
    """Absolute deck position of a pipette tip."""

    x: Millimetres
    y: Millimetres
    z: Millimetres


@dataclass
class WellGeometry:
    """Well geometry in deck coordinates for liquid-class and touch-tip calculations."""

    center_x: Millimetres
    center_y: Millimetres
    bottom_z: Millimetres
    top_z: Millimetres
    size_x: PositiveMillimetres
    size_y: PositiveMillimetres


@dataclass
class TransferProfile:
    """Fully specified advanced transfer behavior; every value is explicit."""

    aspirate_rate: RateMultiplier
    dispense_rate: RateMultiplier
    push_out: NonNegativeLiquidVolume
    air_gap: NonNegativeLiquidVolume
    mix_before_cycles: ProfileMixCycles
    mix_before_volume: NonNegativeLiquidVolume
    mix_after_cycles: ProfileMixCycles
    mix_after_volume: NonNegativeLiquidVolume
    blow_out: bool


@dataclass
class LiquidLevel:
    """Liquid height detected by the Flex pipette sensors."""

    detected_height: Millimetres


@dataclass
class VerifiedTransferResult:
    """The verified definition and resolved profile used for a completed transfer."""

    liquid_class: VerifiedLiquidClass
    pipette_model: str
    tiprack_uri: TiprackUri
    aspirate_rate: RateMultiplier
    dispense_rate: RateMultiplier
    push_out: NonNegativeLiquidVolume
    air_gap: NonNegativeLiquidVolume


def _point(position: LiquidPosition) -> Point:
    return Point(position.x, position.y, position.z)


def _well(well: WellGeometry) -> LiquidWellGeometry:
    return LiquidWellGeometry(
        center_x=well.center_x,
        center_y=well.center_y,
        bottom_z=well.bottom_z,
        top_z=well.top_z,
        size_x=well.size_x,
        size_y=well.size_y,
    )


def _profile(profile: TransferProfile) -> LiquidTransferProfile:
    return LiquidTransferProfile(
        aspirate_rate=profile.aspirate_rate,
        dispense_rate=profile.dispense_rate,
        push_out=profile.push_out,
        air_gap=profile.air_gap,
        mix_before_cycles=profile.mix_before_cycles,
        mix_before_volume=profile.mix_before_volume,
        mix_after_cycles=profile.mix_after_cycles,
        mix_after_volume=profile.mix_after_volume,
        blow_out=profile.blow_out,
    )


class LiquidHandlingController(sila.Feature):
    """Advanced Flex pipetting: mix, touch-tip, probing, profiles, and liquid classes."""

    def __init__(self, controller: FlexLiquidHandlingController) -> None:
        super().__init__(originator="ca.accelerationconsortium", category="robots", version="1.0")
        self._controller = controller

    @sila.ObservableCommand(errors=_LIQUID_ERRORS)
    async def mix(
        self,
        mount: PipetteMount,
        cycles: MixCycles,
        volume: LiquidVolume,
        aspirate_rate: RateMultiplier,
        dispense_rate: RateMultiplier,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> None:
        """Mix liquid at the current tip position."""
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, "Starting liquid mixing.")
        report_progress(status, intermediate, 0.1, OperationPhase.RUNNING, "Executing repeated liquid cycles.")
        try:
            await self._controller.mix(PIPETTE_MOUNTS[mount], cycles, volume, aspirate_rate, dispense_rate)
        except asyncio.CancelledError:
            report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, "Liquid mixing cancelled; re-home.")
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, "Liquid mixing completed.")

    @sila.ObservableCommand(errors=_LIQUID_ERRORS)
    async def touch_tip(
        self,
        mount: PipetteMount,
        well: WellGeometry,
        z_offset: Millimetres,
        distance_from_edge: NonNegativeMillimetres,
        speed: MovementSpeed,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> LiquidPosition:
        """Touch the attached tip against four inner well walls."""
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, "Starting touch-tip movement.")
        report_progress(status, intermediate, 0.1, OperationPhase.RUNNING, "Tracing the well wall.")
        try:
            result = await self._controller.touch_tip(
                PIPETTE_MOUNTS[mount], _well(well), z_offset, distance_from_edge, speed
            )
        except asyncio.CancelledError:
            report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, "Touch-tip cancelled; re-home.")
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, "Touch-tip completed.")
        return LiquidPosition(result.x, result.y, result.z)

    @sila.ObservableCommand(errors=_LIQUID_ERRORS)
    async def probe_liquid_level(
        self,
        mount: PipetteMount,
        maximum_distance: PositiveMillimetres,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> LiquidLevel:
        """Probe downward with the pipette sensors and report the detected liquid height."""
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, "Starting liquid-level probing.")
        report_progress(status, intermediate, 0.1, OperationPhase.RUNNING, "Probing for liquid.")
        try:
            height = await self._controller.probe_liquid_level(PIPETTE_MOUNTS[mount], maximum_distance)
        except asyncio.CancelledError:
            report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, "Liquid probing cancelled; re-home.")
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, "Liquid level detected.")
        return LiquidLevel(detected_height=height)

    @sila.ObservableCommand(errors=_LIQUID_ERRORS)
    async def aspirate_while_tracking(
        self,
        mount: PipetteMount,
        end_position: LiquidPosition,
        volume: LiquidVolume,
        rate: RateMultiplier,
        movement_delay: TrackingDelay,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> LiquidPosition:
        """Aspirate while moving to follow a changing liquid height."""
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, "Starting tracked aspiration.")
        report_progress(status, intermediate, 0.1, OperationPhase.RUNNING, "Tracking the liquid surface.")
        try:
            result = await self._controller.aspirate_while_tracking(
                PIPETTE_MOUNTS[mount], _point(end_position), volume, rate, movement_delay
            )
        except asyncio.CancelledError:
            report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, "Tracked aspiration cancelled.")
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, "Tracked aspiration completed.")
        return LiquidPosition(result.x, result.y, result.z)

    @sila.ObservableCommand(errors=_LIQUID_ERRORS)
    async def dispense_while_tracking(
        self,
        mount: PipetteMount,
        end_position: LiquidPosition,
        volume: LiquidVolume,
        rate: RateMultiplier,
        push_out: NonNegativeLiquidVolume,
        movement_delay: TrackingDelay,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> LiquidPosition:
        """Dispense while moving to follow a changing liquid height."""
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, "Starting tracked dispense.")
        report_progress(status, intermediate, 0.1, OperationPhase.RUNNING, "Tracking the liquid surface.")
        try:
            result = await self._controller.dispense_while_tracking(
                PIPETTE_MOUNTS[mount], _point(end_position), volume, rate, push_out, movement_delay
            )
        except asyncio.CancelledError:
            report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, "Tracked dispense cancelled.")
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, "Tracked dispense completed.")
        return LiquidPosition(result.x, result.y, result.z)

    @sila.ObservableCommand(errors=_LIQUID_ERRORS)
    async def transfer(
        self,
        mount: PipetteMount,
        source: LiquidPosition,
        source_retract: LiquidPosition,
        destination: LiquidPosition,
        destination_retract: LiquidPosition,
        volume: LiquidVolume,
        profile: TransferProfile,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> None:
        """Run an atomic source-to-destination transfer using an explicit profile."""
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, "Starting liquid transfer.")
        report_progress(status, intermediate, 0.1, OperationPhase.RUNNING, "Executing the transfer profile.")
        try:
            await self._controller.transfer(
                PIPETTE_MOUNTS[mount],
                _point(source),
                _point(source_retract),
                _point(destination),
                _point(destination_retract),
                volume,
                _profile(profile),
            )
        except asyncio.CancelledError:
            report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, "Transfer cancelled; re-home.")
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, "Liquid transfer completed.")

    @sila.ObservableCommand(errors=[*_LIQUID_ERRORS, LiquidClassNotSupportedError])
    async def transfer_with_verified_liquid_class(
        self,
        mount: PipetteMount,
        source_well: WellGeometry,
        destination_well: WellGeometry,
        volume: LiquidVolume,
        liquid_class: VerifiedLiquidClass,
        tiprack_uri: TiprackUri,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> VerifiedTransferResult:
        """Transfer using the installed Opentrons verified liquid-class definition version 2."""
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, "Resolving verified liquid class.")
        report_progress(status, intermediate, 0.1, OperationPhase.RUNNING, "Executing verified liquid transfer.")
        try:
            result = await self._controller.transfer_with_verified_liquid_class(
                PIPETTE_MOUNTS[mount],
                _well(source_well),
                _well(destination_well),
                volume,
                liquid_class.value,
                tiprack_uri,
            )
        except asyncio.CancelledError:
            report_progress(
                status,
                intermediate,
                1.0,
                OperationPhase.CANCELLED,
                "Verified transfer cancelled; reconcile liquid state and re-home.",
            )
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, "Verified liquid transfer completed.")
        return VerifiedTransferResult(
            liquid_class=VerifiedLiquidClass(result.liquid_class),
            pipette_model=result.pipette_model,
            tiprack_uri=result.tiprack_uri,
            aspirate_rate=result.profile.aspirate_rate,
            dispense_rate=result.profile.dispense_rate,
            push_out=result.profile.push_out,
            air_gap=result.profile.air_gap,
        )


__all__ = [
    "LiquidHandlingController",
    "LiquidLevel",
    "LiquidPosition",
    "TransferProfile",
    "VerifiedLiquidClass",
    "VerifiedTransferResult",
    "WellGeometry",
]
