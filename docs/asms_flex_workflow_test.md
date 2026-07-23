# AS-MS workflow: offline evidence and real-Flex handoff

## Current readiness

| Layer | Status | Evidence or remaining gate |
| --- | --- | --- |
| Python syntax and Protocol API 2.27 | Pass | Installed Opentrons 8.8.1 supports API 2.27 |
| Full command graph | Pass with exact labware | 344 run-log records; 82 aspirates, 66 dispenses, 9 gripper moves, and 634 programmed seconds |
| Tip lifecycle | Pass | 26 pickups: 20 discarded full columns and 6 returned/reused pickups |
| Temperature cleanup | Pass | Deactivation is in `finally` and appears once in the run log |
| Material lineage | Corrected and tested | `AS-MS -> elution -> magnetic block -> MeOH -> MS` |
| Connector HTTP/Protocol Engine execution | Pass with exact uploaded bundle in local robot-server stack | Full two-column run succeeds with 462 Protocol Engine commands, including 26 tip pickups, 82 aspirations, 66 dispenses, 9 gripper moves, and safe post-run recovery |
| Controlled mid-run insertion | Pass in local robot-server stack | Bearer-authenticated transfer at an explicit checkpoint compiled to 6 Protocol Engine commands, consumed 8 clean tips, updated one reservoir source and all 8 destination wells, was hash-chain audited, and executed before the next Python step |
| Exact Thermo KingFisher waste plate geometry | Pass offline | Bundled schema-v2 definition, loadName and 2,000 µL/well geometry validated and pinned |
| Exact Azenta/FroggaBio FS-96 MS plate geometry | Pass offline | Bundled schema-v2 definition, loadName and 200 µL/well geometry validated and pinned |
| Axygen plate gripper geometry | Hardware gate | Built-in definition has no dedicated Flex gripper parameters and is not on the official verified movement list |

The optional shadow test still substitutes NEST 2 mL deep-well and Opentrons Tough
200 µL PCR definitions in memory, but it is no longer used as exact readiness
evidence. The HTTP execution test uploads both bundled custom definitions and
additionally verifies that the embedded robot-server identifies the shared
hardware as Flex, uses an explicitly attached simulator gripper, and completes
Opentrons' ordered post-run halt/reset/home sequence.

## Prepared deck and consumables

| Slot | Item | Pre-run state |
| --- | --- | --- |
| A1 | Axygen 96-well 500 µL AS-MS plate | Columns 1-2 contain 250 µL/well for full run |
| A2 | Azenta/FroggaBio FS-96 MS plate | Empty and seated; exact custom definition uploaded |
| A3 | Flex 1000 µL tip rack | Complete rack, all columns present |
| B1 | Axygen 96-well 500 µL elution plate | Empty and seated |
| B2 | Magnetic Block GEN1 | Empty; correct orientation |
| C1 | Temperature Module GEN2 + NEST 12-reservoir | Module connected; reservoir seated |
| C2 | Thermo KingFisher 2 mL waste plate | Empty; exact custom definition uploaded |
| C3 | Flex 1000 µL tip rack | Complete; columns 5-8 reserved for returned wash-mixing tips |
| D1 | Flex trash bin | Installed and empty |

Only the right `flex_8channel_1000` is used. The full two-column run uses every
physical tip in both racks: 160 fresh tips plus 32 parked/reused tips. There is no
retry margin. Replace both racks if any tip is missing, bent, or previously used.

The protocol's authoritative starting-volume declarations already include a
conservative reserve above net consumption. Prepare these declared volumes; if
the lab's validated dead volume requires more, update both the physical fill and
the matching `load_liquid()` value before analysis:

- A2 wash buffer 1: 4.0 mL declared; 3.2 mL net workflow use.
- A3 wash buffer 2: 2.0 mL declared; 1.6 mL net workflow use.
- A4 80% MeOH: 2.4 mL declared; 1.92 mL net workflow use; each pre-wet cycle temporarily aspirates 0.96 mL.

Each C2 waste-plate well in columns 1-2 receives about 550 µL liquid across the
workflow. Each final MS well receives 100 µL, leaving about 20 µL methanol eluate
per elution well.

