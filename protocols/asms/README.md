# AS-MS Flex connector validation bundle

This directory contains the prepared candidate protocol for the AS-MS wash and
elution workflow. It is executed through the connector's embedded Opentrons HTTP
API and Protocol Engine, not through the first-class SiLA liquid commands:

`POST /protocols -> protocol analysis -> POST /runs -> shared OT3API`

## Files required for an exact upload

Upload these three bundled files together in the Opentrons App or through
`POST /protocols`:

1. `asms_single_point_wash_and_elute.py`
2. `labware/thermokingfisherdeepwell_96_wellplate_2000ul.json`
3. `labware/azenta_96_wellplate_200ul_pcr.json`

Do not rename a different definition to satisfy the preflight. Well geometry,
bottom height, deck offsets, and gripper clearance are physical safety inputs.

The default preflight uses the checked-in `labware/` directory and must print
`READY`:

```sh
uv run python scripts/validate_asms_protocol.py
```

Logic-only shadow simulation remains available as an explicitly non-production
comparison:

```sh
uv run python scripts/validate_asms_protocol.py --shadow
```

`SHADOW PASS` is not permission to run on hardware.

The offline connector acceptance test uploads the exact three-file bundle through
the embedded robot-server and executes the complete two-column run. It verifies
462 Protocol Engine commands, including 26 tip pickups, 82 aspirations, 66
dispenses, 9 gripper moves, Temperature Module cleanup, and post-run homing.
The Python Protocol API exact run independently verifies 344 high-level run-log
records and 634 programmed delay seconds. Semantic SHA-256 values and geometry
expectations are pinned by the protocol tests and documented in `labware/README.md`.

## Changes from the supplied work-in-progress file

- Corrected material lineage after wash 2. The bead-containing liquid moves to
  the elution plate; methanol is added to that elution plate; the clarified
  eluate is transferred from the elution plate to the MS plate.
- Added descriptive plate labels so analysis and run logs distinguish the two
  identical Axygen plates.
- Added a `finally` cleanup that deactivates the Temperature Module when Python
  execution exits with an error.
- Added runtime parameters. Scientific defaults remain two columns and full
  90-second/120-second timing. `connector_test_mode=true` shortens protocol
  delays, and `number_of_columns=1` provides a first-contact mechanics run.
  `enable_mutation_checkpoints=false` remains the safe default; enabling it adds
  seven connector-recognized, audited phase-boundary pauses.
- Declared all starting reagent/sample volumes plus zero-volume destination and
  waste markers so controlled mutation can fail closed on reagent and capacity
  calculations instead of guessing liquid state.
- Kept the original full-column tip strategy. The two-column workflow consumes
  all 20 fresh tip columns plus four physical parked-tip columns; both A3 and C3
  racks must be complete at the start.

The source file in `~/Downloads` is intentionally unchanged.

The controlled-mutation API, ownership guarantees, checkpoint list, token setup,
and operator procedure are documented in
[`../../docs/asms_flex_workflow_test.md`](../../docs/asms_flex_workflow_test.md#controlled-mid-run-steps).
