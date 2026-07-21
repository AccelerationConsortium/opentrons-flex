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
would otherwise hit. `HardwareProxy` also wraps every attached module exposed to
robot-server, so Heater-Shaker, Thermocycler, Temperature Module, Plate Reader, and
Stacker HTTP operations use that same lock. Cancellation-sensitive Reader work holds
ownership until its native operation settles; a cancelled Stacker move deactivates its
motors before releasing ownership and requires a complete home before reuse.
The embedded server retains its native lifespan for persistence, notifications,
and task management. After the connector injects the already-initialized shared
hardware, it runs the native post-initialization callbacks that prepare Protocol
Engine services, so `/runs`, `/protocols`, and `/commands` remain available without
creating a second hardware controller. The connector remains the sole owner of
the underlying asynchronous hardware cleanup.

The standard `opentrons-robot-server` systemd service is disabled on deployment. Our
`sila2-connector` service owns the hardware and starts the HTTP API in-process via
uvicorn on a Unix domain socket (`/run/aiohttp.sock`). nginx on the Flex proxies
external TCP port 31950 to that socket — so the HTTP API is reachable at
`http://<robot-ip>:31950` exactly as it would be with the stock `opentrons-robot-server`
service.

Motion is exposed per **mount** (`LEFT`, `RIGHT`, `GRIPPER`) in deck coordinates
(x, y, z mm), matching how the Flex hardware API models the robot.

### Verified models and environments

Automated verification uses the real Opentrons hardware simulators supplied by
Opentrons 8.8.1 on Python 3.10/3.11 and Opentrons 9.0.0 on Python 3.12+. The
connector recognizes these official model identifiers:

| Instrument or accessory | Model identifier | Verification status |
| --- | --- | --- |
| Opentrons Flex | OT-3 hardware API | OT3API simulator and guarded physical HITL suite |
| Heater-Shaker Module | `heaterShakerModuleV1` | Explicit module simulator and guarded physical HITL suite |
| Flex Stacker | `flexStackerModuleV1` | Explicit module simulator and guarded physical HITL suite |
| Absorbance Plate Reader | `absorbanceReaderV1` | Explicit module simulator and guarded physical HITL suite |
| Temperature Module GEN2 | `temperatureModuleV2` | Explicit module simulator and guarded physical HITL suite |
| Thermocycler Module GEN2 | `thermocyclerModuleV2` | Explicit module simulator and guarded manifest-driven physical HITL workflow |

The automated suite does not claim a physical-device pass: hardware tests are
deliberately opt-in and must be recorded against the serial number and firmware
of the robot/module under test before that combination is described as validated.

**SiLA features:**

| Feature | Commands / properties |
|---------|------------------------|
| `MotionController` | `Home`, `HomeMount`, `MoveTo`, `MoveRelative`, `GetPosition`, `Aspirate`, `Dispense`, `BlowOut`, `PrepareForAspirate`, `EmergencyStop`, `Pause`, `Resume`, `SetLights`; `Lights`, `IsSimulating`, `MachineStatus` |
| `LiquidHandlingController` | `Mix`, `TouchTip`, `ProbeLiquidLevel`, `AspirateWhileTracking`, `DispenseWhileTracking`, atomic `Transfer`, and `TransferWithVerifiedLiquidClass` |
| `LabwareMovementController` | `MoveLabware` / `MoveLid` execute locally allowlisted plans with server-owned occupancy, module-state, waypoint, pickup-width, and cancellation checks |
| `PipetteController` | `GetAttachedPipettes`; full, single-nozzle, and rectangular partial-tip layouts for 1/8/96-channel heads |
| `TipController` | Atomic move + `PickUpTip` / `DropTip`, `GetTipPresence`; sensor verification and defined recovery errors (use `MotionController.EmergencyStop` for a global halt) |
| `GripperController` | `Grip`, `Ungrip`, `HomeJaw`; `Status`, `JawWidth` |
| `CalibrationController` | `CalibratePipette`, `CalibrateGripperJaw`, `CalibrateDeck` (automatic probe-based routines) |
| `HeaterShakerController` | Observable heat, shake, stop, and latch commands with constrained °C/rpm inputs |
| `AbsorbanceReaderController` | `InitializeSingle`, `InitializeSingleWithReference`, `InitializeMultiple`, `ReadPlate`, `Deactivate`; observable `Status`, static `DeviceInfo` |
| `FlexStackerController` | Routine `RetrieveLabware` and `StoreLabware`; observable `Status`, static `DeviceInfo` |
| `FlexStackerMaintenanceController` | `HomeAll`, axis/latch, LED, and motor-stop service commands; observable `Status` and `LimitSwitchStatus`, static `DeviceInfo` |
| `TemperatureController` | `SetTemperature`, atomic `SetTemperatureAndWait`, `Deactivate`; observable `Status`, static `DeviceInfo` |
| `ThermocyclerController` | Observable lid control, lid/block temperatures, typed multi-step profiles, deactivation, status, and device identity |

