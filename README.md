# Opentrons Flex

A SiLA2 connector for the Opentrons Flex liquid-handling robot that also replaces the
standard Opentrons robot-server HTTP API. Both servers share a single hardware API
instance so they cannot conflict over the CAN bus.

## Architecture

This project runs two servers in the same process when deployed to a real Flex:

| Server | Protocol | Port | Purpose |
|--------|----------|------|---------|
| SiLA2 connector | gRPC | 50051 | Lab automation clients (SiLA Browser, UniteLabs platform) |
| Opentrons robot-server | HTTP REST | 31950 | Opentrons App, any REST client |

Both servers are backed by one shared `HardwareControlAPI` (the Flex `OT3API`) wrapped
in `HardwareProxy` — an `asyncio.Lock` around every hardware call. This serialises the
SiLA gRPC server and the in-process HTTP robot-server so their CAN commands cannot
interleave, and avoids the "hardware already initialised" error two separate processes
would otherwise hit.

The standard `opentrons-robot-server` systemd service is disabled on deployment. Our
`sila2-connector` service owns the hardware and starts the HTTP API in-process via
uvicorn on a Unix domain socket (`/run/aiohttp.sock`). nginx on the Flex proxies
external TCP port 31950 to that socket — so the HTTP API is reachable at
`http://<robot-ip>:31950` exactly as it would be with the stock `opentrons-robot-server`
service.

Motion is exposed per **mount** (`LEFT`, `RIGHT`, `GRIPPER`) in deck coordinates
(x, y, z mm), matching how the Flex hardware API models the robot.

**SiLA features:**

| Feature | Commands / properties |
|---------|------------------------|
| `MotionControlFeature` | `Home`, `HomeMount`, `MoveTo`, `MoveRelative`, `GetPosition`, `Aspirate`, `Dispense`, `BlowOut`, `PrepareForAspirate`, `EmergencyStop`, `Pause`, `Resume`, `SetLights`; `Lights`, `IsSimulating`, `MachineStatus` |
| `PipetteFeature` | `GetAttachedPipettes` (Flex pipette models, per mount) |
| `GripperFeature` | `Grip`, `Ungrip`, `HomeJaw`; `Status` |
| `CalibrationFeature` | `CalibratePipette`, `CalibrateGripperJaw`, `CalibrateDeck` (automatic probe-based routines) |
| Module features | `HeaterShaker`, `Thermocycler`, `Temperature`, `AbsorbanceReader`, `FlexStacker` (registered when attached) |

**Key source files:**

- `src/unitelabs/opentrons_flex/__init__.py` — `create_app()` entry point and
  `_create_app_with_robot_server()` (the in-process HTTP server startup)
- `src/unitelabs/opentrons_flex/io/flex_motion.py` — `FlexMotionController` (hardware-API wrapper)
- `src/unitelabs/opentrons_flex/io/hardware_proxy.py` — `HardwareProxy` shared-lock wrapper
- `tests/test_create_app_with_robot_server.py` — unit tests for the startup wiring (mocked)
- `tests/integration/` — end-to-end gRPC + HTTP API tests against the simulator or a live robot

## Getting Started

