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
PYTHONPATH=src .venv/bin/pytest -p no:cacheprovider \
  tests/test_acceptance_manifest.py \
  tests/integration/test_grpc_thermocycler.py \
  tests/integration/test_grpc_advanced_flex.py \
  tests/integration/test_grpc_tip_controller.py \
  tests/integration/test_grpc_pipette.py -q
```

Then run only the guarded campaign against the robot. The manifest gate and the explicit actuation gate are both
required. Keep the JUnit file with the robot/module identity evidence:

```sh
PYTHONPATH=src .venv/bin/pytest -p no:cacheprovider \
  tests/integration/hardware/test_hitl_full_workflow.py \
  --robot ROBOT_HOST:50051 \
  --acceptance-manifest /absolute/path/flex_acceptance.json \
  --acceptance-workflow-actuation \
  --junitxml=flex-acceptance.xml -vv
```

On failure, the runner de-energizes module actuators and lights, but it never guesses a Gripper recovery route. Inspect
the physical deck, reconcile the state ledger, home the robot, and only then restart the campaign.

## Run or publish the Unitelabs workflow

The workflow package uses the same connector-side `AcceptanceManifest` contract and calls the independent SiLA
features through the Unitelabs SDK. After the direct run passes, provision its JUnit
`commissioned_manifest_sha256` in the workflow worker environment, then run the identical manifest:

```sh
FLEX_ACCEPTANCE_MANIFEST_SHA256=THE_PASSED_JUNIT_DIGEST \
uv run --directory workflows/flex-system-acceptance workflow \
  --manifest /absolute/path/flex_acceptance.json \
  --device "Opentrons Flex"
```

To publish it with the repository's existing workflow tooling:

```sh
cd workflows
uv run scripts/publish_workflows.py flex-system-acceptance
```

Use the direct gRPC test for commissioning and troubleshooting because its JUnit evidence includes target mode, host,
module serials, pipette identity, manifest fingerprint, phase names, and the final acceptance result. Use the published
workflow for routine operator execution after that exact hardware/layout combination has passed commissioning. If any
coordinate, labware, module identity, or operating value changes, rerun direct commissioning and update the approved
fingerprint; never compute and approve a new fingerprint merely to bypass a failed workflow gate.
