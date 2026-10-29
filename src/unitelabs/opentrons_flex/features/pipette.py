"""SiLA2 feature for Flex pipette detection and partial-tip nozzle layouts."""

import typing
from dataclasses import dataclass

from unitelabs.cdk import sila
from unitelabs.cdk.sila import constraints

from ..io import (
    SYSTEM_ERRORS,
    HardwareBusyError,
    HardwareError,
    FlexMotionController,
    NozzleConfigurationError,
    NotHomedError,
    PipetteConfigurationError,
    PipetteNotAttachedError,
    RunOwnershipError,
)
from ._pipette_types import PIPETTE_MOUNTS, PipetteMount
from ._progress import OperationProgress, run_observable

_MILLIMETRE = constraints.Unit(
    "mm",
    [constraints.Unit.Component(constraints.Unit.SI.METER)],
    factor=0.001,
)
_MICROLITRE = constraints.Unit(
    "µL",
    [constraints.Unit.Component(constraints.Unit.SI.METER, exponent=3)],
    factor=1e-9,
)
LiquidVolume = typing.Annotated[float, constraints.MinimalInclusive(0.0), _MICROLITRE]
TiprackDiameter = typing.Annotated[float, constraints.MinimalExclusive(0.0), _MILLIMETRE]
NozzleIdentifier = typing.Annotated[str, constraints.Pattern(r"^[A-H](?:[1-9]|1[0-2])$")]
_MICROLITRE_PER_SECOND = constraints.Unit(
    "µL/s",
    [
        constraints.Unit.Component(constraints.Unit.SI.METER, exponent=3),
        constraints.Unit.Component(constraints.Unit.SI.SECOND, exponent=-1),
    ],
    factor=1e-9,
)
TipVolume = typing.Annotated[float, constraints.Set([50.0, 200.0, 1000.0]), _MICROLITRE]
FlowRate = typing.Annotated[
    float, constraints.MinimalExclusive(0.0), constraints.MaximalInclusive(1000.0), _MICROLITRE_PER_SECOND
]
ChannelCount = typing.Annotated[int, constraints.Set((0, 1, 8, 96))]


@dataclass
class PipetteInfo:
    """
    A pipette attached to one mount.

    Flex pipette models are e.g. ``flex_1channel_1000`` / ``flex_8channel_1000`` /
    ``flex_96channel_1000``. ``attached`` is False (and the other fields are empty
    / zero) when no pipette is present on that mount.
    """

    mount: PipetteMount
    attached: bool
    model: str
    name: str
    pipette_id: str
    channels: ChannelCount
    min_volume: LiquidVolume
    max_volume: LiquidVolume
    has_tip: bool


@dataclass
class NozzleConfiguration:
    """Current active nozzle rectangle and tip-rack geometry."""

    mount: PipetteMount
    starting_nozzle: NozzleIdentifier
    back_left_nozzle: NozzleIdentifier
    front_right_nozzle: NozzleIdentifier
    active_nozzles: typing.Annotated[int, constraints.MinimalInclusive(1), constraints.MaximalInclusive(96)]
    tiprack_diameter: TiprackDiameter


@dataclass
class FlowRates:
    """Active tip type and the flow rates later liquid commands are scaled from."""

    mount: PipetteMount
    tip_volume: typing.Annotated[float, _MICROLITRE]
    aspirate: typing.Annotated[float, _MICROLITRE_PER_SECOND]
    dispense: typing.Annotated[float, _MICROLITRE_PER_SECOND]
    blow_out: typing.Annotated[float, _MICROLITRE_PER_SECOND]


