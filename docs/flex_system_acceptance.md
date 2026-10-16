# Flex system acceptance workflow

This acceptance campaign is designed for the final simulator-to-hardware handoff. It combines a publishable
Unitelabs workflow with a direct SiLA 2 gRPC hardware-in-the-loop test so failures can be separated into workflow,
connector, robot, and physical-preparation layers.

The campaign uses two prepared plates and one reader lid:

- the process plate runs through the Thermocycler, Heater-Shaker, and passive Magnetic Block;
- the assay plate runs through liquid handling, the Temperature Module, and the Absorbance Plate Reader;
- the Stacker retrieves and stores a separate item as an independent round trip.

The Stacker is intentionally not chained to a Gripper move. Its shuttle state is owned by the Stacker feature, while
the Gripper's durable deck ledger is owned by `LabwareMovementController`. Until those state models share one atomic
handoff, claiming that a retrieved item is available to a Gripper plan would be unsafe.

## What the campaign validates

The direct hardware path verifies:

1. the SiLA gRPC service and the parallel native robot-server HTTP health endpoint reach the same live Flex;
2. the run is not connected to a simulator and all declared module serial numbers match;
3. the selected pipette is attached and the requested liquid volumes fit its advertised range;
4. every Gripper/lid plan is locally provisioned, has the correct plate identity and direction, and the deck ledger is valid;
5. homing, deck lights, full nozzle configuration, sensor-verified tip pickup/drop, liquid probing, mixing, an explicit
   transfer profile, a verified water liquid class, and touch-tip;
6. a Thermocycler profile with lid control, Heater-Shaker heat/shake/latch control, Temperature Module hold, Stacker
   retrieve/store, Plate Reader initialization and 96-well multi-wavelength measurement;
7. every Gripper move through an allowlisted plan, including the passive Magnetic Block and Plate Reader lid handling;
8. machine error state and durable deck-state validity after every Gripper plan.

The normal simulator suite remains the exhaustive endpoint layer. The physical campaign is intentionally risk-based:
it does not trigger Emergency Stop, calibration, raw Gripper jaw commands, arbitrary Stacker maintenance-axis motion,
or every alternative nozzle layout. Those operations either change calibration/recovery state or add collision risk
without improving the assay-path acceptance signal.

## Prepare the manifest and connector

Copy [`config/flex_acceptance.example.json`](../config/flex_acceptance.example.json) outside the repository and replace
every `REPLACE...` value. The parser rejects missing fields, unknown fields, placeholder strings, unsafe ranges,
inconsistent well/retract geometry, insufficient source liquid, duplicate plan identifiers, and a reader reference
without a distinct sample wavelength before it sends a hardware command.

The pipetting coordinates are commissioning inputs for a trained local operator; they are not a remotely editable
runtime layout. Derive them from the installed Opentrons labware definitions and calibrated deck, review clearances,
then run the direct HITL path under physical supervision. Only a successful run records
`commissioned_manifest_sha256` in JUnit. The
published Unitelabs workflow refuses every manifest except that exact fingerprint, provisioned by an administrator as
`FLEX_ACCEPTANCE_MANIFEST_SHA256`, so a routine workflow caller cannot substitute coordinates or module identities.

The manifest never supplies Gripper coordinates. Configure the connector's local `labware_movement_config` with the
11 named round-trip/chain plans, validated grip points from installed labware definitions, a durable `state_file`, and
the true `initial_occupancy`. Restart the connector, inspect `AvailablePlans` and `DeckState`, and do not replace a
ledger after an interrupted move until the physical deck has been reconciled.

Operator preparation checklist:

- close the Flex door, disengage the E-stop, clear all travel corridors, and keep the E-stop reachable;
- install the declared pipette and Gripper; place one compatible tip at `tip_pickup` and prepare `tip_drop`;
- add at least `prepared_source_volume` of safe test liquid at the declared source geometry;
- place the process plate, assay plate, and reader lid at the initial locations recorded by the deck ledger;
- install compatible adapters/labware on the Heater-Shaker, Thermocycler, Temperature Module, and Plate Reader;
- leave the reader empty with its lid on before the run;
- load one compatible Stacker item, leave the shuttle empty, close the hopper door, and measure the assembled height;
- verify that all module serial numbers and reader wavelengths match the manifest.

## Run the direct SiLA hardware acceptance

First run the simulator and static acceptance tests:

```sh
uv run pytest -p no:cacheprovider tests/test_acceptance_manifest.py tests/integration/test_grpc_thermocycler.py tests/integration/test_grpc_advanced_flex.py tests/integration/test_grpc_tip_controller.py tests/integration/test_grpc_pipette.py -q
```

