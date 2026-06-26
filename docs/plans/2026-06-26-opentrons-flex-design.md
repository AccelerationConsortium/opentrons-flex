# Opentrons Flex SiLA2 Connector — Design

**Date:** 2026-06-26
**Status:** Approved design, implementation starting
**Derived from:** `AccelerationConsortium/opentrons-ot2` (this tree is a copy, adapted for Flex)

## 1. Goal & scope

Build a SiLA2 connector for the **Opentrons Flex** liquid-handling robot, mirroring the
existing OT-2 connector: a gRPC SiLA2 server plus the in-process Opentrons robot-server
HTTP API, both backed by a single shared hardware API instance.

This document covers **Repo A** only: the **in-process `OT3API`** variant that runs on the
Flex's onboard host computer and controls hardware directly (the OT-2-equivalent
architecture). A separate **Repo B** (a SiLA connector acting as an HTTP client of a remote
Flex robot-server) will be designed and built afterward and is out of scope here.

## 2. Key architectural decision: wrap OT3API, do not touch firmware

A colleague raised the question:

> "for the ot3 I'm not sure if we want to modify their fw to have the sila connector run
> directly on the controller or just wrap their comms stuff. The ot3 api might be flexible
> enough that we don't need to go all the way down to the firmware."

**Decision: wrap the OT3 host API. Do NOT modify `ot3-firmware`.**

Rationale — the Flex stack layers as:

```
SiLA2 connector (this repo, Python)        ─┐
opentrons robot-server (Python, HTTP)       ├─ run on the Flex onboard host computer
OT3API  (opentrons.hardware_control.ot3api) ─┘   ← we wrap here
opentrons_hardware  (Python, speaks CAN)
        │  CAN bus
ot3-firmware (C++/C on STM32G4 boards: gantry, head, pipettes, gripper, rear-panel)
```

- `ot3-firmware` runs on STM32G4 microcontrollers over CAN. A gRPC/Python SiLA server
  cannot run there; "SiLA directly on the controller" is infeasible.
- `OT3API` is a `HardwareControlAPI` implementation that already exposes everything we need
  (motion, instruments, lights, gripper, calibration routines). This is the same
  abstraction level the OT-2 connector's in-process path uses (`API` / OT2API).
- Therefore the connector lives on the host and shares one `OT3API` with robot-server via
  the existing `HardwareProxy` lock — no firmware changes, no CAN-level code.

## 3. OT-2 → Flex differences

| Dimension | OT-2 (current) | Flex (this repo) |
|---|---|---|
| Host hardware API | `opentrons.hardware_control.API` (OT2API) | `opentrons.hardware_control.ot3api.OT3API` |
| Motion layer | `OT2MotionController` wrapping `SmoothieDriver` (G-code/serial) | `FlexMotionController` wrapping `HardwareControlAPI` high-level methods |
| Transport | Smoothie over serial `/dev/ttyAMA0` | CAN bus (handled inside OT3API/opentrons_hardware) |
| Axes | X, Y, Z, A, B, C | X, Y, Z_L, Z_R, P_L, P_R (+ gripper Z_G, G) |
| Pipettes | `p20/p300/...` GEN2 | `flex_1channel_1000`, `flex_8channel_1000`, `flex_96channel_1000` |
| Lights / button | `GPIOCharDev` + ALSA tone + physical button | `OT3API.set_lights()/get_lights()`; no button/buzzer |
| Magnetic module | supported (`MagneticModuleController`) | **not supported** (passive Magnetic Block; no controller) — dropped |
| Calibration | Smoothie config writes (steps_per_mm, endstop_debounce, ...) | capacitive-probe auto-calibration routines (`ot3_calibration`) — **redesigned** |
| Gripper | n/a | **new** GripperFeature |
| robot-server | OT-2 mode | Flex mode (`ROBOT_MODEL`/OT-3 env) |
| Onboard OS | glibc 2.25, grpcio compile pain | newer glibc — OT-2 build workarounds likely N/A, must re-verify |

## 4. Module / feature plan

Starting from the OT-2's 7 features:

| Feature | Action for Flex |
|---|---|
| `MotionControlFeature` | **Rewrite internals** at HardwareControlAPI level; endpoints largely preserved. `Axis` enum → Flex axes; `probe` → capacitive probe; lights via `set_lights`; drop ALSA buzzer. |
| `PipetteFeature` | **Port** ~as-is. `get_attached_pipettes` → `cache_instruments()` + `attached_instruments`; Flex pipette model names. |
| `CalibrationFeature` | **Redesign** (see §5). Replace Smoothie config writes with Flex auto-calibration routines. |
| `HeaterShakerFeature` | **Port.** Flex connects modules via USB; `attached_modules` API identical. |
| `ThermocyclerFeature` | **Port.** |
| `TemperatureModuleFeature` | **Port.** |
| `MagneticModuleFeature` | **Delete.** Not supported on Flex. |
| `GripperFeature` | **New** (see §6). |