class PipetteFeature(sila.Feature):
    """SiLA2 feature reporting the pipettes attached to the Flex mounts."""

    def __init__(self, controller: FlexMotionController):
        super().__init__(
            originator="ca.accelerationconsortium",
            category="robots",
            identifier="PipetteController",
            name="Pipette Controller",
            version="1.3",
        )
        self._controller = controller

    @sila.ObservableCommand(errors=[RunOwnershipError, HardwareBusyError, HardwareError])
    async def get_attached_pipettes(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> list[PipetteInfo]:
        """
        Re-scan both pipette mounts and report one entry per mount.

        Empty mounts have ``attached=False`` and empty metadata fields.

        Yields:
            Update: Current pipette scan progress update.
        """
        await run_observable(
            status,
            intermediate,
            "Scanning attached Flex pipettes.",
            "Attached Flex pipettes scanned.",
            "Attached Flex pipette scan cancelled.",
            self._controller.cache_instruments(),
        )
        instruments = self._controller.attached_instruments

        results: list[PipetteInfo] = []
        for mount, ot3_mount in PIPETTE_MOUNTS.items():
            info = instruments.get(ot3_mount.to_mount()) or instruments.get(ot3_mount) or {}
            model = info.get("model") or ""
            results.append(
                PipetteInfo(
                    mount=mount,
                    attached=bool(model),
                    model=model,
                    name=info.get("name") or "",
                    pipette_id=info.get("pipette_id") or "",
                    channels=int(info.get("channels") or 0),
                    min_volume=float(info.get("min_volume") or 0.0),
                    max_volume=float(info.get("max_volume") or 0.0),
                    has_tip=bool(info.get("has_tip", False)),
                )
            )
        return results

    @sila.UnobservableCommand(
        errors=[RunOwnershipError, PipetteNotAttachedError, NotHomedError, *SYSTEM_ERRORS, NozzleConfigurationError]
    )
    async def configure_full_nozzle_layout(
        self,
        mount: PipetteMount,
        tiprack_diameter: TiprackDiameter,
    ) -> NozzleConfiguration:
        """Activate every nozzle on the attached pipette."""
        state = await self._controller.configure_nozzle_layout(
            PIPETTE_MOUNTS[mount],
            back_left_nozzle=None,
            front_right_nozzle=None,
            starting_nozzle=None,
            tiprack_diameter=tiprack_diameter,
        )
        return self._nozzle_result(mount, state)

    @sila.UnobservableCommand(
        errors=[RunOwnershipError, PipetteNotAttachedError, NotHomedError, *SYSTEM_ERRORS, NozzleConfigurationError]
    )
    async def configure_single_nozzle_layout(
        self,
        mount: PipetteMount,
        nozzle: NozzleIdentifier,
        tiprack_diameter: TiprackDiameter,
    ) -> NozzleConfiguration:
        """Activate one nozzle for partial-column tip pickup."""
        state = await self._controller.configure_nozzle_layout(
            PIPETTE_MOUNTS[mount],
            back_left_nozzle=nozzle,
            front_right_nozzle=nozzle,
            starting_nozzle=nozzle,
            tiprack_diameter=tiprack_diameter,
        )
        return self._nozzle_result(mount, state)

    @sila.UnobservableCommand(
        errors=[RunOwnershipError, PipetteNotAttachedError, NotHomedError, *SYSTEM_ERRORS, NozzleConfigurationError]
    )
    async def configure_rectangular_nozzle_layout(
        self,
        mount: PipetteMount,
        back_left_nozzle: NozzleIdentifier,
        front_right_nozzle: NozzleIdentifier,
        starting_nozzle: NozzleIdentifier,
        tiprack_diameter: TiprackDiameter,
    ) -> NozzleConfiguration:
        """Activate a rectangular nozzle range with an explicit critical-point nozzle."""
        state = await self._controller.configure_nozzle_layout(
            PIPETTE_MOUNTS[mount],
            back_left_nozzle=back_left_nozzle,
            front_right_nozzle=front_right_nozzle,
            starting_nozzle=starting_nozzle,
            tiprack_diameter=tiprack_diameter,
        )
        return self._nozzle_result(mount, state)

    @sila.UnobservableCommand(errors=[RunOwnershipError, PipetteNotAttachedError, NozzleConfigurationError])
    def get_nozzle_configuration(self, mount: PipetteMount) -> NozzleConfiguration:
        """Return the current nozzle rectangle for one pipette mount."""
        return self._nozzle_result(mount, self._controller.nozzle_configuration(PIPETTE_MOUNTS[mount]))

    @sila.UnobservableCommand(
        errors=[RunOwnershipError, PipetteNotAttachedError, NotHomedError, *SYSTEM_ERRORS, PipetteConfigurationError]
    )
    async def configure_tip_type(self, mount: PipetteMount, tip_volume: TipVolume) -> FlowRates:
        """
        Select the tip type the pipette uses next, by tip volume.

        The tip type sets the working volume and restores that tip's default
        aspirate, dispense, and blow-out flow rates. Run it with no tip attached,
        before picking up a tip of a different type.

        Args:
            Mount: Pipette mount (LEFT or RIGHT).
            TipVolume: Nominal volume of the tips in µL (50, 200, or 1000).

        Returns:
            FlowRates: The active tip type and its default flow rates.
        """
        state = await self._controller.configure_tip_type(PIPETTE_MOUNTS[mount], tip_volume)
        return self._flow_result(mount, state)

    @sila.UnobservableCommand(
        errors=[RunOwnershipError, PipetteNotAttachedError, NotHomedError, *SYSTEM_ERRORS, PipetteConfigurationError]
    )
    async def set_flow_rates(
        self,
        mount: PipetteMount,
        aspirate_flow_rate: FlowRate,
        dispense_flow_rate: FlowRate,
        blow_out_flow_rate: FlowRate,
    ) -> FlowRates:
        """
        Set the plunger flow rates that later liquid commands use.

        Aspirate, Dispense, BlowOut, Mix, and Transfer multiply these rates by
        their Rate parameter, so a Rate of 1 runs at exactly these µL/s. The
        rates stay until changed or until ConfigureTipType restores defaults.

        Args:
            Mount: Pipette mount (LEFT or RIGHT).
            AspirateFlowRate: Aspirate flow rate in µL/s.
            DispenseFlowRate: Dispense flow rate in µL/s.
            BlowOutFlowRate: Blow-out flow rate in µL/s.

        Returns:
            FlowRates: The active tip type and the flow rates now in effect.
        """
        state = await self._controller.set_flow_rates(
            PIPETTE_MOUNTS[mount], aspirate_flow_rate, dispense_flow_rate, blow_out_flow_rate
        )
        return self._flow_result(mount, state)

    @sila.UnobservableCommand(errors=[RunOwnershipError, PipetteNotAttachedError])
    def get_flow_rates(self, mount: PipetteMount) -> FlowRates:
        """Return the active tip type and flow rates of one pipette mount."""
        return self._flow_result(mount, self._controller.flow_rates(PIPETTE_MOUNTS[mount]))

    @staticmethod
    def _flow_result(mount: PipetteMount, state: object) -> FlowRates:
        return FlowRates(
            mount=mount,
            tip_volume=state.tip_volume,
            aspirate=state.aspirate,
            dispense=state.dispense,
            blow_out=state.blow_out,
        )

    @staticmethod
    def _nozzle_result(mount: PipetteMount, state: object) -> NozzleConfiguration:
        return NozzleConfiguration(
            mount=mount,
            starting_nozzle=state.starting_nozzle,
            back_left_nozzle=state.back_left_nozzle,
            front_right_nozzle=state.front_right_nozzle,
            active_nozzles=state.active_nozzles,
            tiprack_diameter=state.tiprack_diameter,
        )
