"""
IO wrapper for Opentrons Flex (OT-3) motion control.

Unlike the OT-2 connector, which drives the Smoothie board over a serial port,
the Flex talks to its motor-controller boards over CAN bus. There is no
``SmoothieDriver`` to wrap. Instead this controller wraps the high-level
``HardwareControlAPI`` (the ``OT3API`` implementation), which already exposes
mount/axis motion, instrument caching, lights and gripper control on top of the
CAN layer. We work at that API seam — we do not reimplement the CAN protocol or
modify ot3-firmware.

Concurrency: the SiLA2 gRPC server and the in-process opentrons robot-server
share a single ``HardwareControlAPI``. ``HardwareProxy`` serialises every call
from the robot-server through an ``asyncio.Lock``. This controller is handed the
*same* lock so SiLA motion commands cannot interleave with HTTP-driven motion.
"""

import asyncio
import logging
import typing

from opentrons.hardware_control import HardwareControlAPI
from opentrons.hardware_control.types import Axis, OT3Mount
from opentrons.types import Point

from .hardware_proxy import _TimedLock

log = logging.getLogger(__name__)

# Axes that belong to each mount, used when homing "just this mount".
_MOUNT_AXES: dict[OT3Mount, list[Axis]] = {
    OT3Mount.LEFT: [Axis.Z_L, Axis.P_L],
    OT3Mount.RIGHT: [Axis.Z_R, Axis.P_R],
    OT3Mount.GRIPPER: [Axis.Z_G, Axis.G],
}


