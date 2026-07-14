"""SiLA2 feature for Opentrons Flex pipette detection and tip lifecycle."""

import enum
import typing
from dataclasses import dataclass

from opentrons.hardware_control.types import OT3Mount, TipStateType
from unitelabs.cdk import sila
from unitelabs.cdk.sila import constraints

from ..io import (
    FlexMotionController,
    MachineErrorStateError,
    MovementOutOfBoundsError,
    NotHomedError,
    PipetteNotAttachedError,
    StallDetectedError,
    TipDropError,
    TipPickupError,
    TipStateError,
)
from ._progress import OperationProgress, run_observable
from .motion_control import Mount


@dataclass
class PipetteInfo:
    """
    A pipette attached to one mount.

    Flex pipette models are e.g. ``flex_1channel_1000`` / ``flex_8channel_1000`` /
    ``flex_96channel_1000``. ``attached`` is False (and the other fields are empty
    / zero) when no pipette is present on that mount.
    """

    mount: Mount
    attached: bool
    model: str
    name: str
    pipette_id: str
    channels: int
    min_volume: float
    max_volume: float
    has_tip: bool


_PIPETTE_INFO_MOUNTS = {Mount.LEFT: OT3Mount.LEFT, Mount.RIGHT: OT3Mount.RIGHT}

_TIP_MOTION_ERRORS = [
    NotHomedError,
    MovementOutOfBoundsError,
    StallDetectedError,
    MachineErrorStateError,
]
_TIP_STATE_ERRORS = [PipetteNotAttachedError, TipStateError]


class TipPresence(enum.Enum):
    """Physical tip-presence state reported by the pipette sensor."""

    ABSENT = "ABSENT"
    PRESENT = "PRESENT"


class PipetteMount(enum.Enum):
    """A Flex mount that can hold a pipette."""

    LEFT = "LEFT"
    RIGHT = "RIGHT"


_PIPETTE_MOUNTS = {PipetteMount.LEFT: OT3Mount.LEFT, PipetteMount.RIGHT: OT3Mount.RIGHT}


def _tip_presence(state: TipStateType) -> TipPresence:
    return TipPresence[state.name]


class PipetteFeature(sila.Feature):
    """SiLA2 feature reporting pipettes and controlling their tip lifecycle."""

    def __init__(self, controller: FlexMotionController):
        super().__init__(originator="ca.accelerationconsortium", category="robots")
        self._controller = controller

    @sila.ObservableCommand()
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
        for mount, ot3_mount in _PIPETTE_INFO_MOUNTS.items():
            info = instruments.get(ot3_mount) or {}
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

    @sila.ObservableCommand(errors=[*_TIP_MOTION_ERRORS, TipPickupError, *_TIP_STATE_ERRORS])
    async def pick_up_tip(
        self,
        mount: PipetteMount,
        tip_length: typing.Annotated[float, constraints.MinimalExclusive(0.0)],
        presses: typing.Annotated[int, constraints.MinimalInclusive(0)],
        increment: typing.Annotated[float, constraints.MinimalInclusive(0.0)],
        prep_after: bool,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> TipPresence:
        """
        Pick up a tip at the pipette's current deck position and verify it.

        The caller must first move the pipette into the intended tip well. A
        ``presses`` or ``increment`` value of 0 selects the hardware default.

        Args:
            mount: Pipette mount to operate (LEFT or RIGHT).
            tip_length: Physical tip length in millimetres.
            presses: Number of pickup presses, or 0 for the hardware default.
            increment: Additional pickup depth per press in mm, or 0 for default.
            prep_after: Prepare the plunger for aspiration after pickup.

        Yields:
            Update: Current tip-pickup progress update.

        Returns:
            PRESENT after the hardware sensor verifies the pickup.
        """
        state = await run_observable(
            status,
            intermediate,
            f"Picking up a tip on {mount.value} at the current position.",
            f"Tip pickup on {mount.value} verified.",
            f"Tip pickup on {mount.value} cancelled.",
            self._controller.pick_up_tip(
                _PIPETTE_MOUNTS[mount],
                tip_length=tip_length,
                presses=presses if presses > 0 else None,
                increment=increment if increment > 0 else None,
                prep_after=prep_after,
            ),
        )
        return _tip_presence(state)

    @sila.ObservableCommand(errors=[*_TIP_MOTION_ERRORS, TipDropError, *_TIP_STATE_ERRORS])
    async def drop_tip(
        self,
        mount: PipetteMount,
        home_after: bool,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> TipPresence:
        """
        Drop the attached tip at the pipette's current position and verify it.

        The caller is responsible for first moving to a safe trash or return-tip
        location.

        Args:
            mount: Pipette mount to operate (LEFT or RIGHT).
            home_after: Home the pipette plunger after releasing the tip.

        Yields:
            Update: Current tip-drop progress update.

        Returns:
            ABSENT after the hardware sensor verifies the drop.
        """
        state = await run_observable(
            status,
            intermediate,
            f"Dropping the tip on {mount.value} at the current position.",
            f"Tip drop on {mount.value} verified.",
            f"Tip drop on {mount.value} cancelled.",
            self._controller.drop_tip(_PIPETTE_MOUNTS[mount], home_after=home_after),
        )
        return _tip_presence(state)

    @sila.ObservableCommand(errors=_TIP_STATE_ERRORS)
    async def get_tip_presence(
        self,
        mount: PipetteMount,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> TipPresence:
        """
        Read the tip-presence sensor on one pipette mount.

        Yields:
            Update: Current tip-state read progress update.
        """
        state = await run_observable(
            status,
            intermediate,
            f"Reading tip presence on {mount.value}.",
            f"Tip presence on {mount.value} read.",
            f"Tip presence read on {mount.value} cancelled.",
            self._controller.get_tip_presence(_PIPETTE_MOUNTS[mount]),
        )
        return _tip_presence(state)
