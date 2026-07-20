# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] - Opentrons Flex (OT-3) port

Adapted from the opentrons-ot2 connector for the Opentrons Flex. The connector now
wraps the high-level `OT3API` (CAN) instead of the OT-2 `SmoothieDriver` (serial);
the Flex firmware is not modified.

### Added

- Complete `TemperatureController` v2 surface for Temperature Module GEN2 with
  4-95 °C unit constraints, structured current/target/status properties, atomic
  set-and-wait semantics, cancellation deactivation, cross-interface target-change
  detection without a robot-wide wait lock, explicit simulation, gRPC workflow
  coverage, and guarded real-hardware heating/cooling acceptance tests
- Complete `AbsorbanceReaderController` v2 workflow with single, referenced-single,
  and multi-wavelength initialization, module-capability validation, 96-well
  A1–H12 structured measurements, explicit simulation, and guarded hardware tests
- Complete `FlexStackerController` v2 workflow with retrieve/store operations,
  readiness interlocks, unit-annotated labware geometry and LED duration, explicit
  simulation, and guarded hardware retrieve/store acceptance tests
- Defined `InvalidWavelengthError`, `PlateReaderNotReadyError`, and
  `StackerNotReadyError` recovery paths, plus explicit Stacker travel and firmware
  configuration validation and re-home enforcement after interrupted motion
- `LiquidHandlingController` with atomic mix, touch-tip, liquid-level probing,
  aspirate/dispense while tracking, explicit transfer profiles, and execution of
  Opentrons verified liquid-class definitions for water, 80% ethanol, and 50% glycerol
- `LabwareMovementController` with locally allowlisted movement plans,
  a durable location-to-labware identity ledger, plan/deck-state discovery,
  module lid/latch interlocks under the shared hardware lock, official Flex
  gripper waypoints, mandatory pickup-width verification, lid movement,
  fail-closed restart/cancellation recovery, raw-gripper bypass prevention, and
  guarded real-hardware coverage
- Partial-tip nozzle configuration for Flex 1-, 8-, and 96-channel pipettes,
  including full, single-nozzle, and rectangular layouts with tip-rack geometry
- Dynamic liquid-volume checks, explicit SiLA units, defined liquid/nozzle/labware
  errors, and positive simulator coverage for all Flex pipette channel families
- Explicit `simulated_heater_shaker` configuration, full Heater-Shaker SiLA gRPC
  workflow coverage, and separately gated read-only/actuating real-hardware tests
- Heater-Shaker FDL units for degrees Celsius and revolutions per minute, with
  public operating-range constraints and cancellation coverage for mechanical actions
- `FlexMotionController` (OT3API wrapper): mount + deck-coordinate motion, plunger
  aspirate/dispense, status-bar/deck lights
- `GripperFeature` + `FlexGripperController` — Flex-only gripper (grip/ungrip/home jaw, status)
- `CalibrationFeature` redesigned around automatic probe-based routines
  (`ot3_calibration`: pipette offset, gripper jaw, deck) replacing OT-2 Smoothie config writes
- Defined SiLA errors with OT3API translation: `NotHomedError`, `MovementOutOfBoundsError`,
  `StallDetectedError`, `GripperNotAttachedError`, `GripActionError`,
  `CalibrationProbeNotAttachedError`, `CalibrationFailedError`
- Flex test suite: unit/simulation (OT3 simulator), gRPC + HTTP integration, and a
  real-CDK SiLA feature-definition test; runs offline via a conftest CDK stub
- aarch64 wheel build (`Dockerfile.build`, `build-flex-arm-wheels` workflow) and Flex
  deploy/service/switch-mode scripts

### Changed

- Version 0.8.0 completes the Temperature Module GEN2 SiLA surface and its
  simulator-to-HITL verification path.
- **BREAKING**: every public Feature Definition now follows SiLA Part A naming:
  `MotionController`, `PipetteController`, `GripperController`,
  `CalibrationController`, `HeaterShakerController`, and
  `ThermocyclerController` replace identifiers ending in `Feature`. Generated
  clients must be regenerated for the renamed gRPC packages and services.
- **BREAKING**: `HeaterShakerController` is version 3.0. `SetSpeed` and
  `GetSpeed` replace identifiers that embedded the abbreviated rpm unit; rpm is
  represented only by the SiLA unit constraint.
- Stacker workflow and maintenance concerns are now separate Feature Definitions:
  routine retrieve/store operations remain in `FlexStackerController`, while raw
  axis, latch, LED, motor-stop, and full-home recovery operations are exposed by
  `FlexStackerMaintenanceController`.
