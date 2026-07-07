"""Stage 4 — hardware-in-the-loop (HITL) motion tests against a real OT-Flex.

These tests run **only** against a real robot and are skipped otherwise. Run them
with the SiLA address of a live Flex::

    uv run pytest tests/integration/hardware/ --robot <flex-ip>:50051 -v

Two rules make these tests trustworthy on real hardware:

Pitfall #1 — mode is never ambiguous. The autouse ``_record_run_context`` fixture
(see ``tests/integration/conftest.py``) records ``mode=hardware``, the target and
the ``device_id`` to every result, and a ``hardware_only`` test that somehow runs
in smoketest mode fails loudly instead of silently passing against the simulator.

Pitfall #2 — a movement command that "returns" is NOT assumed to have succeeded.
After **every** movement we query ``MachineStatus`` and assert the robot did not
silently enter a hardware error state (E-stop engaged, etc.). If the connector's
post-move guard is working, a machine that faulted mid-move would already have
raised ``MachineErrorStateError`` before we get here; ``assert_machine_ok`` is the
belt-and-braces confirmation from the client side.

Only safe, small, reversible moves are issued (home, tiny Z jog up then back,
lights). No labware, no liquid, no XY traversal across the deck.
"""

import grpc.aio
import pytest
import pytest_asyncio

from unitelabs.opentrons_flex.features.motion_control import MachineStatus, Mount, Position

from ..observable import call_observable

_PKG = "sila2.ca.accelerationconsortium.robots.motioncontrolfeature.v1"
_SERVICE = f"{_PKG}.MotionControlFeature"

# Safe HITL jog distance (mm): small, along +Z (up, away from the deck).
_JOG_MM = 5.0

pytestmark = pytest.mark.hardware_only


class _HitlMotion:
    """Minimal SiLA MotionControl client with a built-in error-state check."""

    def __init__(self, channel: grpc.aio.Channel, pb: object) -> None:
        self._ch = channel
        self._pb = pb

    async def _observable(self, method: str, params: dict | None = None) -> dict:
        return await call_observable(self._ch, self._pb, _SERVICE, _PKG, method, params, timeout_s=60.0)

    async def _property(self, name: str) -> dict:
        stub = self._ch.unary_unary(f"/{_SERVICE}/{name}")
        return await self._pb.decode(f"{_PKG}.{name}_Responses", await stub(b""))

    async def machine_status(self) -> MachineStatus:
        value = next(iter((await self._property("Get_MachineStatus")).values()))
        assert isinstance(value, MachineStatus), f"expected MachineStatus, got {type(value).__name__}"
        return value

    async def assert_machine_ok(self, after: str) -> MachineStatus:
        """Query machine status after a command and fail on any hidden error state."""
        status = await self.machine_status()
        assert status.is_error_state is False, (
            f"robot entered an error state {after}: estop={status.estop} "
            f"door_open={status.door_open} :: {status.message}"
        )
        return status

    async def home(self) -> None:
        await self._observable("Home")

    async def get_position(self, mount: Mount) -> Position:
        value = next(iter((await self._observable("GetPosition", {"mount": mount})).values()))
        assert isinstance(value, Position)
        return value

    async def move_relative(self, mount: Mount, dx: float, dy: float, dz: float) -> Position:
        value = next(
            iter(
                (
                    await self._observable(
                        "MoveRelative",
                        {"mount": mount, "delta_x": dx, "delta_y": dy, "delta_z": dz, "speed": 0.0},
                    )
                ).values()
            )
        )
        assert isinstance(value, Position)
        return value

    async def set_lights(self, button: bool, rails: bool) -> None:
        await self._observable("SetLights", {"button": button, "rails": rails})


@pytest_asyncio.fixture
async def motion(sila_channel) -> _HitlMotion:
    channel, pb = sila_channel
    return _HitlMotion(channel, pb)


@pytest.mark.asyncio
async def test_precondition_machine_not_simulating(sila_channel) -> None:
    """Sanity: a hardware run must be talking to a real robot, not the simulator."""
    channel, pb = sila_channel
    stub = channel.unary_unary(f"/{_SERVICE}/Get_IsSimulating")
    decoded = await pb.decode(f"{_PKG}.Get_IsSimulating_Responses", await stub(b""))
    assert next(iter(decoded.values())) is False, "connected to a simulator under --robot; check the target"


@pytest.mark.asyncio
async def test_machine_status_is_readable_and_clean_at_rest(motion: _HitlMotion) -> None:
    """The robot reports a legible, non-error safety state before any motion."""
    status = await motion.machine_status()
    assert status.estop in {"DISENGAGED", "NOT_PRESENT"}, (
        f"robot is not ready (estop={status.estop}); clear the E-stop before running HITL tests"
    )
    assert status.is_error_state is False, status.message


@pytest.mark.asyncio
async def test_home_then_no_hidden_error(motion: _HitlMotion) -> None:
    """Home the robot, then confirm no hidden error state was left behind."""
    await motion.home()
    await motion.assert_machine_ok("after home")


@pytest.mark.asyncio
async def test_small_z_jog_roundtrip_then_no_hidden_error(motion: _HitlMotion) -> None:
    """Jog the LEFT mount up and back, checking machine status after every move."""
    await motion.home()
    await motion.assert_machine_ok("after home")

    start = await motion.get_position(Mount.LEFT)

    down = await motion.move_relative(Mount.LEFT, 0.0, 0.0, -_JOG_MM)
    await motion.assert_machine_ok("after -Z jog")
    assert down.z == pytest.approx(start.z - _JOG_MM, abs=0.5)

    back = await motion.move_relative(Mount.LEFT, 0.0, 0.0, _JOG_MM)
    await motion.assert_machine_ok("after +Z jog")
    assert back.z == pytest.approx(start.z, abs=0.5)


@pytest.mark.asyncio
async def test_lights_command_then_no_hidden_error(motion: _HitlMotion) -> None:
    """A non-motion command still gets a post-command machine-status confirmation."""
    await motion.set_lights(button=True, rails=True)
    await motion.assert_machine_ok("after set lights")
    await motion.set_lights(button=False, rails=False)
    await motion.assert_machine_ok("after set lights off")
