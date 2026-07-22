"""Narrow compatibility fixes for explicitly requested OT3 simulation."""

from __future__ import annotations

from opentrons.hardware_control import HardwareControlAPI


class OT3SimulatorCompatibilityAdapter:
    """
    Repair known simulator-only state drift without touching safety policy.

    Opentrons 8.8.1's OT3 simulator overwrites the gripper encoder during
    ``tip_drop_moves()`` without updating its jaw state. This adapter restores
    the intended closed-idle state only when the simulated gripper was safe
    before the tip operation. It refuses real hardware by construction.
    """

    def __init__(self, api: HardwareControlAPI) -> None:
        if not bool(api.is_simulator):
            message = "OT3SimulatorCompatibilityAdapter requires an explicit simulator backend."
            raise ValueError(message)
        object.__setattr__(self, "_api", api)

    @property
    def wrapped_api(self) -> HardwareControlAPI:
        """Return the concrete API for identity checks and lifecycle ownership."""
        return self._api

    def __setattr__(self, name: str, value: object) -> None:
        setattr(self._api, name, value)

    def __getattr__(self, name: str) -> object:
        return getattr(self._api, name)

    async def tip_drop_moves(self, *args: object, **kwargs: object) -> object:
        """Delegate a tip drop and repair only the known simulated jaw drift."""
        restore_idle_gripper = self._api.has_gripper() and self._api.gripper_jaw_can_home()
        result = await self._api.tip_drop_moves(*args, **kwargs)
        if restore_idle_gripper and not self._api.gripper_jaw_can_home():
            await self._api.ungrip()
            await self._api.idle_gripper()
        return result


__all__ = ["OT3SimulatorCompatibilityAdapter"]
