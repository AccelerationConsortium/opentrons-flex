"""Shared public pipette-mount vocabulary for Flex SiLA features."""

import enum

from opentrons.hardware_control.types import OT3Mount


class PipetteMount(enum.Enum):
    """A Flex mount that can hold a pipette."""

    LEFT = "LEFT"
    RIGHT = "RIGHT"


PIPETTE_MOUNTS: dict[PipetteMount, OT3Mount] = {
    PipetteMount.LEFT: OT3Mount.LEFT,
    PipetteMount.RIGHT: OT3Mount.RIGHT,
}


__all__ = ["PIPETTE_MOUNTS", "PipetteMount"]
