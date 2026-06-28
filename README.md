# Opentrons Flex

A SiLA2 connector for the Opentrons **Flex** (OT-3) liquid-handling robot that also
replaces the standard Opentrons robot-server HTTP API. Both servers share a single
hardware API instance so they cannot conflict over the CAN bus.

This connector is adapted from the [opentrons-ot2 connector](https://github.com/AccelerationConsortium/opentrons-ot2).
The key difference: the Flex has no Smoothie serial board — motion, pipettes, the
gripper and calibration are all driven through the high-level `OT3API`
(`opentrons.hardware_control.ot3api`) over CAN. The connector wraps that host API; it
does **not** modify the Flex motor-controller firmware.

## Architecture

This project runs two servers in the same process when deployed to a real Flex:

| Server | Protocol | Port | Purpose |
|--------|----------|------|---------|
| SiLA2 connector | gRPC | 50051 | Lab automation clients (SiLA Browser, UniteLabs platform) |
| Opentrons robot-server | HTTP REST | 31950 | Opentrons App, any REST client |

Both servers are backed by one shared `OT3API` (`HardwareControlAPI`) wrapped in
`HardwareProxy` — an `asyncio.Lock` around every hardware call. This serialises the
SiLA gRPC server and the in-process HTTP robot-server so their CAN commands cannot
interleave, and avoids the "hardware already initialised" error two separate
processes would hit.

The stock `opentrons-robot-server` systemd service is disabled on deployment. Our
`sila2-connector` service owns the hardware and starts the HTTP API in-process via
uvicorn on a Unix domain socket (`/run/aiohttp.sock`). nginx on the Flex proxies
external TCP port 31950 to that socket, so the HTTP API is reachable at
`http://<flex-ip>:31950` exactly as with the stock service.

### SiLA features

| Feature | Commands / properties |
|---------|------------------------|
| `MotionControlFeature` | `Home`, `HomeMount`, `MoveTo`, `MoveRelative`, `GetPosition`, `Aspirate`, `Dispense`, `BlowOut`, `PrepareForAspirate`, `EmergencyStop`, `Pause`, `Resume`, `SetLights`; `Lights`, `IsSimulating` |
| `PipetteFeature` | `GetAttachedPipettes` (Flex pipette models, per mount) |
| `GripperFeature` | `Grip`, `Ungrip`, `HomeJaw`; `Status` (Flex-only instrument) |
| `CalibrationFeature` | `CalibratePipette`, `CalibrateGripperJaw`, `CalibrateDeck` (automatic probe-based routines) |
| Module features | `HeaterShaker`, `Thermocycler`, `Temperature` (registered when attached) |

Motion is exposed per **mount** (`LEFT`, `RIGHT`, `GRIPPER`) in deck coordinates
(x, y, z mm), matching how `OT3API` models the Flex. The **Magnetic Module** is not
supported on the Flex (it is replaced by the passive Magnetic Block).

**Key source files:**

- `src/unitelabs/opentrons_flex/__init__.py` — `create_app()` and
  `_create_app_with_robot_server()` (in-process HTTP server startup, OT3API)
- `src/unitelabs/opentrons_flex/io/flex_motion.py` — `FlexMotionController` (OT3API wrapper)
- `src/unitelabs/opentrons_flex/io/_errors.py` — defined SiLA errors + OT3API error translation
- `tests/integration/` — end-to-end gRPC + HTTP tests (simulator or live Flex)

## Getting started

Requires [uv](https://docs.astral.sh/uv/) (`pipx install uv`).

```sh
git clone https://github.com/AccelerationConsortium/opentrons-flex.git
cd opentrons-flex
uv sync --all-extras
```

Run the connector in **simulator** mode (no hardware, OT3 simulator backend):

```sh
uv run connector start --app unitelabs.opentrons_flex:create_app -vvv
```

The connector reads config from a `config.json` (see `config/flex_config.json` for a
template). `use_simulator: true` selects the OT3 simulator; `with_robot_server: true`
additionally starts the in-process HTTP robot-server.

## Testing

The test suite runs **fully offline** against the real opentrons OT3 *simulator* — no
robot required. It needs `opentrons` and (for the integration/HTTP tests) `httpx`;
the real `unitelabs-cdk` (public on PyPI) is used when installed, and a lightweight
stub in `tests/conftest.py` lets the simulation tests run even without it.

```sh
# Everything except the live-robot HTTP tests (which skip without a target)
uv run pytest

# Just the unit + simulation tests
uv run pytest tests/io tests/features

# gRPC integration tests over the wire (in-process SiLA server + OT3 simulator)
uv run pytest tests/integration -k grpc
```

What the suite covers without hardware:

- **Unit / simulation** (`tests/io`, `tests/features`) — controllers and SiLA features
  driven against the OT3 simulator; SiLA feature-definition generation with the real CDK.
- **gRPC integration** (`tests/integration/test_grpc_*`) — real gRPC calls over a
  dynamic port through the full chain `gRPC → SiLA server → feature → OT3API`, including
  defined-execution-errors propagating over the wire.

### Testing against a real Flex

Once the connector is deployed and running on a Flex (see **Deploying** below), point
the same integration tests at it. `--robot HOST:50051` runs the gRPC tests against the
live SiLA server; `--robot-http HOST:31950` (or `--robot`, which derives it) runs the
HTTP API tests against the in-process robot-server.

```sh
# gRPC feature tests against the live Flex (simulator-only cases auto-skip)
uv run pytest tests/integration -k grpc --robot <flex-ip>:50051

# HTTP robot-server API tests against the live Flex
uv run pytest tests/integration/http_api --robot-http <flex-ip>:31950

# Both, in one run
uv run pytest tests/integration --robot <flex-ip>:50051
```

Tests marked `@pytest.mark.simulator_only` (e.g. "IsSimulating is True") are skipped
automatically when `--robot` is set. Tests marked `@pytest.mark.robot_http_only` only
run when an HTTP target is provided. Motion assertions compare against a freshly
captured `homed_position` fixture rather than hardcoded coordinates, so they hold on
both the simulator and real firmware.

To exercise the in-process HTTP stack locally (needs the Opentrons `robot_server`
system package installed in the environment):

```sh
uv run pytest tests/integration/http_api --with-http-server
```

## Deploying to the Flex

The Flex host is **aarch64** (ARM64) with a modern glibc. Unlike the OT-2 (armv7l,
glibc 2.25, which needed grpcio/OpenSSL compiled from source), standard PyPI
`manylinux_2_17_aarch64` wheels install directly.

### 1. Build aarch64 wheels

```sh
docker buildx build --platform linux/arm64 -f Dockerfile.build \
    --target export --output type=local,dest=dist_arm .
```

This produces `dist_arm/` (the connector wheel + all runtime deps). The Opentrons
`opentrons`/`robot_server` system packages are intentionally excluded — the robot
venv inherits them via `--system-site-packages`.

### 2. Deploy and install the service

```sh
# Copy wheels + config, create the venv on the robot
./deploy.sh <flex-ip>

# Disable the stock robot-server and install sila2-connector as a systemd service
./scripts/install_connector_service.sh <flex-ip>
```

`config/flex_config.json` is shipped as the robot's `config.json`. For a machine-specific
override, create `config/flex_config.local.json` (gitignored) and `deploy.sh` will prefer it.

### 3. Switch modes / iterate

```sh
# Switch between the SiLA connector and the stock opentrons robot-server (persists across reboot)
./scripts/switch_mode.sh <flex-ip> connector
./scripts/switch_mode.sh <flex-ip> opentrons

# Push Python source changes without rebuilding wheels, then restart the service
./scripts/deploy_python_changes.sh <flex-ip>

# Tail logs
ssh root@<flex-ip> 'journalctl -u sila2-connector -f'
```

In `connector` mode both interfaces are live: SiLA2 gRPC on `50051` and the opentrons
HTTP API on `31950` (nginx → `/run/aiohttp.sock`).

> **Hardware-specific notes (verify on your device):** the systemd unit assumes the
> stock service is `opentrons-robot-server` and that the opentrons stack auto-detects
> OT-3 hardware (no OT-2 `RUNNING_ON_PI`/`OT_SMOOTHIE_ID` env is set). The Python minor
> version of the robot venv is resolved dynamically by the deploy scripts.

## Connecting a client

Use the [SiLA Browser](https://gitlab.com/unitelabs/sila2/sila-browser) against
`<flex-ip>:50051`, or any SiLA2 client. The Opentrons App and any REST client can use
the HTTP API at `http://<flex-ip>:31950` unchanged.

### TLS

To secure the SiLA channel, install `cryptography` and generate certificates:

```sh
uv pip install cryptography
certificate generate            # writes cert.pem / key.pem, can update config.json
```

Set `sila_server.tls: true`, `certificate_chain` and `private_key` in the config.
Never share `key.pem`; clients only need `cert.pem`.

## Contribute

```sh
uv sync --all-extras
uv run pre-commit install
uv run pytest
uv run ruff check src tests
```

Dev mode (auto-reload on source changes):

```sh
uv run connector dev --app unitelabs.opentrons_flex:create_app
```

## Contact

Found a bug? Use the [issue tracker](https://github.com/AccelerationConsortium/opentrons-flex/issues).

[uv]: https://docs.astral.sh/uv/