The Heater-Shaker controller exposes observable temperature, shaking, and latch
operations with intermediate execution updates and defined module errors:
`SetTemperature`, `WaitForTemperature`, `DeactivateHeater`, `SetSpeed`,
`StopShaking`, `OpenLatch`, `CloseLatch`, `GetTemperature`, `GetSpeed`,
`GetLatchStatus`, `GetStatus`, and `GetDeviceInfo`.
Temperature inputs carry a degrees Celsius unit constraint and a 0–95 °C range;
active shaking carries a revolutions-per-minute unit constraint and a 200–3000
rpm range. The physical unit stays in the FDL constraint rather than the endpoint
identifier. Use `StopShaking` instead of sending an implicit zero-speed sentinel.

The Absorbance Plate Reader workflow follows the official physical sequence:
use an allowlisted `LabwareMovementController` plan and the Flex Gripper to place
the lid on an empty reader, initialize one or more wavelengths, move the lid and
plate, replace the lid, and call `ReadPlate`. Lid and plate movement remain in the
labware-movement concern; the reader feature validates its live lid/plate state and
returns measurements grouped by wavelength with explicit A1–H12 well identifiers.

The Flex Stacker workflow uses `RetrieveLabware` to move the bottom item onto the
shuttle and `StoreLabware` to return the shuttle item to the stack. Both commands
require the exact assembled labware height, expose millimetre units and physical
range constraints, and can enforce the Stacker's hopper and shuttle sensors. A
cancelled or failed motion stops the motors and requires
`FlexStackerMaintenanceController.HomeAll` before another retrieve/store command,
preventing an uncertain shuttle position from being reused. The recovery gate is
shared with the embedded robot-server and also fails closed from polled axis state.

The Temperature Module GEN2 controller supports both heating and cooling across
the public 4-95 °C range. `SetTemperature` starts control and returns immediately;
`SetTemperatureAndWait` atomically sets and waits for the same target. Cancelling
the waiting command deactivates temperature control before returning cancellation.
The autonomous thermal wait does not monopolize the connector-wide hardware lock,
so unrelated robot-server operations can continue; if the parallel HTTP path changes
the module target, the SiLA command stops with a defined error instead of reporting
false success. Status and identity are exposed as properties with structured state.

The Magnetic Block is passive and has no powered state or command surface, so the
connector intentionally does not register a Magnetic Block feature. Move plates on
and off it with server-provisioned `LabwareMovementController` plans and the existing
Flex Gripper, just like movement between other deck locations.

For commissioning the complete robot plus Heater-Shaker, Thermocycler, Temperature
Module, Absorbance Plate Reader, Flex Stacker, and passive Magnetic Block, use the
[manifest-driven system acceptance workflow](docs/flex_system_acceptance.md). It
provides both a publishable Unitelabs workflow and a guarded direct SiLA gRPC HITL
runner with JUnit identity evidence.

### Module feature v2 migration

The connector is still pre-1.0; version 0.9.0 adds the guarded, manifest-driven
system acceptance path and retains the standards-compliant module definitions.
Regenerate SiLA clients and update these endpoint mappings when upgrading:

| Previous v1 endpoint | v2 endpoint |
| --- | --- |
| `ConfigureMeasurement` | `InitializeSingle`, `InitializeSingleWithReference`, or `InitializeMultiple` |
| `StartMeasure` | `ReadPlate` (typed wavelength and A1-H12 results) |
| `GetStatus`, `GetDeviceInfo` | `Status`, `DeviceInfo` properties |
| `DispenseLabware` | `RetrieveLabware` |
| Boolean maintenance results | Structured `FlexStackerStatus` results or Defined Execution Errors |
| Temperature `SetTemperature(TemperatureCelsius)` | `SetTemperature(Temperature)` |
| Temperature `WaitForTemperature` | Atomic `SetTemperatureAndWait` |
| Temperature `GetTemperature`, `GetDeviceInfo` | `Status`, `DeviceInfo` properties |