- Reader, Stacker, and Temperature dynamic status endpoints are observable SiLA
  properties. The Absorbance Reader's pinned Opentrons compatibility logic and
  explicit simulator behavior now live behind connector-owned IO adapters.
- Stacker recovery authority is shared by the SiLA and robot-server module paths,
  and non-finite Temperature Module targets are rejected before hardware access.
- Every attached module exposed through the embedded robot-server now uses the
  connector-wide hardware lock. Stacker recovery is checked after lock acquisition,
  cancelled Stacker motion deactivates before unlocking, and cancelled Plate Reader
  work retains ownership until the native operation has settled.
- Shared-hardware injection now preserves robot-server's native lifespan and
  explicitly completes its skipped Protocol Engine post-initialization callbacks,
  keeping `/runs`, `/protocols`, and `/commands` operational while the connector
  remains the sole owner of asynchronous hardware cleanup.
- **BREAKING**: the Temperature Module Feature Definition is now the
  `TemperatureController` version 2.0. The parameter is the unit-annotated
  `Temperature`; `SetTemperatureAndWait` replaces the ambiguous independent wait
  target; and `Status`/`DeviceInfo` properties replace read commands. Generated
  SiLA clients must be regenerated; the endpoint mapping is in the README.
- Version 0.7.0 completes the SiLA surfaces and pre-hardware validation paths for
  the Flex Stacker and Absorbance Plate Reader official accessories.
- **BREAKING**: the Stacker and Absorbance Reader Feature Definitions are now
  version 2.0 controller features. Status and device identity are SiLA properties;
  Stacker motion returns structured state instead of success booleans; reader
  results carry wavelength and well identifiers instead of unlabeled matrices.
  This pre-1.0 migration requires generated SiLA clients to be regenerated; the
  endpoint mapping is documented in the README.
- Version 0.6.0 expands the connector from primitive-only pipetting to a
  SiLA-native advanced liquid-handling and labware-movement surface while
  retaining all existing primitive endpoints.
- **BREAKING**: `MotionControlFeature` is now version 2.0 because primitive
  liquid volumes are strictly positive and the public FDL adds units and Defined
  Execution Errors. `GripperFeature` is version 1.2 for the additive `JawWidth`
  property, and `PipetteFeature` is version 1.1 for partial-nozzle commands.
- **BREAKING**: Heater-Shaker Feature Definition is now version 2.0. Temperature
  command parameters use the unit-free `Temperature` identifier, and temperature/
  speed responses expose explicit target-active fields instead of Optional values.
- Firmware `FailedCommand` responses are reported as `ModuleOperationError` while
  preserving the vendor error response, rather than being mislabeled as disconnects.
- `MotionControlFeature` rewritten around `OT3Mount` (LEFT/RIGHT/GRIPPER) and deck
  coordinates instead of raw X/Y/Z/A/B/C Smoothie axes
- `PipetteFeature` reports Flex pipette models via `cache_instruments`/`attached_instruments`
- In-process robot-server wiring builds `OT3API` (CAN) instead of OT-2 `API` (serial)
- Deployment targets aarch64 + modern glibc — removed the OT-2 armv7 from-source
  grpcio/OpenSSL build

### Removed

- `MagneticModuleFeature` / `MagneticModuleController` — the Flex uses the passive
  Magnetic Block (no controller); magnetic module is unsupported
- OT-2 Smoothie motion layer, GPIO button/ALSA tone, per-axis motor-current commands

## [0.4.0] - 2026-05-26

### Added

- `PipetteFeature` — GetAttachedPipettes and ConfigureMount via SiLA2
- `CalibrationFeature` — raw Smoothie EEPROM write commands for axis calibration
- `MotionControlFeature` extensions: axis bounds validation, motor current management, BoardRevision, SerialNumber, DisengageAxes, aspirate/dispense commands
- Dynamic module feature loading at startup via `/dev/ot_module*` scan (heater-shaker, thermocycler, temperature, magnetic)
- `with_robot_server=True` mode: in-process opentrons HTTP robot-server sharing one HardwareAPI and serial port with the SiLA2 gRPC server via `HardwareProxy` and `asyncio.Lock`
- `HardwareProxy` shim for in-process driver sharing between `OT2MotionController` and robot_server
- HTTP + gRPC integration test suite running against the simulator in CI
- Two CI matrix targets: Python 3.10/opentrons 8.8.1 and Python 3.12/opentrons 9.0.0
- Self-contained ARM wheel bundle for OT-2 deployment
- Tailscale setup script

### Changed

