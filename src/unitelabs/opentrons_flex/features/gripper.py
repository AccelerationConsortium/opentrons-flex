"""
SiLA2 feature for the Opentrons Flex gripper.

Flex-only instrument: grips and releases labware for on-deck moves. Backed by
``FlexGripperController`` (a wrapper around ``OT3API`` gripper methods).

Note: per AGENTS.md, robotic movements are ideally modelled as *observable*
commands with progress and cancellation. The OT-2 connector currently exposes
its moves as unobservable commands; this feature follows that same convention
for consistency, and converting grip/ungrip to observable+cancellable commands
is tracked as a planned enhancement.
"""

import typing
from dataclasses import dataclass

from unitelabs.cdk import sila
from unitelabs.cdk.sila import constraints

from ..io import FlexGripperController, GripActionError, GripperNotAttachedError

# Flex gripper grip force range in Newtons (documented operating envelope).
_MIN_FORCE_N = 5.0
_MAX_FORCE_N = 25.0
_GripForce = typing.Annotated[
    float, constraints.MinimalInclusive(_MIN_FORCE_N), constraints.MaximalInclusive(_MAX_FORCE_N)
]


@dataclass
class GripperStatus:
    """Current gripper attachment and identity."""

    attached: bool
    model: str
    gripper_id: str
    state: str


class GripperFeature(sila.Feature):
    """SiLA2 feature for the Flex gripper: grip, ungrip, and home the jaw."""

    def __init__(self, controller: FlexGripperController):
        super().__init__(originator="ca.accelerationconsortium", category="robots")
        self._controller = controller

    @sila.UnobservableCommand(errors=[GripperNotAttachedError, GripActionError])
    async def grip(self, force: _GripForce) -> None:
        """
        Close the jaw to grip labware.

        Args:
            force: Grip force in Newtons (5-25 N).
        """
        await self._controller.grip(force_newtons=force)

    @sila.UnobservableCommand(errors=[GripperNotAttachedError, GripActionError])
    async def ungrip(self) -> None:
        """Open the jaw fully to release labware."""
        await self._controller.ungrip()

    @sila.UnobservableCommand(errors=[GripperNotAttachedError, GripActionError])
    async def home_jaw(self) -> None:
        """Home the gripper jaw to its reference position."""
        await self._controller.home_jaw()

    @sila.UnobservableProperty()
    def status(self) -> GripperStatus:
        """Gripper attachment status and identity."""
        info = self._controller.info
        if not info:
            return GripperStatus(attached=False, model="", gripper_id="", state="")
        return GripperStatus(
            attached=True,
            model=str(info.get("model") or ""),
            gripper_id=str(info.get("gripper_id") or ""),
            state=str(info.get("state") or ""),
        )