Advanced liquid commands retain the low-level primitives for compatibility but
run multi-step operations atomically under the same hardware lock. Verified
liquid-class transfers read the version-2 definitions shipped with the installed
Opentrons package and match them against the attached pipette plus the full tip-rack
URI. Physical pipette installation remains an operator action; the connector
detects the installed head, configures its active nozzles, and verifies tip state.

Labware movement never accepts grip coordinates, sensor tolerances, or deck
occupancy from a remote SiLA call. Set `labware_movement_config` to a local JSON
file containing `plans`, a durable `state_file`, and `initial_occupancy`; clients can then
select only a provisioned `plan_identifier`. The connector updates its deck state
after each completed move, always keeps pickup geometry verification enabled, and
serializes module latch/lid operations with gripper motion through the shared
hardware lock. Leave the setting `null` to expose discovery and defined errors
without authorizing physical labware movement.

The local file has this shape; coordinates and grip values must come from the
installed labware definitions and the calibrated deck model:

```json
{
  "state_file": "/var/lib/unitelabs-opentrons-flex/labware-state.json",
  "initial_occupancy": {"D1": "plate-1"},
  "plans": [
    {
      "identifier": "plate_d1_to_d2",
      "labware_identifier": "plate-1",
      "is_lid": false,
      "source": {
        "identifier": "D1",
        "kind": "DECK_SLOT",
        "grip_point": {"x": -999.0, "y": -999.0, "z": -999.0}
      },
      "destination": {
        "identifier": "D2",
        "kind": "DECK_SLOT",
        "grip_point": {"x": -999.0, "y": -999.0, "z": -999.0}
      },
      "grip": {
        "force_newtons": 15.0,
        "expected_width": 74.0,
        "uncertainty_wider": 2.0,
        "uncertainty_narrower": 2.0
      },
      "post_drop_slide_offset": {"x": 0.0, "y": 0.0, "z": 0.0}
    }
  ]
}
```

The negative grip coordinates above are placeholders and are intentionally not a runnable
deck plan. Replace them with validated absolute grip points before enabling the
file on hardware, and provision a separate reverse plan for a round trip. The
state ledger is marked invalid before physical movement and committed only after
completion; an interrupted move or unclean shutdown requires local deck
reconciliation before replacing the ledger and restarting.

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
- `simulated_heater_shaker` — when `true`, explicitly attaches one simulated
  Heater-Shaker to the OT3 simulator. It is rejected when `use_simulator=false`.
- `simulated_flex_stacker` — explicitly attaches one simulated Flex Stacker.
- `simulated_absorbance_reader` — explicitly attaches one simulated Absorbance
  Plate Reader.
- `simulated_temperature_module` — explicitly attaches one simulated Temperature
  Module GEN2. All simulated-module settings are rejected in live-hardware mode.
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

# Full Heater-Shaker workflow over SiLA gRPC against the explicit module simulator
uv run --extra test python -m pytest tests/integration/test_grpc_heater_shaker.py -v

# Flex Stacker, Absorbance Plate Reader, and Temperature Module workflows
uv run --extra test python -m pytest \
  tests/integration/test_grpc_flex_stacker.py \
  tests/integration/test_grpc_absorbance_reader.py \
  tests/integration/test_grpc_temperature_module.py -v

# Advanced liquid, partial-nozzle, and gripper labware workflows over SiLA gRPC
uv run --extra test python -m pytest \
  tests/integration/test_grpc_advanced_flex.py tests/integration/test_grpc_pipette.py -v

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
the read-only smoke tests at it first. `--robot HOST:50051` targets the live SiLA
server; `--robot-http HOST:31950` targets the in-process robot-server.

Do **not** run the full simulator gRPC suite against a real robot. Tests that home,
move, actuate lights, pause/resume, emergency-stop, grip/ungrip, or calibrate are
simulator-only unless they live under `tests/integration/hardware/`, where each
movement is intentionally small and paired with a `MachineStatus` check.

```sh
# Read-only gRPC smoke tests against the live Flex
uv run --extra test python -m pytest tests/integration/test_grpc_motion_control.py \
    -k "is_simulating or machine_status" --robot <robot-ip>:50051 -v

# HTTP robot-server API tests against the live Flex
uv run --extra test python -m pytest tests/integration/http_api --robot-http <robot-ip>:31950
```

