"""End-to-end gRPC integration tests for the Flex MotionControlFeature (simulate mode).

Spins up a real SiLA gRPC server on a dynamic port (see conftest ``sila_channel``)
and makes real gRPC calls over the wire using the server's own protobuf codec.
Confirms the full chain:
  gRPC channel → SiLA server → MotionControlFeature → FlexMotionController → OT3API

``pb.encode`` accepts the feature method's snake_case parameter names; ``pb.decode``
returns a single-entry dict whose value is the native dataclass the method returned
(empty for void commands).
"""

import typing

import grpc.aio
import pytest
import pytest_asyncio

from unitelabs.opentrons_flex.features.motion_control import Lights, Mount, Position
from .observable import call_observable

_PKG = "sila2.ca.accelerationconsortium.robots.motioncontrolfeature.v1"
_SERVICE = f"{_PKG}.MotionControlFeature"

T = typing.TypeVar("T")


class _MotionClient:
    """Raw gRPC client for MotionControlFeature using the server's protobuf codec."""

    def __init__(self, channel: grpc.aio.Channel, pb: object) -> None:
        self._ch = channel
        self._pb = pb

    @staticmethod
    def _single(decoded: dict, expected_type: type[T]) -> T:
        value = next(iter(decoded.values()))
        assert isinstance(value, expected_type), (
            f"Expected {expected_type.__name__}, got {type(value).__name__}: {value}"
        )
        return value

    async def _call(self, method: str, params: dict | None = None) -> dict:
        req = await self._pb.encode(f"{_PKG}.{method}_Parameters", params or {})
        stub = self._ch.unary_unary(f"/{_SERVICE}/{method}")
        resp_bytes = await stub(req)
        return await self._pb.decode(f"{_PKG}.{method}_Responses", resp_bytes)

    async def _observable(self, method: str, params: dict | None = None) -> dict:
        return await call_observable(self._ch, self._pb, _SERVICE, _PKG, method, params)

    async def _get_property(self, name: str) -> dict:
        stub = self._ch.unary_unary(f"/{_SERVICE}/{name}")
        resp_bytes = await stub(b"")
        return await self._pb.decode(f"{_PKG}.{name}_Responses", resp_bytes)

    async def home(self) -> None:
        await self._observable("Home")

    async def home_mount(self, mount: Mount) -> None:
        await self._observable("HomeMount", {"mount": mount})

    async def get_position(self, mount: Mount) -> Position:
        return self._single(await self._observable("GetPosition", {"mount": mount}), Position)

    async def move_to(self, mount: Mount, x: float, y: float, z: float, speed: float = 0.0) -> Position:
        return self._single(
            await self._observable("MoveTo", {"mount": mount, "x": x, "y": y, "z": z, "speed": speed}),
            Position,
        )

    async def move_relative(
        self, mount: Mount, delta_x: float, delta_y: float, delta_z: float, speed: float = 0.0
    ) -> Position:
        return self._single(
            await self._observable(
                "MoveRelative",
                {"mount": mount, "delta_x": delta_x, "delta_y": delta_y, "delta_z": delta_z, "speed": speed},
            ),
            Position,
        )

    async def set_lights(self, button: bool, rails: bool) -> Lights:
        return self._single(await self._observable("SetLights", {"button": button, "rails": rails}), Lights)

    async def emergency_stop(self) -> str:
        return next(iter((await self._observable("EmergencyStop")).values()))

    async def pause(self) -> str:
        return next(iter((await self._observable("Pause")).values()))

    async def resume(self) -> str:
        return next(iter((await self._observable("Resume")).values()))

    async def get_lights(self) -> Lights:
        return self._single(await self._get_property("Get_Lights"), Lights)

    async def get_is_simulating(self) -> bool:
        return next(iter((await self._get_property("Get_IsSimulating")).values()))


@pytest_asyncio.fixture
async def client(sila_channel) -> _MotionClient:
    channel, pb = sila_channel
    return _MotionClient(channel, pb)


@pytest_asyncio.fixture
async def homed_position(client: _MotionClient) -> Position:
    """Home the robot and return the LEFT mount's resulting position."""
    await client.home()
    return await client.get_position(Mount.LEFT)


# ── simulation flag ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_is_simulating_is_true(client: _MotionClient) -> None:
    """Get_IsSimulating returns True in simulate mode (over the wire)."""
    assert await client.get_is_simulating() is True


# ── home / position ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_home_then_get_position_returns_a_position(client: _MotionClient) -> None:
    """Home, then GetPosition decodes to a Position dataclass over the wire."""
    await client.home()
    pos = await client.get_position(Mount.LEFT)
    assert isinstance(pos, Position)


@pytest.mark.asyncio
async def test_home_mount_then_get_position_returns_a_position(client: _MotionClient) -> None:
    """HomeMount is exposed over gRPC and leaves the addressed mount queryable."""
    await client.home_mount(Mount.LEFT)
    pos = await client.get_position(Mount.LEFT)
    assert isinstance(pos, Position)


@pytest.mark.asyncio
async def test_home_position_is_reproducible(client: _MotionClient, homed_position: Position) -> None:
    """A second home returns the same coordinates as the first."""
    await client.home()
    pos = await client.get_position(Mount.LEFT)
    assert pos.x == pytest.approx(homed_position.x)
    assert pos.y == pytest.approx(homed_position.y)
    assert pos.z == pytest.approx(homed_position.z)


# ── motion ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_move_to_sets_absolute_position(client: _MotionClient, homed_position: Position) -> None:
    """MoveTo returns the requested absolute coordinates."""
    target_x = homed_position.x - 20.0
    target_y = homed_position.y - 20.0
    target_z = homed_position.z - 20.0
    result = await client.move_to(Mount.LEFT, target_x, target_y, target_z)
    assert result.x == pytest.approx(target_x)
    assert result.y == pytest.approx(target_y)
    assert result.z == pytest.approx(target_z)


@pytest.mark.asyncio
async def test_move_relative_accumulates(client: _MotionClient, homed_position: Position) -> None:
    """Two relative Y moves of -10 mm produce Y = homed_Y - 20 mm."""
    await client.move_relative(Mount.LEFT, 0.0, -10.0, 0.0)
    result = await client.move_relative(Mount.LEFT, 0.0, -10.0, 0.0)
    assert result.y == pytest.approx(homed_position.y - 20.0)


# ── lights ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_lights_returns_lights(client: _MotionClient) -> None:
    """SetLights decodes to a Lights dataclass over the wire."""
    result = await client.set_lights(button=True, rails=False)
    assert isinstance(result, Lights)


@pytest.mark.asyncio
async def test_get_lights_property(client: _MotionClient) -> None:
    """The Lights property decodes to a Lights dataclass over the wire."""
    assert isinstance(await client.get_lights(), Lights)


# ── stop / pause / resume ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pause_and_resume_return_status_strings(client: _MotionClient) -> None:
    """Pause and Resume return human-readable status strings."""
    assert isinstance(await client.pause(), str)
    assert isinstance(await client.resume(), str)


@pytest.mark.asyncio
async def test_emergency_stop_returns_status_string(client: _MotionClient) -> None:
    """EmergencyStop returns a status string mentioning the required re-home."""
    msg = await client.emergency_stop()
    assert "re-home" in msg.lower()
