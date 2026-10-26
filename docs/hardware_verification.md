# Hardware verification with `flexctl verify`

`flexctl verify <robot-ip> --level <level>` checks a deployed connector and writes
`flex-verify-<robot>-<time>.json`. Levels build on each other; `all` runs every one.
Each step prints PASS, WARN, FAIL, or SKIP. Steps that need a person ask first:
press Enter to continue or `s` to skip. Nothing moves before you confirm the deck
is clear (or pass `--deck-clear`).

| Level | Time | Needs | Steps |
|---|---|---|---|
| `smoke` | ~30 s | robot on | Connector status (not the simulator, E-stop, recovery guidance); HTTP `/health` and `/unitelabs/runtime` (connector-pe); attached pipettes; gripper; every registered module answers its Status. |
| `motion` | ~2 min | clear deck, door closed, hand near E-stop | Home all axes. One 10 cm move at 10 mm/s at the top of travel is interrupted by `EmergencyStop`: the move must end with `OperationHaltedError`, status must become `RECOVERY_REQUIRED`, a further move must be refused with `RecoveryRequiredError`, and `Home` must restore `READY`. Optional: press the physical E-stop and confirm it is reported, then release and re-home. |
| `recovery` | ~2 min | a pipette; SSH | `ReconcileTipState` on each pipette must match the tip sensor and clear the quarantine. Emergency stop, restart the service over SSH, and confirm the re-home gate survived (requires `recovery_state_path`, set in the shipped configs). |
| `modules` | ~3 min | hands at the robot | Unplug a registered module's USB cable: it must show disconnected; plug the same module back in: it must work again without a restart. Optionally plug in a module type that is not registered yet: the connector must restart itself when idle and register its feature. |

Useful options:

- `--unattended` never prompts; steps that need hands are skipped.
- `--deck-clear` answers the deck-clear question in advance.
- `--move-speed 5` slows the interrupted move if your network is slow and the
  move finishes before the stop arrives (the step then reports WARN). Allowed
  range 1-50 mm/s. The test move is bounded (10 cm at the top of travel), so if
  `verify` itself crashes mid-step the move simply finishes; keep a hand near the
  physical E-stop anyway.
- `--level` is cumulative: `motion` runs smoke + motion, `modules` (same as `all`)
  runs every level.
- In connector-unit mode SiLA listens only on the robot's loopback; `verify`
  opens an SSH tunnel automatically.

The older pytest hardware suites (`tests/integration/hardware`, see
[real_flex_test_runbook.md](real_flex_test_runbook.md)) remain available for
module actuation, labware movement, and the full acceptance workflow.
