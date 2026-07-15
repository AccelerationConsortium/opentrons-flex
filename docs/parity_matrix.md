# OT HTTP API ↔ SiLA2 connector — feature parity matrix

**Source of truth:** [`parity_matrix.json`](./parity_matrix.json). This document is
the human-readable rendering; `tests/integration/http_api/test_parity_matrix.py`
validates the JSON and (under `--with-http-server`) cross-checks the HTTP paths
against the live robot-server OpenAPI schema so the matrix cannot silently drift.

## How to read this

The connector re-exposes the entire opentrons robot-server HTTP route shape
(same FastAPI app, backed by our shared `HardwareProxy`) at
`http://<robot-ip>:31950`. When durable labware plans are configured, raw HTTP
gripper/extension and gripper-axis actuation is rejected so it cannot bypass the
labware ledger. This matrix records which HTTP functions *also* have a first-class
**SiLA2** equivalent, and where parity is not yet confirmed.

`sila_support`:

- **supported** — a SiLA2 equivalent exists and is exercised against the simulator.
- **unclear** — a SiLA2 path exists but full parity needs hardware confirmation.
- **unsupported** — intentionally HTTP-only, no SiLA2 equivalent by design
  (server settings, protocol/run engine, identify).

## Matrix

| Group | HTTP | SiLA feature | SiLA element | Support | Notes |
|-------|------|--------------|--------------|---------|-------|
| Health | `GET /health` | MotionControlFeature | IsSimulating (+ server metadata) | ✅ supported | Full field parity (api_version, fw) is HTTP-only |
| Instruments | `GET /pipettes` | PipetteFeature | GetAttachedPipettes | ✅ supported | Per-mount attached pipette models |
| Modules | `GET /modules` | HeaterShaker/Thermocycler/Temperature/AbsorbanceReader/FlexStacker | dynamic registration on attach | ✅ supported | Attached modules register SiLA features at startup |
| Lights | `GET /robot/lights` | MotionControlFeature | Lights (property) | ✅ supported | Status-bar + deck state |
| Lights | `POST /robot/lights` | MotionControlFeature | SetLights | ✅ supported | Toggle lights |
| Motion | `POST /robot/home` | MotionControlFeature | Home / HomeMount | ✅ supported | Observable + cancellable |
| Motion | `POST /robot/move` | MotionControlFeature | MoveTo / MoveRelative | ❓ unclear | SiLA is per-mount deck coords; confirm parity on hardware |
| Motion | `GET /robot/positions` | MotionControlFeature | GetPosition | ✅ supported | HTTP variant returns preset named positions (deprecated) |
| Safety | `GET /robot/control/estopStatus` | MotionControlFeature | **MachineStatus** | ❓ unclear | E-stop/door; drives the Pitfall #2 post-move error-state check |
| Liquid handling | *(SiLA-only)* | MotionControlFeature | Aspirate / Dispense / BlowOut / PrepareForAspirate | ✅ supported | No low-level HTTP aspirate/dispense (HTTP uses protocol engine) |
| Advanced liquid handling | *(SiLA-only)* | LiquidHandlingController | Mix / TouchTip / ProbeLiquidLevel / tracked moves / Transfer / verified liquid classes | ✅ supported | Atomic workflows under the shared hardware lock |
| Tip handling | *(SiLA-only)* | TipController | PickUpTip / DropTip / GetTipPresence | ✅ supported | Sensor-verified atomic move and tip actuation |
| Gripper | *(SiLA-only)* | GripperFeature | Grip / Ungrip / HomeJaw / Status / JawWidth | ✅ supported | HTTP exposes gripper only via protocol engine |
| Labware movement | *(SiLA-only)* | LabwareMovementController | MoveLabware / MoveLid / AvailablePlans / DeckState | ✅ supported | Locally allowlisted plans, durable identity occupancy, module-state, official waypoints, pickup-width, fail-closed recovery, and raw-gripper bypass prevention |
| Calibration | *(SiLA-only)* | CalibrationFeature | CalibratePipette / CalibrateGripperJaw / CalibrateDeck | ❓ unclear | Probe-based; confirm on hardware with probe attached |
| Server settings | `GET /settings` | — | — | ⛔ unsupported | Server config is HTTP-only per AGENTS.md |
| Identify | `POST /identify` | — | — | ⛔ unsupported | Operator equivalent on SiLA is SetLights |
| Protocol engine | `POST /runs` | — | — | ⛔ unsupported | Connector exposes primitives, not the run engine |
| Protocol engine | `POST /protocols` | — | — | ⛔ unsupported | Protocol upload/management is HTTP-only by design |

## Maintenance

When adding or changing a SiLA feature that maps to an HTTP function, update
`parity_matrix.json` and this table together. Run the cross-check with the local
HTTP stack:

```sh
uv run pytest tests/integration/http_api/test_parity_matrix.py --with-http-server -v
```