Tests marked `@pytest.mark.simulator_only` are skipped automatically when `--robot` is
set. Hardware motion is validated through the explicit HITL suite below, not through
the broad simulator integration suite.

For a Flex with a physical Heater-Shaker, begin with the read-only identity and
status check. It does not move the latch, heat, or shake:

```sh
uv run --extra test python -m pytest \
  tests/integration/hardware/test_hitl_heater_shaker.py \
  -k identity --robot <robot-ip>:50051 -v
```

The actuation test is gated separately. Run it only after installing a compatible
thermal adapter and secured labware, closing the robot door, and keeping the E-stop
ready. It closes the latch, shakes at the minimum 200 rpm, then stops and deactivates
the heater in cleanup:

```sh
uv run --extra test python -m pytest \
  tests/integration/hardware/test_hitl_heater_shaker.py \
    --robot <robot-ip>:50051 --heater-shaker-actuation -v
```

For a Flex Stacker, run identity/status first. The guarded round trip requires an
operator-loaded hopper, an empty shuttle, a closed hopper door, and the exact
assembled labware height:

```sh
uv run --extra test python -m pytest \
  tests/integration/hardware/test_hitl_flex_stacker.py \
  -k identity --robot <robot-ip>:50051 -v

uv run --extra test python -m pytest \
  tests/integration/hardware/test_hitl_flex_stacker.py \
  --robot <robot-ip>:50051 --stacker-actuation \
  --stacker-labware-height 14.4 -v
```

For an Absorbance Plate Reader, the default hardware check is read-only. The
separately gated initialization requires an empty reader and a lid placed by the
Flex Gripper; never move the reader lid by hand:

```sh
uv run --extra test python -m pytest \
  tests/integration/hardware/test_hitl_absorbance_reader.py \
  -k identity --robot <robot-ip>:50051 -v

uv run --extra test python -m pytest \
  tests/integration/hardware/test_hitl_absorbance_reader.py \
  --robot <robot-ip>:50051 --plate-reader-actuation \
  --plate-reader-wavelength 450 -v
```

After initialization, use the Flex Gripper through an allowlisted movement plan
to remove the lid, place a compatible plate, and replace the lid. Then run the
separately gated physical measurement without restarting the connector:

```sh
uv run --extra test python -m pytest \
  tests/integration/hardware/test_hitl_absorbance_reader.py \
  -k guarded_plate_measurement --robot <robot-ip>:50051 \
  --plate-reader-measurement -v
```

For a Temperature Module GEN2, begin with the read-only identity/status check:

```sh
uv run --extra test python -m pytest \
  tests/integration/hardware/test_hitl_temperature_module.py \
  -k identity --robot <robot-ip>:50051 -v
```

Heating and cooling are separately safety-gated and always end by deactivating
the module. Use compatible labware and provide an explicit target at least 1 °C
away from the current reading. To verify both directions, run once with a higher
target and once with a lower target, keeping both within 4-95 °C:

```sh
uv run --extra test python -m pytest \
  tests/integration/hardware/test_hitl_temperature_module.py \
  --robot <robot-ip>:50051 --temperature-module-actuation \
  --temperature-module-target 37 -v
```

Labware movement has a separate physical safety gate. First provision validated
outbound and return plans in the connector's local `labware_movement_config`.
The test selects only those allowlisted plans, moves the prepared labware out and
back, and checks machine state:

```sh
uv run --extra test python -m pytest \
  tests/integration/hardware/test_hitl_labware_movement.py \
  --robot <robot-ip>:50051 --gripper-labware-actuation \
  --gripper-outbound-plan PLAN_ID \
  --gripper-return-plan RETURN_PLAN_ID -v
```

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

To interact with the running connector, we recommend using the [SiLA Browser](https://gitlab.com/unitelabs/sila2/sila-browser) against `<robot-ip>:50051`. The Opentrons App and REST clients can use the standard HTTP API shape at `http://<robot-ip>:31950`. When durable labware plans are enabled, raw HTTP gripper/extension and gripper-axis actuation is intentionally rejected so it cannot bypass `LabwareMovementController`; use the allowlisted SiLA plans or disable the local plan registry for a deliberate maintenance session.

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