Immediately before actuation, generate a no-motion readiness report from the operator computer:

```powershell
uv run python scripts/preflight_flex.py ROBOT_HOST --acceptance-manifest C:\path\to\flex_acceptance.json --runtime-manifest C:\path\to\verified-artifact\runtime-manifest.json --output .\flex-hitl-readiness.json
```

This command is native Python and works from PowerShell, Command Prompt, macOS, or Linux. On Windows it uses the
built-in OpenSSH client to run the robot-side, no-hardware runtime check; install the Windows OpenSSH Client feature if
`ssh` is unavailable. The remote shell remains on the Flex and is not executed locally. Use the
`runtime-manifest.json` from the wheel bundle that was verified before deployment. The check compares that artifact's
release ID and bundle digest with both the active Flex venv and the immutable release identity captured by the
currently running connector process. A symlink change without a service restart therefore blocks readiness instead
of approving the stale process.

Then run only the guarded campaign against the robot. The manifest gate, fresh readiness evidence, and explicit
actuation gate are all required. The pytest fixture reruns the same live preflight in-process and overwrites the
specified report immediately before any actuation; an old or hand-edited JSON file is not an authorization token.
Keep the JUnit file with the robot/module/release identity evidence:

```powershell
uv run pytest -p no:cacheprovider tests/integration/hardware/test_hitl_full_workflow.py --robot ROBOT_HOST:50051 --acceptance-manifest C:\path\to\flex_acceptance.json --acceptance-runtime-manifest C:\path\to\verified-artifact\runtime-manifest.json --acceptance-readiness-report .\flex-hitl-readiness.json --acceptance-workflow-actuation --junitxml=.\flex-acceptance.xml -vv
```

On failure, the runner de-energizes module actuators and lights, but it never guesses a Gripper recovery route. Inspect
the physical deck, reconcile the state ledger, home the robot, and only then restart the campaign.

Evidence states are deliberately non-interchangeable:

- `OFFLINE_VALIDATED` describes simulator/static coverage only;
- `READY_FOR_HITL` is emitted by the live, no-motion preflight and never claims physical success;
- `HARDWARE_PASSED` appears only in JUnit after every guarded physical phase and every safety-settlement operation
  completes. Cleanup failure blocks this state.

## Run or publish the Unitelabs workflow

The workflow package uses the same shared `AcceptanceManifest` contract as the connector and calls the independent SiLA
features through the Unitelabs SDK. After the direct run passes, provision its JUnit
`commissioned_manifest_sha256` in the workflow worker environment, then run the identical manifest:

```powershell
$env:FLEX_ACCEPTANCE_MANIFEST_SHA256 = "THE_PASSED_JUNIT_DIGEST"
uv run --directory workflows/flex-system-acceptance workflow --manifest C:\path\to\flex_acceptance.json --device "Opentrons Flex"
```

`uv` selects the workflow's Python 3.12 environment. It does not install the
Python 3.10 hardware connector into Windows: both sides share the dependency-free
`unitelabs-flex-acceptance-contract` package instead.

The routine workflow always attempts every de-energization action: Heater-Shaker
stop and heater deactivation, Thermocycler deactivation, Temperature Module
deactivation, Plate Reader deactivation, Stacker deactivation, and lights off.
Failures are accumulated instead of stopping cleanup early, but any failure
marks the workflow unsuccessful and requires operator reconciliation. A
completed hardware sequence with incomplete shutdown is never logged as an
accepted run.

To publish it with the repository's existing workflow tooling:

```powershell
Set-Location workflows
uv run scripts/publish_workflows.py flex-system-acceptance
```

Use the direct gRPC test for commissioning and troubleshooting because its JUnit evidence includes target mode, host,
module serials, pipette identity, manifest fingerprint, phase names, and the final acceptance result. Use the published
workflow for routine operator execution after that exact hardware/layout combination has passed commissioning. If any
coordinate, labware, module identity, or operating value changes, rerun direct commissioning and update the approved
fingerprint; never compute and approve a new fingerprint merely to bypass a failed workflow gate.

Deployment and robot service switching are a separate maintainer path. The repository's `deploy.sh` and service
scripts target the Flex Linux host and require CI, macOS, WSL, or Git Bash; the lab's native PowerShell path above does
not depend on those scripts. The Mac checkout can therefore remain a backup/test operator without becoming the source
of truth for the Windows `main` deployment.