## 5. CalibrationFeature redesign (Flex)

Flex has no Smoothie config. Calibration is automatic via a capacitive calibration probe,
using `opentrons.hardware_control.ot3_calibration` routines. New surface:

- `calibrate_pipette(mount)` — **observable command** (long-running, progress/status,
  cancellable per AGENTS.md). Runs automatic pipette-offset calibration via capacitive probe.
- `calibrate_gripper()` — observable command; gripper jaw / offset calibration.
- `calibrate_deck()` — observable command; deck auto-calibration.
- `calibration_status(mount)` — unobservable property: enum status (Set constraint, not bool),
  current offset, last-calibrated timestamp.

Defined execution errors:
- `CalibrationProbeNotAttachedError` — operator must attach the calibration probe; message says so.
- `CalibrationFailedError` — include probe coordinates / measured deviation for support.

## 6. GripperFeature (new)

Backed by `OT3API` gripper methods (`grip()`, `ungrip()`, `home_gripper_jaw()`, gripper
instrument query).

- `grip(force)` / `ungrip()` — observable commands (mechanical, cancellable).
- `home_jaw()` — unobservable command.
- `jaw_width` — unobservable property (Unit constraint: mm).
- `gripper_attached` — unobservable property.
- Grip force bounded (~5–20 N) with constraint + Unit (N) annotation.

Defined execution errors: `GripperNotAttachedError`, `GripActionError`.

## 7. Repository layout

Package renamed `unitelabs.opentrons_ot2` → `unitelabs.opentrons_flex`. AC convention:
standalone repo, topic `sila2-connector` + `flex`, common layout (`src/`, `workflows/`,
`tests/`, `deploy.sh`).

```
src/unitelabs/opentrons_flex/
  __init__.py            # create_app + _create_app_with_robot_server (OT3API)
  __main__.py
  io/
    flex_motion.py       # FlexMotionController (HardwareControlAPI level)
    hardware_proxy.py    # unchanged — lock mechanism is API-agnostic
    gripper.py           # NEW
    calibration.py       # NEW (ot3_calibration wrapper)
    heater_shaker.py / thermocycler.py / temperature_module.py   # ported
    modules.py / _module_base.py / _errors.py / _types.py        # ported (drop magnetic)
  features/
    motion_control.py / pipette.py            # rewritten / ported
    calibration.py / gripper.py               # redesigned / new
    heater_shaker.py / thermocycler.py / temperature.py
tests/
  features/ io/ integration/http_api/         # mirror, mock OT3API
```

## 8. robot-server wiring

`_create_app_with_robot_server` keeps the same shape; changes:
- `from opentrons.hardware_control import API` → `OT3API`.
- `API.build_hardware_controller(port=...)` → `OT3API.build_hardware_controller()` (no
  serial port; CAN). `build_hardware_simulator()` for sim.
- Replace `OT_SMOOTHIE_ID` / `RUNNING_ON_PI` Smoothie env with Flex equivalents
  (`ROBOT_MODEL` / OT-3 markers). Verify against robot-server's Flex startup.
- `HardwareProxy` + shared `asyncio.Lock` + app-state pre-population: **unchanged**.
- Module discovery loop: drop `ModuleType.MAGNETIC` factory entry.

## 9. Testing & simulation

- Simulation backend: `OT3API.build_hardware_simulator()` (AGENTS.md mandates a sim mode).
- Unit tests per SiLA endpoint and per controller method, mocking `OT3API`.
- Keep `tests/integration/http_api` pattern for live-robot runs (skipped without hardware).
- Observable calibration/gripper commands tested for progress + cancellation.

## 10. Deployment

- `deploy.sh` / Dockerfiles: adapt service names and paths for the Flex host.
- OT-2 glibc-2.25 / grpcio-from-source workarounds in AGENTS.md are likely unnecessary on
  Flex's newer OS — re-verify the wheel/ABI situation rather than copying the OT-2 dance.

## 11. Out of scope (this repo)

- Repo B (remote HTTP-client SiLA connector) — separate design.
- Protocol/run orchestration beyond the primitive motion/liquid moves the OT-2 connector exposes.
- Liquid-class logic (kept on the client side, as in OT-2).
