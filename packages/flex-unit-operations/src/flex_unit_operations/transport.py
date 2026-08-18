"""
SiLA2 transport for Flex unit operations.

A thin, typed façade over the connector's SiLA features. It owns no control logic:
each method is one SiLA command or property read. Connection setup, retry and error
classification come from ``sila_toolkit`` rather than being reimplemented here.

Wire conventions worth knowing:

* Parameters and response fields use SiLA identifiers in PascalCase (``Mount``,
  ``X``, ``Volume``), not the connector's Python names.
* Command responses come back as dicts keyed ``Response0``, ``Response1``, ... The
  server's own dataclasses are not available to a generic client.
* An enum crosses the wire as its lowercased member name, so ``Mount.GRIPPER``
  is sent as ``"gripper"``.
"""

from __future__ import annotations

import typing

from sila_toolkit import AsyncSilaTransport, connect_sila

from .errors import ConnectorError
from .geometry import Point

MOTION = "ca.accelerationconsortium/robots/MotionController/v2"
GRIPPER = "ca.accelerationconsortium/robots/GripperController/v1"
TIP = "ca.accelerationconsortium/robots/TipController/v1"
LIQUID = "ca.accelerationconsortium/robots/LiquidHandlingController/v1"
PIPETTE = "ca.accelerationconsortium/robots/PipetteController/v1"

FEATURES = (MOTION, GRIPPER, TIP, LIQUID, PIPETTE)

#: Mount identifiers, lowercased for the wire as SiLA encodes enum members.
LEFT = "left"
RIGHT = "right"
GRIPPER_MOUNT = "gripper"

_RESPONSE = "Response0"

#: Transfer profile that performs a plain aspirate/dispense with a blow-out and no
#: mixing or air gap. Rates of 1.0 mean "the pipette's configured default".
DEFAULT_PROFILE: dict[str, object] = {
    "AspirateRate": 1.0,
    "DispenseRate": 1.0,
    "PushOut": 0.0,
    "AirGap": 0.0,
    "MixBeforeCycles": 0,
    "MixBeforeVolume": 0.0,
    "MixAfterCycles": 0,
    "MixAfterVolume": 0.0,
    "BlowOut": True,
}


def _xyz(point: Point) -> dict[str, float]:
    return {"X": point.x, "Y": point.y, "Z": point.z}