- **Enum-based API throughout**: replaced all magic axis/mount string literals with `Axis` and `Mount` enums across feature and IO layers; SiLA2 parameters and responses now use Set constraints instead of unvalidated strings, giving clients introspectable valid values and preventing invalid inputs at the protocol level
- `.pyc` bytecode now precompiled into `/var/cache/sila2-pycache` at deploy time (eliminates 39-minute cold start caused by read-only `/usr/lib` filesystem on OT-2)

## [0.3.0] - 2026-04-17

### Changed

- `MotionControlFeature` now includes GPIO methods (set_button_light, set_rail_lights, read_button, read_door_switch)
- Removed standalone `GPIOControlFeature` in favor of unified motion+GPIO feature
- Redesigned move commands to avoid Optional types (unitelabs-cdk 0.9.0 compatibility)
  - `move_to(position, speed)` - Move all axes to position
  - `move_axis(axis, position, speed)` - Move single axis
  - `move_relative_axis(axis, delta, speed)` - Relative single axis move

### Added

- `HeaterShakerFeature` - SiLA2 feature for Heater-Shaker module (temperature, shaking, latch control)
- `ThermocyclerFeature` - SiLA2 feature for Thermocycler module (lid, plate temperature control)
- `TemperatureModuleFeature` - SiLA2 feature for Temperature Module
- `MagneticModuleFeature` - SiLA2 feature for Magnetic Module (engage/disengage magnets)
- `config/robot_settings.json` - Default OT-2 robot settings

## [0.2.0] - 2026-04-16

### Changed

- **BREAKING**: Refactored IO layer to use Opentrons driver layer instead of direct serial communication
- Replaced custom `SmoothieConnection` with wrapper around `opentrons.drivers.smoothie_drivers.SmoothieDriver`
- Replaced custom `GPIOController` with wrapper around `opentrons.drivers.rpi_drivers.GPIOCharDev`
- Now depends on `opentrons>=8.0.0` package instead of direct `pyserial` implementation

### Added

- `OT2MotionController` - High-level motion controller using proven Opentrons SmoothieDriver
  - Proper homing sequences with current management, unstick moves, and axis ordering
  - Move with backlash compensation and move splitting
  - GPIO control (lights, buttons, door switch)
- `HeaterShakerController` - Wrapper for Heater-Shaker module driver
- `ThermocyclerController` - Wrapper for Thermocycler module driver
- `TemperatureModuleController` - Wrapper for Temperature module driver
- `MagneticModuleController` - Wrapper for Magnetic module driver
- `Temperature` and `RPM` dataclasses for type-safe readings

### Deprecated

- Direct serial implementation moved to `_legacy_direct.py`
- Direct GPIO implementation moved to `_legacy_gpio.py`

## [0.1.0] - 2026-04-16

### Added

- Initial project structure using UniteLabs connector-factory template
- `SmoothieConnection` class for direct GCode serial communication with Smoothie controller
- `SimulatingSmoothieConnection` for testing without hardware
- `GPIOController` class for direct Linux sysfs GPIO control
- `SimulatingGPIOController` for testing without hardware
- `MotionControlFeature` SiLA2 feature with 5 key commands:
  - `home` - Home specified axes (XYZABC)
  - `move` - Move to absolute position
  - `get_position` - Get current axis positions
  - `set_lights` - Control button and rail lights
  - `emergency_stop` - Emergency stop with GPIO reset
- `GPIOControlFeature` SiLA2 feature with GPIO commands:
  - `set_button_light` - RGB button LED control
  - `get_button_light` - Read button LED state
  - `set_rail_lights` - Deck rail light control
  - `get_rail_lights` - Read rail light state
  - `read_button` - Read button press state
  - `read_door_switch` - Read door switch state
- Auto-detection of Smoothie serial port (internal UART /dev/ttyAMA0 on OT-2)
- Configuration options for simulator mode, serial port, and baud rate
- `dist_minimal/ot2_connector.py` - Single-file distribution (12KB)
- `deploy.sh` - Automated deployment script for OT-2 (uses ssh+cat, no scp required)
- `gcode_test.py` - Standalone GCode test script (no SiLA2 dependencies)

### Changed

- Removed `opentrons` package dependency to avoid jsonschema version conflicts
- Implemented direct GCode protocol communication instead of using opentrons drivers
- Dependencies: only `unitelabs-cdk~=0.9.0` and `pyserial>=3.5`

### Tested

- GCode communication verified on OT-2 via /dev/ttyAMA0
- Commands tested: M115 (firmware), M114.2 (position), M119 (limit switches)