## Controlled mid-run steps

Controlled insertion is disabled in ordinary scientific runs. Set the protocol
runtime parameter `enable_mutation_checkpoints=true` only when the operator plans
to inspect or add a step. The Python protocol then pauses at these seven named
phase boundaries:

1. `ready-before-initial-separation`
2. `before-wash-1-round-1`
3. `before-wash-1-round-2`
4. `before-wash-2-transfer`
5. `before-elution-plate-separation`
6. `before-methanol-elution`
7. `before-ms-plate-transfer`

The connector enforces the following boundary for every non-terminal current run:

- Protocol Engine owns the Flex. Independent SiLA motion, pipetting, gripper,
  calibration, module actuation, and labware movement are rejected.
- A manual pause is not an insertion point. Normal steps are accepted only while
  the running command is a `waitForResume` whose message begins
  `UNITELABS_MUTATION_CHECKPOINT:`. Recovery steps require the native
  `awaiting-recovery` state.
- The planning snapshot comes from the current Protocol Engine state and includes
  command history/current command, pipettes and attached-tip state, every tip-rack
  well, labware, modules, disposal areas, and tracked well volumes/capacities.
- Transfer and mix validation allocates only complete clean tip sets for the
  active nozzle map and recalculates source reagent, destination/waste capacity,
  pipette/tip working volume, conservative device-derived flow ceilings, and
  every affected multi-channel well. Non-finite numbers are rejected at the API.
- Accepted steps compile only to Protocol Engine commands. Checkpoint commands use
  `PROTOCOL` intent; recovery commands use `FIXIT` intent.
- One checkpoint accepts one resource-reserving mutation batch containing up to
  100 ordered steps. This prevents a second queued batch from allocating tips or
  liquid that the first queued batch has not executed yet.
- Every request, approval/rejection, compiled command, enqueue result, actor,
  client host, reason, timestamp, and state fingerprint is fsynced to a local
  hash-chained JSONL ledger.
- Any validation or audit failure creates a resume-blocking hold. A possibly
  partial enqueue is fatal and requires stopping the run. Holds are reconstructed
  from the ledger after connector restart.

### Provision authentication

The checked-in configuration contains only the environment-variable name and
ledger path, never the secret. Before installing or starting the service on a
Flex, provision a long random token locally on the robot:

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
  chmod 600 /var/lib/unitelabs-opentrons-flex/run-mutation.env
  printf "Copy this token into the no-echo client prompt: %s\n" "$token"
'
```

The token is generated on the robot instead of appearing in shell history or
process arguments. It must contain at least 32 characters. Replace
`operator-name` before running the command; every mutation or hold-release
request must use that exact `actor`. The connector records the environment-bound
identity, not an untrusted self-declared value. The service also fails closed
unless the installed Python 3.10, Opentrons, and embedded robot-server runtime is
the validated 9.0.0 target.
Its durable ledger is
`/var/lib/unitelabs-opentrons-flex/run-mutations.jsonl`.

Do not send this token directly over port 31950. Open an encrypted SSH tunnel and
keep the mutation client on local loopback (or use an authenticated TLS/mTLS
reverse proxy):

```sh
ssh -N -L 31951:127.0.0.1:31950 root@<robot-ip>
```

### Inspect, submit, audit, then resume

Use the IDs returned by the snapshot; never guess them from deck slots. This
example adds one 10 µL, eight-channel transfer and discards the tips into the
loaded Flex trash area:

```sh
FLEX_API=http://127.0.0.1:31951
FLEX_RUN_ID=<run-id>
read -r -s MUTATION_TOKEN
export MUTATION_TOKEN

curl -fsS \
  -H "Authorization: Bearer ${MUTATION_TOKEN}" \
  "${FLEX_API}/unitelabs/runs/${FLEX_RUN_ID}/mutation-snapshot"