class FlexTransport(AsyncSilaTransport):
    """A connected SiLA client for the Flex connector's robot features."""

    def __init__(self, client: object, features: dict[str, object]) -> None:
        self._client = client
        self._features = features

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int = 50051,
        *,
        timeout_ms: float = 30000,
        max_retries: int = 2,
    ) -> FlexTransport:
        """
        Open a connection and load every feature the unit operations use.

        Note the connector binds robot loopback in SiLA-only unit mode, so reaching it
        from another machine needs a forwarded port:
        ``ssh -N -L 50052:127.0.0.1:50051 root@<robot>``.
        """
        result = await connect_sila(
            host,
            port,
            feature_fqis=FEATURES,
            tls=False,
            timeout_ms=timeout_ms,
            max_retries=max_retries,
        )
        return cls(result.client, result.features)

    async def close(self) -> None:
        """Close the underlying SiLA client."""
        await self._client.close()

    async def _command(
        self,
        feature: str,
        name: str,
        params: dict[str, object] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict:
        """
        Run one SiLA command and return its responses.

        Most of the connector's commands are observable, because they drive physical
        motion: ``execute()`` hands back a ``CommandExecution`` that has to be awaited
        for the result, rather than the responses themselves. Unobservable commands
        return the response mapping directly, so both shapes are handled here and the
        callers stay uniform.
        """
        try:
            result = await self._features[feature].commands[name].execute(params or {})
            if hasattr(result, "get_responses"):
                result = await result.get_responses(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - re-raised with the connector's own message
            msg = f"{feature.rsplit('/', 2)[-2]}.{name} failed: {exc}"
            raise ConnectorError(msg) from exc
        return dict(result) if result is not None else {}

    async def _property(self, feature: str, name: str) -> object:
        try:
            return await self._features[feature].properties[name].read()
        except Exception as exc:  # noqa: BLE001 - re-raised with the connector's own message
            msg = f"{feature.rsplit('/', 2)[-2]}.{name} read failed: {exc}"
            raise ConnectorError(msg) from exc

    # ---------------- motion ----------------

    async def machine_status(self) -> object:
        """Read the aggregate machine state (estop, door, error, homed flags)."""
        return await self._property(MOTION, "MachineStatus")

    async def is_simulating(self) -> bool:
        """True when the connector is backed by the OT3 simulator, not real hardware."""
        return bool(await self._property(MOTION, "IsSimulating"))

    async def home_mount(self, mount: str) -> None:
        """Home one mount."""
        await self._command(MOTION, "HomeMount", {"Mount": mount})

    async def move_to(self, mount: str, point: Point, *, speed: float = 0.0) -> None:
        """Move a mount to an absolute deck coordinate. Speed 0 means the default."""
        await self._command(MOTION, "MoveTo", {"Mount": mount, "X": point.x, "Y": point.y, "Z": point.z, "Speed": speed})

    async def get_position(self, mount: str) -> dict:
        """Read a mount's current deck coordinate."""
        return await self._command(MOTION, "GetPosition", {"Mount": mount})

    # ---------------- gripper ----------------

    async def gripper_status(self) -> object:
        """Read gripper attachment and jaw state."""
        return await self._property(GRIPPER, "Status")

    async def jaw_width(self) -> float:
        """Read the current jaw opening in millimetres."""
        return float(typing.cast(float, await self._property(GRIPPER, "JawWidth")))

    async def home_jaw(self) -> None:
        """Home the gripper jaw."""
        await self._command(GRIPPER, "HomeJaw")

    async def grip(self, force_newtons: float) -> None:
        """Close the jaws at the given force, wherever the gripper currently is."""
        await self._command(GRIPPER, "Grip", {"Force": force_newtons})

    async def ungrip(self, force_newtons: float) -> None:
        """Open the jaws, releasing whatever is held."""
        await self._command(GRIPPER, "Ungrip", {"Force": force_newtons})

    # ---------------- pipettes and tips ----------------

    async def attached_pipettes(self) -> object:
        """List the attached pipettes with their mounts and volume ranges."""
        return (await self._command(PIPETTE, "GetAttachedPipettes")).get(_RESPONSE)

    async def configure_full_nozzle(self, mount: str, tiprack_diameter: float) -> None:
        """Select the full-nozzle layout for a pipette."""
        await self._command(
            PIPETTE,
            "ConfigureFullNozzleLayout",
            {"Mount": mount, "TiprackDiameter": tiprack_diameter},
        )

    async def tip_presence(self, mount: str) -> object:
        """Read whether a tip is sensed on a mount."""
        return (await self._command(TIP, "GetTipPresence", {"Mount": mount})).get(_RESPONSE)

    async def pick_up_tip(self, mount: str, location: Point, tip_length: float, *, prep_after: bool = False) -> object:
        """Pick up a tip at a deck coordinate and return the resulting tip presence."""
        return (
            await self._command(
                TIP,
                "PickUpTip",
                {
                    "Mount": mount,
                    "Location": _xyz(location),
                    "TipLength": tip_length,
                    "PrepAfter": prep_after,
                },
            )
        ).get(_RESPONSE)

    async def drop_tip(self, mount: str, location: Point, *, home_after: bool = False) -> object:
        """Drop the tip at a deck coordinate and return the resulting tip presence."""
        return (
            await self._command(
                TIP,
                "DropTip",
                {"Mount": mount, "Location": _xyz(location), "HomeAfter": home_after},
            )
        ).get(_RESPONSE)

    # ---------------- liquid ----------------

    async def transfer(
        self,
        mount: str,
        *,
        source: Point,
        source_retract: Point,
        destination: Point,
        destination_retract: Point,
        volume_ul: float,
        profile: dict[str, object] | None = None,
    ) -> object:
        """Aspirate at ``source`` and dispense at ``destination`` in one atomic command."""
        return (
            await self._command(
                LIQUID,
                "Transfer",
                {
                    "Mount": mount,
                    "Source": _xyz(source),
                    "SourceRetract": _xyz(source_retract),
                    "Destination": _xyz(destination),
                    "DestinationRetract": _xyz(destination_retract),
                    "Volume": volume_ul,
                    "Profile": dict(profile or DEFAULT_PROFILE),
                },
            )
        ).get(_RESPONSE)