For a general introduction to connector development with the UniteLabs CDK, see the [connector development documentation](https://docs.unitelabs.io/connector-development).

### Prerequisites

Ensure that [uv](https://docs.astral.sh/uv/) is installed on your system. You can install it with:

```sh
pipx install uv
```

### Installation

#### Create a Virtual Environment

It is highly recommended to use a virtual environment to manage the dependencies for your connector project. This keeps the dependencies for different connectors isolated from each other. Use the following command to create a virtual environment:

```sh
uv venv
```

Activate the virtual environment:

- On **Windows**:

  ```sh
  .\venv\Scripts\activate.bat
  ```

- On **macOS**/**Linux**:

  ```sh
  source .venv/bin/activate
  ```

If you are on a Windows machine, you may additionally wish to set the `UNITELABS_CDK_APP` environment variable to the connector's entry point:

```sh
set UNITELABS_CDK_APP=unitelabs.opentrons_flex:create_app
```

Setting this environment variable will allow you to run various CLI commands without providing `--app unitelabs.opentrons_flex:create_app` every time.

#### Install Required Dependencies

Install the connector and its dependencies into your active virtual environment:

```sh
uv sync --all-extras
```

#### Configure the Connector

To get information about the configuration values for the connector simply run:

- On **Windows**:

  ```sh
  config show --app unitelabs.opentrons_flex:create_app
  ```

- On **macOS**/**Linux**:

  ```sh
  config show
  ```

To create a configuration file for the connector we run:

- On **Windows**:

  ```sh
  config create --app unitelabs.opentrons_flex:create_app
  ```

- On **macOS**/**Linux**:

  ```sh
  config create
  ```

Used as such this command will create a `config.json` in the current working directory. If you prefer yaml, or would like to save the file to a different location, add the `--path` argument. A ready-made template is provided in `config/flex_config.json`.

Key values:

- `use_simulator` — `true` runs the OT3 hardware simulator (no robot); `false` drives real Flex hardware.
- `with_robot_server` — `true` additionally starts the in-process opentrons HTTP robot-server.

Note: The `cloud_server_endpoint` values are only necessary if you want to use the connector with the UniteLabs platform.

#### Verify the Installation

Start the connector in simulator mode using the CLI tool included in the dependencies:

```sh
connector start --app unitelabs.opentrons_flex:create_app -vvv
```

If you created your configuration file at a non-default location, specify it with `--config-path` (or `-cfg`):

```sh
connector start --app unitelabs.opentrons_flex:create_app -cfg <path to config> -vvv
```

## Testing

The test suite runs **fully offline** against the real opentrons OT3 *simulator* — no
robot required.

```sh
# Everything except the live-robot HTTP tests (which skip without a target).
# --extra test makes uv install pytest/httpx instead of accidentally using a global pytest.
uv run --extra test python -m pytest

# Just the unit + simulation tests
uv run --extra test python -m pytest tests/io tests/features

# gRPC integration tests over the wire (in-process SiLA server + OT3 simulator)
uv run --extra test python -m pytest tests/integration -k grpc

# Full local smoketest: SiLA gRPC + in-process robot-server HTTP API, both backed
# by the OT3 simulator. This is the CI/CD end-to-end path before real hardware.
uv run --extra test python -m pytest tests/integration --with-http-server -v
```

The suite covers the controllers and SiLA features driven against the OT3 simulator,
SiLA feature-definition generation, and real gRPC calls through the full chain
`gRPC → SiLA server → feature → hardware API`, including defined-execution errors
propagating over the wire.

`config/smoketest_config.json` is the local no-hardware config. It keeps
`use_simulator=true`, `with_robot_server=true`, and cloud/discovery disabled. The
deployment config in `config/flex_config.json` remains explicitly live-hardware
(`use_simulator=false`) so simulator and robot runs do not blur together.

### Testing against a real Flex

Once the connector is deployed and running on a Flex (see **Deploying** below), point
the same integration tests at it. `--robot HOST:50051` runs the gRPC tests against the
live SiLA server; `--robot-http HOST:31950` runs the HTTP API tests against the
in-process robot-server.

```sh
# gRPC feature tests against the live Flex (simulator-only cases auto-skip)
uv run --extra test python -m pytest tests/integration -k grpc --robot <robot-ip>:50051

# HTTP robot-server API tests against the live Flex
uv run --extra test python -m pytest tests/integration/http_api --robot-http <robot-ip>:31950

# Both, in one run
uv run --extra test python -m pytest tests/integration --robot <robot-ip>:50051
```

Tests marked `@pytest.mark.simulator_only` are skipped automatically when `--robot` is
set. Position assertions compare against a freshly captured `homed_position` fixture
rather than hardcoded coordinates, so they hold on both the simulator and real firmware.

Every integration run prints its **mode/target/device** header and records
`mode` / `sila_target` / `http_target` / `device_id` to each test's junit
properties, so a result is never ambiguous about whether it hit the simulator or
a real robot (a `hardware_only` test that somehow runs in smoketest mode fails
loudly rather than passing against the simulator).

### Hardware-in-the-loop (HITL) motion tests

`tests/integration/hardware/` holds Stage-4 HITL tests that run **only** against a
real Flex (`--robot`/`--robot-http`) and are skipped otherwise. They issue only
small, reversible moves (home, a 5 mm Z jog up-and-back, lights) and — crucially —
query `MachineStatus` **after every movement** to assert the robot did not
silently enter a hardware error state (E-stop engaged, etc.). A move that
returns is not assumed to have succeeded; the connector's post-move guard raises
`MachineErrorStateError` on a hidden fault, and the HITL client re-checks from
the outside.

```sh
uv run --extra test python -m pytest tests/integration/hardware --robot <robot-ip>:50051 -v
```

### HTTP ↔ SiLA parity matrix

`docs/parity_matrix.md` (source: `docs/parity_matrix.json`) records which
Opentrons HTTP API functions have a first-class SiLA2 equivalent
(supported / unclear / unsupported). `tests/integration/http_api/test_parity_matrix.py`
validates the matrix and, under `--with-http-server`, cross-checks its HTTP paths
against the live robot-server OpenAPI so the matrix cannot silently drift.

## Deploying to the Flex

The Flex host is aarch64 (ARM64) with a modern glibc, so standard PyPI
`manylinux_2_17_aarch64` wheels for C-extension packages (grpcio, numpy, …) install
directly — no from-source build is required.

### Building `dist_arm/`

Build the aarch64 wheel bundle with the provided Dockerfile:

```sh
docker buildx build --platform linux/arm64 -f Dockerfile.build \
    --target export --output type=local,dest=dist_arm .
```

Or trigger the **Build Flex aarch64 Wheels** GitHub Actions workflow
(`.github/workflows/build-flex-arm-wheels.yml`) and download the `flex-arm-wheels`
artifact into `dist_arm/`.

### Installing on the Flex

Copy the wheels and create the venv on the robot:

```sh
./deploy.sh <robot-ip>
```

Then install the connector as a persistent systemd service (this disables the Opentrons robot server so the connector owns the hardware):

```sh
./scripts/install_connector_service.sh <robot-ip>
```

Switch between the SiLA connector and the stock opentrons robot-server at any time (the choice persists across reboot):

```sh
./scripts/switch_mode.sh <robot-ip> connector
./scripts/switch_mode.sh <robot-ip> opentrons
```

To deploy Python source changes to a robot that already has the service installed:

```sh
./scripts/deploy_python_changes.sh <robot-ip>
```

Logs:

```sh
ssh root@<robot-ip> 'journalctl -u sila2-connector -f'
```

### Why `--system-site-packages`

The `opentrons` package (and the `robot_server` HTTP server) are pre-installed as
system packages by the Opentrons robot software. They are intentionally excluded from
`dist_arm/` to avoid version conflicts; the venv inherits them via `--system-site-packages`.

`robot_server` is not distributed in the public `opentrons` PyPI wheel. Its source lives
in the Opentrons monorepo under
[`robot-server/robot_server`](https://github.com/Opentrons/opentrons/tree/edge/robot-server/robot_server).
Before the first hardware run, verify the Flex image exposes the same package:

```sh
ssh root@<robot-ip> \
  '/var/sila2_flex/bin/python -c "import robot_server, robot_server.hardware, robot_server.app; print(robot_server.__file__)"'
```

Then start the connector and run the live HTTP integration tests:

```sh
uv run --extra test python -m pytest tests/integration/http_api --robot-http <robot-ip>:31950
```

## Usage

To interact with the running connector, we recommend using the [SiLA Browser](https://gitlab.com/unitelabs/sila2/sila-browser) against `<robot-ip>:50051`. The Opentrons App and any REST client can use the HTTP API at `http://<robot-ip>:31950` unchanged.

### Encryption

To secure communication between the connector and its clients, you can enable TLS encryption. Start by installing the optional `cryptography` package for generating TLS certificates:

```sh
uv pip install cryptography
```

To generate a pair of public and private keys, use:

```sh
certificate generate
```

Without any arguments this command uses the default config location to get the connector's UUID and host name. It will prompt you as to whether or not you want to update your config file to enable TLS. This prompt can be suppressed with `--non-interactive`/`-y`, or with `--embed`/`-e` to write the file contents into the config file directly.

If you choose not to auto-update the config, set the following values yourself under `sila_server`:

- `certificate_chain` — the path to the `cert.pem` file
- `private_key` — the path to the `key.pem` file
- `tls` — a boolean that toggles TLS encryption for the SiLA server

> **Important:** Never share the `key.pem` file with anyone. Only the `cert.pem` is required for clients to connect to encrypted servers.

## Contribute

We welcome contributions to improve this connector. We use [uv][] for Python packaging (requires `uv>=0.6.8`).

Clone the repository and set up the development environment:

```sh
git clone https://github.com/AccelerationConsortium/opentrons-flex.git
cd opentrons-flex
uv sync --all-extras
uv run connector start -vvv
```

Install `pre-commit` hooks to ensure code quality:

```sh
uv run pre-commit install
```

Run the test suite and linters:

```sh
uv run pytest
uv run ruff check src tests
```

To improve the development experience, run the connector in developer mode, which automatically reloads whenever source changes are saved:

```sh
uv run connector dev --app unitelabs.opentrons_flex:create_app
```

## Contact

If you found a bug, please use the [issue tracker][issue-tracker].

[issue-tracker]: https://github.com/AccelerationConsortium/opentrons-flex/issues
[uv]: https://docs.astral.sh/uv/