curl -fsS -X POST \
  -H "Authorization: Bearer ${MUTATION_TOKEN}" \
  -H 'Content-Type: application/json' \
  "${FLEX_API}/unitelabs/runs/${FLEX_RUN_ID}/mutations" \
  -d '{
    "mutationId": "<new-uuid>",
    "actor": "<operator-name>",
    "reason": "<scientific-or-recovery-reason>",
    "mode": "checkpoint",
    "steps": [{
      "stepType": "transfer",
      "pipetteId": "<snapshot-pipette-id>",
      "tipRackIds": ["<snapshot-tip-rack-id>"],
      "source": {"labwareId": "<source-id>", "wellName": "A2"},
      "destination": {"labwareId": "<destination-id>", "wellName": "A1"},
      "disposal": {
        "disposalType": "addressableArea",
        "addressableAreaName": "<snapshot-trash-area-name>"
      },
      "volume": 10,
      "aspirateFlowRate": 10,
      "dispenseFlowRate": 10
    }]
  }'

curl -fsS \
  -H "Authorization: Bearer ${MUTATION_TOKEN}" \
  "${FLEX_API}/unitelabs/runs/${FLEX_RUN_ID}/mutations"
```

Resume only after the submission returns HTTP 201, the predicted volumes and tip
allocation are correct, and the audit ends in `mutation_enqueued`. Reusing the
same UUID with the identical body is idempotent; changing its body is rejected.
Put every intended addition for that phase into the same `steps` array; a second
new UUID at the same checkpoint is rejected. If validation is rejected, review
the audit and either correct the physical/state problem or explicitly discard the
rejected proposal with:

```sh
curl -fsS -X POST \
  -H "Authorization: Bearer ${MUTATION_TOKEN}" \
  -H 'Content-Type: application/json' \
  "${FLEX_API}/unitelabs/runs/${FLEX_RUN_ID}/mutation-hold/release" \
  -d '{"actor":"<operator-name>","reason":"discard rejected proposal after review"}'
```

That release is itself audited and is not allowed for a fatal/partially enqueued
mutation. In that case, stop the run and inspect the Protocol Engine command log.
At every named checkpoint, `play` itself also requires the same bearer token and
adds a durable resume-authorization audit event. Resume through the encrypted
tunnel, not the Opentrons App:

```sh
curl -fsS -X POST \
  -H "Authorization: Bearer ${MUTATION_TOKEN}" \
  -H 'Content-Type: application/json' \
  "${FLEX_API}/runs/${FLEX_RUN_ID}/actions" \
  -d '{"data":{"actionType":"play"}}'
```

An authenticated `play` with no mutation is the explicit, audited
"continue without insertion" acknowledgement. Unauthenticated clients cannot
skip a named checkpoint. The two native `resume-from-recovery` action variants
have the same token and audit requirement while the run is `awaiting-recovery`.

## Fastest safe real-Flex sequence

From macOS or Linux, `scripts/run_asms_hardware.py` provides the shortest
operator path through exact offline preflight, live connector health, deck
fixture validation, upload, analysis, execution, and command-count evidence.
Its default is analysis-only and cannot move hardware:

```sh
uv run --extra test python scripts/run_asms_hardware.py \
  --host <robot-ip> --columns 1
```

After the empty-plate gripper qualification and the physical deck checklist
below are complete, opt in to the shortened one-column mechanics run:

```sh
uv run --extra test python scripts/run_asms_hardware.py \
  --host <robot-ip> --columns 1 --execute \
  --confirm-deck-ready ASMS-DECK-READY
```

Replace all test liquids and both complete tip racks before the two-column run:

```sh
uv run --extra test python scripts/run_asms_hardware.py \
  --host <robot-ip> --columns 2 --execute \
  --confirm-deck-ready ASMS-DECK-READY
```

Add `--scientific` only for the final real-sample run; it restores the full
programmed delays. After the first one-column mechanics pass, the same runner can
exercise the complete controlled-insertion path. Keep the SSH tunnel shown above
open, export the robot-provisioned token without placing it on the command line,
then provide the exact actor bound to that token:

```sh
read -r -s UNITELABS_RUN_MUTATION_TOKEN
export UNITELABS_RUN_MUTATION_TOKEN
uv run --extra test python scripts/run_asms_hardware.py \
  --host 127.0.0.1 --port 31951 --columns 1 --execute \
  --checkpoint-transfer --mutation-actor <operator-name> \
  --confirm-deck-ready ASMS-DECK-READY
