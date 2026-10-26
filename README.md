# Opentrons Flex

A SiLA2 connector for the Opentrons Flex liquid-handling robot. It supports two
exclusive connector profiles: the full Protocol Engine profile also replaces the
standard Opentrons robot-server HTTP API, while the unit-operation profile gives
SiLA sole ownership of the hardware for guarded atomic commissioning.

## Start here (5 minutes)

You need a computer on the same network as the Flex. Nothing else has to be
installed first: the bootstrap script installs [uv](https://docs.astral.sh/uv/),
Python 3.12, the connector, and the `flexctl` tool, then starts a guided setup.
中文版快速上手：[docs/quickstart.zh.md](docs/quickstart.zh.md).

```sh
git clone https://github.com/AccelerationConsortium/opentrons-flex.git
cd opentrons-flex
./bootstrap.sh                 # macOS / Linux
# Windows PowerShell:  powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1
```

The guided setup asks one question: **try the simulator, or set up a real Flex?**

- **Simulator** starts a complete virtual Flex on this computer (SiLA on port
  50051). In a second terminal, `uv run flexctl verify 127.0.0.1 --allow-simulator`
  checks it. Add `--with-http` to `flexctl sim` for the Opentrons HTTP API too.
- **Real Flex** finds the robot on the network, installs an SSH key (over a direct
  Ethernet cable, or with a USB drive when you only have Wi-Fi), downloads the
  matching robot software, deploys and starts the connector, and runs a smoke test.

Then test the hardware with the robot in view (each step tells you what to do and
what it expects; press `s` to skip a step):

```sh
uv run flexctl verify <robot-ip> --level motion   # ~2 min: homing and emergency stop
uv run flexctl verify <robot-ip> --level all      # + tip reconciliation, restart, module unplug/plug
```

A JSON report (`flex-verify-<robot>-<time>.json`) is written next to you.
See [docs/hardware_verification.md](docs/hardware_verification.md) for what each
step checks.

| Task | Command |
|---|---|
| Find robots | `uv run flexctl discover` |
| SSH key only | `uv run flexctl ssh-setup <robot-ip> [--usb-drive /Volumes/USB]` |
| Robot software bundle | `uv run flexctl build` (GitHub Actions artifact, or Docker) |
| Deploy / update | `uv run flexctl up <robot-ip> --mode pe` (or `--mode unit`) |
| What is running | `uv run flexctl status <robot-ip>` |
| Logs | `uv run flexctl logs <robot-ip> --follow` |
| Back to stock Opentrons | `uv run flexctl mode <robot-ip> opentrons` |
| Previous release | `uv run flexctl rollback <robot-ip>` |
| Tidy the checkout | `uv run flexctl clean --yes` |

## Architecture

The deployed service has two selectable profiles:

| Profile | Interfaces | Purpose |
|--------|----------|---------|
| `connector-pe` | SiLA gRPC 50051 + embedded HTTP 31950 | Protocol upload, Protocol Engine runs, and controlled checkpoints |
| `connector-unit` | SiLA gRPC 50051 only | One guarded SiLA atomic operation at a time; no upload, run, or checkpoint path |

In `connector-pe`, both servers are backed by one shared `HardwareControlAPI` (the Flex `OT3API`) wrapped
in `HardwareProxy` — an `asyncio.Lock` around every hardware call. This serialises the
SiLA gRPC server and the in-process HTTP robot-server so their CAN commands cannot
interleave, and avoids the "hardware already initialised" error two separate processes
would otherwise hit. `HardwareProxy` also wraps every attached module exposed to
robot-server, so Heater-Shaker, Thermocycler, Temperature Module, Plate Reader, and
Stacker HTTP operations use that same lock. Cancellation-sensitive Reader work holds
ownership until its native operation settles; a cancelled Stacker move deactivates its
motors before releasing ownership and requires a complete home before reuse.
Autonomous Heater-Shaker and Thermocycler temperature waits release that global
lock between short state reads, so unrelated gantry, pipette, or gripper work can
continue. A target changed through the parallel HTTP API fails the original SiLA
wait with a defined error instead of reporting success against the wrong target.
The embedded server retains its native lifespan for persistence, notifications,
and task management. After the connector injects the already-initialized shared
hardware, it runs the native post-initialization callbacks that prepare Protocol
Engine services, so `/runs`, `/protocols`, and `/commands` remain available without
creating a second hardware controller. The connector remains the sole owner of
the underlying asynchronous hardware cleanup. The run-orchestrator store is
created during startup, so SiLA commands are arbitrated correctly even before
any HTTP client has called `/runs`. If the embedded HTTP server stops while the
connector is running, `RobotStatusProvider` reports `HARDWARE_ERROR` and the
connector exits with status 75 after a short grace period so systemd restarts it.

In either connector profile, the standard `opentrons-robot-server` systemd
service is disabled and `sila2-connector` owns the hardware. In `connector-pe`,
it also starts the HTTP API in-process via uvicorn on a Unix domain socket
(`/run/aiohttp.sock`). nginx on the Flex proxies external TCP port 31950 to that
socket, so the HTTP API remains reachable at `http://<robot-ip>:31950`.
`connector-unit` does not start that HTTP server.

In `connector-unit`, `with_robot_server=false`: the connector builds one real
`OT3API`, exposes SiLA only on robot loopback (Windows clients use an
authenticated SSH tunnel), and loads a local gripper-plan registry plus durable
deck ledger. This is a reversible commissioning mode, not a second full-workflow
implementation. Switch back to `connector-pe` for the current one-hour AS-MS
Protocol Engine workflow until an external workflow owns all logical tip,
liquid, material-lineage, and recovery state.

Motion is exposed per **mount** (`LEFT`, `RIGHT`, `GRIPPER`) in deck coordinates
(x, y, z mm), matching how the Flex hardware API models the robot.

### Verified models and environments

Automated verification and the ARM deployment artifact use the real Opentrons
9.0.0 hardware simulators on Python 3.12. Controlled Protocol Engine
mutation is intentionally unavailable on unvalidated Opentrons/Python matrices.
The connector recognizes these official model identifiers:

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
| `HeaterShakerController` | Observable heat, shake, stop, and latch commands with constrained °C/rpm inputs; observable `Status`, static `DeviceInfo` |
| `AbsorbanceReaderController` | `InitializeSingle`, `InitializeSingleWithReference`, `InitializeMultiple`, `ReadPlate`, `Deactivate`; observable `Status`, static `DeviceInfo` |
| `FlexStackerController` | Routine `RetrieveLabware` and `StoreLabware`; observable `Status`, static `DeviceInfo` |
| `FlexStackerMaintenanceController` | `HomeAll`, axis/latch, LED, and motor-stop service commands; observable `Status` and `LimitSwitchStatus`, static `DeviceInfo` |
| `TemperatureController` | `SetTemperature`, atomic `SetTemperatureAndWait`, `Deactivate`; observable `Status`, static `DeviceInfo` |
| `ThermocyclerController` | Observable lid control, lid/block temperatures, typed multi-step profiles and deactivation; observable `Status`, static `DeviceInfo` |

The Heater-Shaker controller exposes observable temperature, shaking, and latch
operations with intermediate execution updates and defined module errors:
`SetTemperature`, `WaitForTemperature`, `DeactivateHeater`, `SetSpeed`,
`StopShaking`, `OpenLatch`, `CloseLatch`, `GetTemperature`, `GetSpeed`,
`GetLatchStatus`, `GetStatus`, and `GetDeviceInfo`.
The read commands remain for compatibility; new clients should subscribe to
`Status` and read the static `DeviceInfo` property. Temperature inputs carry a
degrees Celsius unit constraint and the Opentrons 9.0 API's 0–95 °C range;
targets below ambient may be physically unreachable because the module does not cool.
active shaking carries a revolutions-per-minute unit constraint and a 200–3000
rpm range. The physical unit stays in the FDL constraint rather than the endpoint
identifier. Use `StopShaking` instead of sending an implicit zero-speed sentinel.
Cancelling `WaitForTemperature` stops the wait but intentionally leaves the active
target enabled; call `DeactivateHeater` when heating must stop.

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

The Thermocycler validates finite temperatures, hold times, block maximum volume,
profile size, and direction-dependent ramp rates before actuation. A zero block
volume selects the Opentrons default of 25 µL; non-zero values are the greatest
volume in any individual well. Long waits and profiles publish current/target and
step progress without monopolizing the global hardware lock. Cancelling a profile
deactivates both thermal controllers, while cancelling a standalone wait leaves
its target active and reports that explicitly.

The current Feature Definitions represent one physical instance of each accessory
type. If two supported modules of the same type are attached, startup fails with
their serial numbers instead of silently replacing one Feature registration.
Disconnect the extra module until a serial-routed, multi-instance Feature contract
is deployed.

The Magnetic Block is passive and has no powered state or command surface, so the
connector intentionally does not register a Magnetic Block feature. Move plates on
and off it with server-provisioned `LabwareMovementController` plans and the existing
Flex Gripper, just like movement between other deck locations.

For commissioning the complete robot plus Heater-Shaker, Thermocycler, Temperature
Module, Absorbance Plate Reader, Flex Stacker, and passive Magnetic Block, use the
[manifest-driven system acceptance workflow](docs/flex_system_acceptance.md). It
provides both a publishable Unitelabs workflow and a guarded direct SiLA gRPC HITL
runner with JUnit identity evidence. The operator-side preflight and evidence
validation are pure Python and use `pathlib`, so the readiness, commissioning,
and routine workflow operator path runs natively on the lab Windows computer;
macOS remains a compatible backup environment. The `deploy.sh` and service
management scripts are POSIX tooling for the Flex's Linux environment: invoke
them from CI, macOS, WSL, or Git Bash rather than native PowerShell.
The connector/runtime and local Unitelabs workflow both target Python 3.12. The
workflow remains isolated and depends only on the shared acceptance contract;
`uv run --directory` keeps its environment separate from robot-only packages.

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

### Quick start with `flexctl`

`flexctl` is installed with the connector (`uv sync --python 3.12 --all-extras`)
and wraps configuration, deployment, and day-to-day operation in single commands.
Every remote action calls the reviewed scripts in this repository, so the
fail-closed deployment behavior is unchanged; add `--dry-run` to print the
commands without running them.

```sh
# Local simulator (add --with-http for the embedded Opentrons HTTP API)
uv run flexctl sim

# Mirror the modules attached to a real robot in a simulator config
uv run flexctl config init --profile simulator --detect <robot-ip> --output config.json

# Check the workstation and wheel bundle, then deploy and start on a Flex
uv run flexctl doctor
uv run flexctl up <robot-ip> --mode pe --operator <your-name>   # or --mode unit
uv run flexctl status <robot-ip>
uv run flexctl logs <robot-ip> --follow
uv run flexctl mode <robot-ip> opentrons                          # hand back to stock
uv run flexctl rollback <robot-ip>
```

`flexctl up` refuses to touch the robot when the wheel bundle is missing, has no
`runtime-manifest.json`, or was built for a different connector version. It
creates the controlled-mutation credential on first use (sent over SSH stdin and
printed once), switches a running connector to stock mode before deployment,
then runs `deploy.sh`, `install_connector_service.sh`, the requested mode switch,
and `scripts/preflight_flex.py`.

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

  ```powershell
  .\.venv\Scripts\Activate.ps1
  ```

- On **macOS**/**Linux**:

  ```sh
  source .venv/bin/activate
  ```

If you are on a Windows machine, you may additionally wish to set the `UNITELABS_CDK_APP` environment variable to the connector's entry point:

```powershell
$env:UNITELABS_CDK_APP = "unitelabs.opentrons_flex:create_app"
```

Setting this environment variable will allow you to run various CLI commands without providing `--app unitelabs.opentrons_flex:create_app` every time.

#### Install Required Dependencies

Install the connector and its dependencies into your active virtual environment:

```sh
uv sync --python 3.12 --all-extras
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

`flexctl config init --profile simulator|simulator-full|connector-pe|connector-unit`
writes a complete file for each supported deployment profile.

Key values:

- `use_simulator` — `true` runs the OT3 hardware simulator (no robot); `false` drives real Flex hardware.
  The connector logs a warning at startup whenever it runs the simulator.
- `simulated_modules` — accessories attached to the simulator: any of
  `heater_shaker`, `flex_stacker`, `absorbance_reader`, `temperature_module`,
  `thermocycler`, `gripper`. Rejected when `use_simulator=false`. The older
  `simulated_<name>: true` flags are still accepted.
- `with_robot_server` — `true` additionally starts the in-process opentrons HTTP robot-server.
- `recovery_state_path` — durable file for recovery gates (tip quarantine, jaw
  home). With it, restarting the connector cannot silently clear a gate.
- `module_hot_plug` — `auto` (default), `restart`, or `report`; see
  "Modules plugged or unplugged while running".
- `robot_server_startup_timeout_s` — startup fails if the embedded robot-server
  is not serving within this time (default 120 s).
- `sila_server.version` — omit it; the connector reports its installed package
  version. A stale explicit value is rejected by the deployment preflight.

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

## Status, recovery, and errors

### `RobotStatusProvider`

Subscribe to the observable `Status` property to see, in one stream, whether the
Flex can accept a command. It is read from cached state and never waits for a
running operation.

| `State` | Meaning |
|---|---|
| `READY` | Commands are accepted. |
| `BUSY` | Another operation holds the hardware; queued commands wait (up to `lock_timeout_s`). |
| `PROTOCOL_RUN_ACTIVE` | A Protocol Engine run owns the Flex; direct SiLA actuation is refused. |
| `RECOVERY_REQUIRED` | Follow `RecoveryGuidance` (re-home, `ReconcileTipState`, `HomeJaw`, or deck reconciliation). |
| `HARDWARE_ERROR` | E-stop engaged, a registered module disconnected, or the embedded HTTP server stopped. |
| `INITIALIZING` | The embedded robot-server is still starting. |

The structure also reports E-stop and door state, each recovery gate, Protocol
Engine run ownership, deck-ledger validity, per-module connection state, modules
attached after startup (restart to use them), and the embedded web-server state.

### Modules plugged or unplugged while running

- **Unplug and replug the same module:** nothing to do. The feature reports
  `ModuleNotRespondingError` while the module is absent and resumes automatically,
  following the module by serial number. A *different* unit of the same type
  (another serial) is never adopted silently, since it may hold other samples or
  temperatures: the error names both serials; restart the connector to switch.
  `Status.Modules[].Connected` shows the current state. A reconnected Flex Stacker
  must be fully homed before it moves labware again.
- **Plug in a module type that had no feature at startup:** with
  `module_hot_plug: auto` (default on a real Flex) the connector waits until the
  module has been present for 5 s and the robot is fully idle (status `READY`, no
  active Protocol Engine run, no SiLA command in flight, no module temperature
  wait or thermocycler profile, no recovery gate pending), sets
  `Status.RestartPending`, and restarts itself (exit status 75, restarted by
  systemd) so the new feature is registered. In the simulator, or with
  `module_hot_plug: report`, it is only listed in `Status.UnregisteredModules`.
  Two modules of the same type never trigger a restart.

### Recovery workflow

1. A halt, emergency stop, or interrupted action sets recovery gates. Every
   actuation then fails with `RecoveryRequiredError`, whose message lists the
   ordered actions (also in `Status.RecoveryGuidance`).
2. `MotionController.Home` clears the re-home gate.
3. `TipController.ReconcileTipState(Mount, TipLength)` reads the tip sensor,
   aligns the software tip model with it, and lifts that mount's quarantine
   without moving the robot. `TipLength` is used only if the sensor finds a tip
   the software did not know about.
4. `GripperController.HomeJaw` clears the jaw gate.
5. An invalid deck ledger (after an interrupted gripper move or an unclean
   shutdown) is reconciled only locally by an operator, never by a SiLA client:

   ```sh
   uv run flexctl mode <robot-ip> opentrons
   uv run flexctl deck <robot-ip> show
   uv run flexctl deck <robot-ip> reconcile --operator <name> --reason "<why>" \
       --set A1=asms-plate --set B1=elution-plate --confirm      # or --empty / --initial
   ```

   Every reconciliation is appended to `<ledger>.reconciliations.jsonl`.

### System-wide defined errors

In addition to their own errors, hardware commands declare:

| Error | When |
|---|---|
| `HardwareBusyError` | The shared hardware stayed busy longer than `lock_timeout_s`. |
| `OperationHaltedError` | Another client's emergency stop or a halt interrupted this command. Client-initiated cancellation still ends as a normal cancellation. |
| `RecoveryRequiredError` | A recovery gate is set; the message lists the required actions. |
| `HardwareError` | Opentrons reported an error without a more specific mapping; the Opentrons error code, name, and detail are preserved. |

Every observable command ends with exactly one terminal intermediate response:
`COMPLETED`, `CANCELLED`, or `FAILED` (with the error type and message).

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

# AS-MS wash/elution protocol: exact bundled-labware gate
uv run python scripts/validate_asms_protocol.py

# Optional logic-only AS-MS simulation with explicit non-production substitutes
uv run python scripts/validate_asms_protocol.py --shadow

# Full two-column AS-MS upload, analysis, and execution through embedded robot-server
uv run --extra test python -m pytest \
  tests/integration/http_api/test_asms_protocol_upload.py --with-http-server -v
```

The suite covers the controllers and SiLA features driven against the OT3 simulator,
SiLA feature-definition generation, and real gRPC calls through the full chain
`gRPC → SiLA server → feature → hardware API`, including defined-execution errors
propagating over the wire.

CI also creates an isolated Windows Python 3.12 workflow environment that does
not install the connector runtime. It validates the native PowerShell
entrypoint and runs the `flex_workflow_offline` import, phase-wiring,
commissioning-digest, and fail-closed shutdown tests with offline SDK/Prefect
stubs. Deployment-script tests execute the actual remote shell bodies under a
temporary filesystem root with fake services, covering preflight refusal,
bounded health failure with stock-service recovery, and atomic rollback
activation.

`config/smoketest_config.json` is the local no-hardware config. It keeps
`use_simulator=true`, `with_robot_server=true`, and cloud access disabled. IPv4
mDNS discovery is enabled, announcing `Opentrons Flex Smoketest` under
`_sila._tcp.local.`. The gRPC endpoint remains on `127.0.0.1:50051` for local
clients; the announcement does not make that loopback endpoint reachable from
other computers. Set `discovery` to `null` to disable announcements. The
deployment config in `config/flex_config.json` remains explicitly live-hardware
(`use_simulator=false`) so simulator and robot runs do not blur together.

The complete AS-MS plate-wash handoff, including its material-lineage correction,
custom-labware blockers, tip inventory, and staged real-Flex sequence, is documented
in [`docs/asms_flex_workflow_test.md`](docs/asms_flex_workflow_test.md).

### Controlled Protocol Engine run mutation

When `with_robot_server=true`, a non-terminal current Protocol Engine run has
exclusive actuation authority over the Flex. Direct SiLA actuation is rejected,
and raw `POST /runs/{run_id}/commands` is blocked after the run starts. Optional
controlled insertion is enabled only when `run_mutation_ledger_path` is configured
and a bearer token of at least 32 random characters named by
`run_mutation_token_env` is present at startup. The token is also bound to the
operator identity named by `run_mutation_actor_env`; callers cannot self-attribute
another actor in the audit. The private-state adapter is startup-gated to the
validated Opentrons 9.0.0 runtime and reports a deterministic runtime contract
identifier. Startup checks the required robot-server callables, mutable app/router
surfaces, and package origins before OT3API or the CAN bus is initialized.

Before a run starts, a small set of non-actuating setup commands remains available
only when robot-server authoritatively identifies the run as protocol-less. This
path requires the same bearer token, accepts built-in resources only, is durably
audited, and is serialized with play/resume so the run cannot start between
authorization and robot-server acceptance. Python/JSON protocol runs (including
AS-MS), custom labware-definition injection, and all actuating commands cannot use
this bypass. Stop remains available if run-state inspection is degraded.

The authenticated `/unitelabs/runs/{run_id}/...` API accepts bounded transfer,
mix, delay, comment, and recovery operations only at explicit Python-protocol
checkpoints or native recovery state. It snapshots authoritative commands,
pipettes, tips, labware, modules, disposal areas, and liquid volumes; recalculates
multi-channel tip/reagent/capacity effects; compiles only Protocol Engine commands;
accepts one ordered batch per checkpoint; and writes a durable hash-chained audit.
All other Robot Server writes are blocked while the current run owns the Flex.
Named-checkpoint and error-recovery resume also require the token and are durably
audited. Validation failures block resume, and cancellation or ambiguous partial
enqueue requires stopping the run. Send mutation traffic only through an SSH
tunnel or authenticated TLS/mTLS proxy; see the
[controlled mid-run operator procedure](docs/asms_flex_workflow_test.md#controlled-mid-run-steps)
for token provisioning, endpoint payloads, audit checks, and recovery rules.

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

The Flex host is aarch64 (ARM64) with a modern glibc, so standard
`manylinux_2_17_aarch64` dependency wheels install directly. The build also
packages the exact Opentrons 9.0.0 tag for `opentrons`, shared data, hardware,
server utilities, and `robot_server`; connector mode never relies on an
unverified mixture of those private packages from the robot image.

### Building `dist_arm/`

Build the aarch64 wheel bundle with the provided Dockerfile:

```sh
docker buildx build --platform linux/arm64 -f Dockerfile.build \
    --target export --output type=local,dest=dist_arm .
```

Or trigger the **Build Flex aarch64 Wheels** GitHub Actions workflow
(`.github/workflows/build-flex-arm-wheels.yml`) and download the `flex-arm-wheels`
artifact into `dist_arm/`.

Changes to the connector, shared acceptance contract, artifact-manifest tool,
Docker build, lockfile, or ARM workflow itself run this build on the pull
request as well as after a main-branch push. Repository branch protection
determines whether GitHub blocks merging on that job.

The artifact is accepted only when `runtime-manifest.json` and `SHA256SUMS`
verify every wheel, the connector/Opentrons/robot-server versions are exact, and
the declared target is Python 3.12 on aarch64. Every platform-specific wheel
must also carry a compatible `cp312`/`abi3` tag and an aarch64 platform tag.

### Installing on the Flex

`uv run flexctl up <robot-ip> --mode pe --operator <name>` performs every step
below in order (including credential provisioning). The manual steps remain
available for review and recovery.

Provision the mutation credential outside any replaceable release directory:

```sh
ssh root@<robot-ip> '
  umask 077
  mkdir -p /var/lib/unitelabs-opentrons-flex
  token=$(od -An -N32 -tx1 /dev/urandom | tr -d " \n")
  actor=operator-name
  {
    printf "UNITELABS_RUN_MUTATION_TOKEN=%s\n" "$token"
    printf "UNITELABS_RUN_MUTATION_ACTOR=%s\n" "$actor"
  } > /var/lib/unitelabs-opentrons-flex/run-mutation.env
  printf "Save this token for the no-echo client prompt: %s\n" "$token"
'
```

Replace `operator-name` first. Then verify, install to an immutable versioned
directory, run the no-hardware import/configuration preflight, and atomically
activate the release:

```sh
./deploy.sh <robot-ip>
```

Then install the connector service. The stock service is stopped only after the
runtime preflight passes. If connector startup, gRPC, `/health`,
`/deck_configuration`, or the mutation OpenAPI check fails within its bounded
timeout, the script restores the stock robot-server automatically:

```sh
./scripts/install_connector_service.sh <robot-ip>
```

Switch among full Protocol Engine connector mode, SiLA-only unit-operation mode,
and the stock Opentrons robot-server at any time (the choice persists across
reboot):

```sh
./scripts/switch_mode.sh <robot-ip> connector-pe
./scripts/switch_mode.sh <robot-ip> connector-unit
./scripts/switch_mode.sh <robot-ip> opentrons
```

`connector` remains an alias for `connector-pe`. The unit-operation mode requires
the selectable-config service installed by the current
`install_connector_service.sh`; rerun that installer after deploying this release.
For the exact guarded Windows commands, see
[`docs/asms_flex_workflow_test.md`](docs/asms_flex_workflow_test.md#connector-unit-fast-commissioning-path-scheme-b).

Run all general read-only readiness checks before uploading a protocol. For the
commissioning/HITL command on the Windows operator machine, including its exact
artifact-binding arguments, follow
[`docs/flex_system_acceptance.md`](docs/flex_system_acceptance.md):

```sh
uv run python scripts/preflight_flex.py <robot-ip>
```

Restore the previously activated release while leaving stock Opentrons mode
active:

```sh
./scripts/rollback_connector.sh <robot-ip>
```

Logs:

```sh
ssh root@<robot-ip> 'journalctl -u sila2-connector -f'
```

### Runtime isolation

Each immutable release inherits the robot image only for operating-system
bindings that Opentrons does not distribute as portable wheels, notably
`systemd-python`. The ARM artifact supplies the complete portable dependency
closure plus all five co-versioned Opentrons Python packages from the immutable
9.0.0 source commit. Deployment rejects incomplete releases and writes a
completion marker only after no-hardware runtime validation. Startup rejects a
wrong Python/Opentrons version, a missing or incorrectly shaped private
robot-server contract, or any of those five Opentrons modules imported from
outside the active release before initializing OT3API or the CAN bus. The
preflight report includes a deterministic `runtime_contract_id`; physical
acceptance evidence must match that identifier and the selected manifest. The
embedded `/unitelabs/runtime` route reports an identity captured by the running
connector process, so preflight rejects an active symlink or artifact that does
not match the process currently serving HTTP/gRPC.

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
uv sync --python 3.12 --all-extras
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
