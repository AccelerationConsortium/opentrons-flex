"""Dependency-free values serialized by the Unitelabs Flex workflow client."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Mount(enum.Enum):
    """A Flex instrument mount."""

    LEFT = "LEFT"
    RIGHT = "RIGHT"
    GRIPPER = "GRIPPER"


class PipetteMount(enum.Enum):
    """A Flex mount that can hold a pipette."""

    LEFT = "LEFT"
    RIGHT = "RIGHT"


class VerifiedLiquidClass(enum.Enum):
    """Verified liquid-class definitions shipped by Opentrons."""

    WATER = "water"
    ETHANOL_80 = "ethanol_80"
    GLYCEROL_50 = "glycerol_50"


@dataclass
class LiquidPosition:
    """Absolute deck position of a pipette tip."""

    x: float
    y: float
    z: float


@dataclass
class WellGeometry:
    """Well geometry in deck coordinates."""

    center_x: float
    center_y: float
    bottom_z: float
    top_z: float
    size_x: float
    size_y: float


@dataclass
class TransferProfile:
    """Fully specified advanced transfer behavior."""

    aspirate_rate: float
    dispense_rate: float
    push_out: float
    air_gap: float
    mix_before_cycles: int
    mix_before_volume: float
    mix_after_cycles: int
    mix_after_volume: float
    blow_out: bool


@dataclass
class TipLocation:
    """Absolute deck position for a pipette tip operation."""

    x: float
    y: float
    z: float


@dataclass
class ThermocyclerProfileStep:
    """One thermocycler profile step."""

    temperature: float
    hold_time: float
    ramp_rate: float


__all__ = [
    "LiquidPosition",
    "Mount",
    "PipetteMount",
    "ThermocyclerProfileStep",
    "TipLocation",
    "TransferProfile",
    "VerifiedLiquidClass",
    "WellGeometry",
]