class FlexMotionController:
    """
    High-level motion controller for the Opentrons Flex, wrapping ``OT3API``.

    This is a thin, SiLA-agnostic wrapper that:
    1. Reuses the battle-tested ``OT3API`` implementation (CAN, calibration, etc.).
    2. Provides a focused surface for the SiLA2 features (motion, pipette, lights).
    3. Serialises hardware access through a lock shared with ``HardwareProxy``.

    Methods take Opentrons types (``OT3Mount``, ``Axis``, ``Point``); the SiLA
    features own the conversion to and from user-facing structures.
    """

    def __init__(
        self,
        api: HardwareControlAPI,
        lock: asyncio.Lock | None = None,
        lock_timeout_s: float | None = None,
    ) -> None:
        """
        Initialize with an existing ``HardwareControlAPI``.

        Use :meth:`build` for normal construction, or :meth:`from_api` when sharing
        the API and lock with the in-process robot-server.
        """
        self._api = api
        raw_lock = lock if lock is not None else asyncio.Lock()
        self._lock: _TimedLock = _TimedLock(raw_lock, lock_timeout_s)

    @classmethod
    async def build(
        cls,
        simulate: bool = True,
        lock_timeout_s: float | None = None,
    ) -> "FlexMotionController":
        """
        Build a standalone ``FlexMotionController`` (own ``OT3API``, own lock).

        Args:
            simulate: If True, build the OT3 hardware *simulator* backend rather than
                connecting to real CAN hardware. AGENTS.md mandates a simulation mode.
            lock_timeout_s: Seconds to wait for the hardware lock before raising
                ``TimeoutError``.
        """
        from opentrons.hardware_control.ot3api import OT3API

        if simulate:
            log.info("Building FlexMotionController in simulation mode")
            api = await OT3API.build_hardware_simulator()
        else:
            log.info("Building FlexMotionController for real Flex hardware (CAN)")
            api = await OT3API.build_hardware_controller()
        return cls(api=api, lock_timeout_s=lock_timeout_s)

    @classmethod
    def from_api(
        cls,
        api: HardwareControlAPI,
        lock: asyncio.Lock,
        lock_timeout_s: float | None = None,
    ) -> "FlexMotionController":
        """
        Wrap an already-built ``HardwareControlAPI``, sharing a lock with ``HardwareProxy``.

        Used in the in-process server mode where both the SiLA2 gRPC server and the
        opentrons HTTP server drive one ``OT3API``. The caller creates one
        ``asyncio.Lock`` and passes it to both this method and ``HardwareProxy`` so
        callers from both servers are serialised through the same lock.
        """
        return cls(api=api, lock=lock, lock_timeout_s=lock_timeout_s)

    # ------------------------------------------------------------------ status

    @property
    def is_simulating(self) -> bool:
        """Whether the underlying API is a simulator backend."""
        return bool(self._api.is_simulator)

    # ------------------------------------------------------------------ motion

    async def home(self, axes: list[Axis] | None = None) -> None:
        """Home the given axes (all axes when ``None``)."""
        async with self._lock:
            await self._api.home(axes=axes)

    async def home_mount(self, mount: OT3Mount) -> None:
        """Home only the axes belonging to one mount."""
        await self.home(_MOUNT_AXES.get(mount))

    async def move_to(self, mount: OT3Mount, point: Point, speed: float | None = None) -> Point:
        """Move ``mount`` to an absolute deck ``point`` and return the resulting position."""
        async with self._lock:
            await self._api.move_to(mount=mount, abs_position=point, speed=speed)
            return await self._api.gantry_position(mount, refresh=True)

    async def move_rel(self, mount: OT3Mount, delta: Point, speed: float | None = None) -> Point:
        """Move ``mount`` by ``delta`` and return the resulting position."""
        async with self._lock:
            await self._api.move_rel(mount=mount, delta=delta, speed=speed)
            return await self._api.gantry_position(mount, refresh=True)

    async def gantry_position(self, mount: OT3Mount) -> Point:
        """Return the current position of ``mount``."""
        async with self._lock:
            return await self._api.gantry_position(mount, refresh=True)

    async def stop(self) -> None:
        """Halt all motion. A re-home is required before further motion."""
        async with self._lock:
            await self._api.halt()

    def pause(self) -> None:
        """Pause execution (does not touch the bus directly; no lock needed)."""
        from opentrons.hardware_control.types import PauseType

        self._api.pause(PauseType.PAUSE)

    def resume(self) -> None:
        """Resume execution after a pause."""
        from opentrons.hardware_control.types import PauseType

        self._api.resume(PauseType.PAUSE)

    # --------------------------------------------------------------- pipettes

    async def cache_instruments(self) -> None:
        """Re-scan the mounts for attached pipettes."""
        async with self._lock:
            await self._api.cache_instruments()

    @property
    def attached_instruments(self) -> dict:
        """Mapping of mount -> attached pipette dict (empty dict when no pipette)."""
        return dict(self._api.attached_instruments)

    async def prepare_for_aspirate(self, mount: OT3Mount, rate: float = 1.0) -> None:
        """Position the plunger at bottom, ready to aspirate."""
        async with self._lock:
            await self._api.prepare_for_aspirate(mount=mount, rate=rate)

    async def aspirate(self, mount: OT3Mount, volume: float | None = None, rate: float = 1.0) -> None:
        """Aspirate ``volume`` µL on ``mount`` (full available volume when ``None``)."""
        async with self._lock:
            await self._api.aspirate(mount=mount, volume=volume, rate=rate)

    async def dispense(
        self, mount: OT3Mount, volume: float | None = None, rate: float = 1.0, push_out: float | None = None
    ) -> None:
        """Dispense ``volume`` µL on ``mount`` (current volume when ``None``)."""
        async with self._lock:
            await self._api.dispense(mount=mount, volume=volume, rate=rate, push_out=push_out)

    async def blow_out(self, mount: OT3Mount, volume: float | None = None) -> None:
        """Blow out any residual liquid on ``mount``."""
        async with self._lock:
            await self._api.blow_out(mount=mount, volume=volume)

    # ------------------------------------------------------------------ lights

    async def set_lights(self, button: bool | None = None, rails: bool | None = None) -> None:
        """Set the status-bar (``button``) and/or deck (``rails``) lights."""
        async with self._lock:
            await self._api.set_lights(button=button, rails=rails)

    async def get_lights(self) -> dict[str, bool]:
        """Return the current light state, e.g. ``{"button": bool, "rails": bool}``."""
        async with self._lock:
            return await self._api.get_lights()

    # ----------------------------------------------------------------- cleanup

    async def disconnect(self) -> None:
        """Release hardware resources owned by a standalone controller."""
        async with self._lock:
            await self._api.clean_up()


# Re-export for callers that map SiLA enums to Opentrons types.
__all__ = ["Axis", "FlexMotionController", "OT3Mount", "Point"]