```

This inserts one 10 µL eight-channel reservoir-to-waste transfer at the first
named checkpoint, verifies allocation of eight clean tips and a terminal
`mutation_enqueued` audit record, then authenticates the remaining six checkpoint
resumes. The option is rejected for two-column runs because the base protocol
already consumes every available fresh tip.

1. **Verify the exact bundle offline.** Run the default exact preflight against
   the checked-in protocol and `protocols/asms/labware/`. Continue only when it
   prints `READY`; record the pinned definition hashes.
2. **Prepare rollback before deployment.** Provision
   `/var/lib/unitelabs-opentrons-flex/run-mutation.env`, run `deploy.sh`, and
   require its artifact and no-hardware runtime preflight to pass. Keep both
   `scripts/switch_mode.sh <robot-ip> opentrons` and
   `scripts/rollback_connector.sh <robot-ip>` ready. The checked-in live
   configuration requires `use_simulator=false`, `with_robot_server=true`, and
   `run_mutation_required=true`.
3. **Start read-only.** Verify connector logs, `GET /health`, `GET /pipettes`,
   `GET /modules`, and SiLA `MachineStatus`. Confirm the right pipette model and
   Temperature Module identity; release E-stop, close the door, and clear errors.
4. **Match the Flex deck configuration.** In the Opentrons App, set A3 to a
   right-slot fixture for the tip rack, D1 to the trash-bin adapter, C1 to the
   connected Temperature Module GEN2, and B2 to Magnetic Block GEN1. Confirm
   every other cutout matches the table above; the Flex default deck reserves
   A3 for trash and treats C1/B2 as ordinary slots, so physical placement alone
   is not sufficient.
5. **Analyze without motion.** Upload the prepared Python file and both exact
   labware JSON files. Confirm protocol analysis completes without warnings or
   labware-offset requests. Do not press Run yet.
6. **Perform labware position checks.** Use the Opentrons App to check A1, A2,
   B1, C2, both tip racks, and reservoir positions with the exact labware files.
7. **Qualify the unverified Axygen gripper path.** With an empty Axygen plate,
   no tips, no liquids, and E-stop ready, validate one A1 -> B2 -> A1 round trip
   and one B1 -> B2 -> B1 round trip. Stop immediately on jaw-width, alignment,
   scraping, or seating uncertainty. This gate cannot be completed offline.
8. **Run one-column connector test.** Load non-hazardous test liquid, select
   `connector_test_mode=true`, `number_of_columns=1`, and
   `enable_mutation_checkpoints=true`. At the first checkpoint, read the snapshot,
   add one small transfer, verify its audit and predicted resources, then resume
   through the authenticated SSH-tunnel request above while monitoring connector
   and Protocol Engine logs.
9. **Run two-column connector test.** Replace both tip racks, reset all plates
   and test liquids, keep test mode enabled, and select two columns.
10. **Run the scientific workflow.** Only after both mechanics runs pass, prepare
   real samples/reagents, select `connector_test_mode=false`, and keep the
   two-column default.
11. **Record and restore.** Save the protocol analysis, run log, connector log,
    robot software version, connector commit, custom labware JSON hashes, and
    calibration identifiers. Switch back to stock Opentrons mode if the shared
    connector runtime is not meant to remain active.

## Offline commands

```sh
# Bounded no-motion robot readiness (after connector mode starts)
uv run python scripts/preflight_flex.py <robot-ip>
# Exact gate using the checked-in ASMS labware bundle (must print READY)
uv run python scripts/validate_asms_protocol.py

# Command-logic evidence with explicit non-production substitutes
uv run python scripts/validate_asms_protocol.py --shadow

# Workflow-specific unit and simulator contract
uv run pytest tests/protocols/test_asms_protocol.py -v

# Full connector HTTP + SiLA stack when robot_server is installed
uv run pytest tests/integration --with-http-server -v

# Entire offline repository suite
uv run pytest -q
```
