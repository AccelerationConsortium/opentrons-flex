"""SiLA2 feature for Opentrons Flex pipette detection."""

from dataclasses import dataclass

from opentrons.hardware_control.types import OT3Mount
from unitelabs.cdk import sila

from ..io import FlexMotionController
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


_PIPETTE_MOUNTS = {Mount.LEFT: OT3Mount.LEFT, Mount.RIGHT: OT3Mount.RIGHT}


class PipetteFeature(sila.Feature):
    """SiLA2 feature reporting the pipettes attached to the Flex mounts."""

    def __init__(self, controller: FlexMotionController):
        super().__init__(originator="ca.accelerationconsortium", category="robots")
        self._controller = controller

    @sila.UnobservableCommand()
    async def get_attached_pipettes(self) -> list[PipetteInfo]:
        """
        Re-scan both pipette mounts and report what is attached.

        Returns:
            Two ``PipetteInfo`` entries (LEFT then RIGHT). ``attached`` is False for
            an empty mount.
        """
        await self._controller.cache_instruments()
        instruments = self._controller.attached_instruments

        results: list[PipetteInfo] = []
        for mount, ot3_mount in _PIPETTE_MOUNTS.items():
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
