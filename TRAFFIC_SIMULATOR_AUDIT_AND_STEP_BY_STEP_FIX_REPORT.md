# Traffic Simulator Audit and Step-by-Step Fix Report

This consolidated document combines the original defect audit and ordered
remediation plan with the later post-fix verification audit. Read it in this
order:

1. **Part I — Baseline audit and implementation plan:** Sections 1–8 record the
   original findings `F-01` through `F-18`, their reproduction evidence, the
   required implementation sequence, and the first completion record.
2. **Part II — Post-fix audit and verification:** Records the later independent
   verification, findings `A-01` through `A-05`, their corrections, the expanded
   98-test result, and remaining hardening recommendations.
3. **Separate gridlock incident report:** The later saturated-network incident
   is intentionally kept in the
   [Gridlock Incident Report](TRAFFIC_SIMULATOR_GRIDLOCK_INCIDENT_REPORT.md)
   because it is a distinct unresolved congestion investigation.

The code-inclusive companion is the
[Simulator Guide and Source Documentation](TRAFFIC_SIMULATOR_GUIDE_AND_DOCUMENTATION.md).

Status chronology matters: an item described as open in the baseline evidence
may be listed as fixed in a later completion or post-fix section. The newest
verified disposition takes precedence, while the earlier evidence remains as a
regression target.

## Part I — Baseline Audit and Ordered Remediation

## 1. Objective and non-negotiable scope

This document is the implementation brief for correcting the traffic simulator in:

```text
C:\Users\ascen\python_projects\traffic_simulator
```

The application entry point is `main.py`. The other Python files are imported modules or the telemetry child process.

The coding agent must correct the verified traffic-safety, integration, timing, priority-control, and telemetry defects described below. Do not redesign unrelated UI styling, rename established route IDs, or remove working controls. The 2026-08-25 re-audit records the pre-implementation baseline; Section 8 records the subsequent implementation and verification completed from this plan.

### LLM controls are intentionally placeholders

The AI/LLM controls in `control_panel.py` must remain placeholders for this task:

- The model selector may update only its displayed selection.
- `RUN LLM` must remain a registered no-op.
- The LLM status must remain inactive.
- The placeholder controls must not change signals, vehicles, TSP, DBL, routes, telemetry, or simulation timing.
- Do not add Ollama, network calls, model processes, command files, or policy managers.

### Required implementation discipline

1. Preserve `main.py` as the sole owner of the main simulation loop and vehicle list.
2. Keep Tkinter callbacks short; they should mutate configuration or request state changes, not perform vehicle physics.
3. Keep safety decisions deterministic and inside `SignalController` and `Vehicle`/`Bus` behavior.
4. Use Node A `x=300`, Node B `x=700`, horizontal center `y=300`, lane width `22`, road width `132`, and stop offset `10` unless geometry is deliberately centralized without changing behavior.
5. Use the project interpreter for all validation:

   ```powershell
   .\myenv\Scripts\python.exe
   ```

6. Do not claim completion from compilation alone. Every acceptance test in this document must pass.

## 2. Current architecture and communication contracts

```text
control_panel.py
  global_config
  approach_configs
  bus_routes_config
          |
          v
main.py --------------------------------------------------+
  owns vehicles, fixed-step loop, spawning, dispatch      |
          |                                               |
          +--> signal_controller.py                       |
          |      phase state, TSP, DBL eligibility        |
          |              |                                |
          +--> vehicle.py <--------------------------------+
          |      movement, following, stopping, turns
          |
          +--> canvas_gemini.py
          |      geometry and rendering
          |
          +--> telemetry_exporter.py
                    |
                    v
            traffic_state_telemetry.json
                    |
                    v
            telemetry_dashboard.py
```

### Contracts that currently work and must be preserved

- All non-placeholder control-panel callbacks are registered.
- Route and approach callback factories correctly retain their row keys.
- Pause gates physics, arrivals, signal updates, and dispatch.
- Reset requests are consumed by `main.py`, including while paused.
- Existing buses respond to live TSP and DBL toggle changes.
- Telemetry export uses temporary-file creation followed by `os.replace()`.
- All six routes complete when unobstructed.
- Exact current red/yellow signals stop all four approaches.
- Node coordinates currently agree across modules.

### Weak contracts that must be corrected

- Semantic signal strings are now used, but invalid values still fail open instead of being treated as red.
- DBL does not identify a specific grant, bus, route leg, or controlled boundary and does not request a protected signal phase.
- Vehicle following ignores cross-direction and turning conflicts.
- Geometry is duplicated with literals in several files.
- Dashboard and telemetry paths depend on the caller's working directory.
- Dashboard metric constants disagree with the vehicle model.

## 3. Verified defects and required outcomes

| ID | Severity | Verified defect | Required outcome |
|---|---|---|---|
| F-01 | Critical | DBL stops a lane-2 car ahead of the bus, causing both to stop permanently | DBL must never trap its granted bus behind a vehicle it stopped |
| F-02 | Critical | An NB vehicle can enter an EB spillback queue during valid NS green | A vehicle must not enter an occupied conflict box or blocked downstream path |
| F-03 | High | Left turns teleport approximately 40 px in one update | Turn movement must be continuous and bounded by vehicle speed |
| F-04 | High | A bus unable to merge reaches `x=685` and stops inside Node B | A bus lacking its required lane must stop upstream of the stop bar |
| F-05 | High | A lane-1 straight R2 bus activates lane-2 DBL at Node A | DBL must match the actual bus, node, route leg, movement, and lane |
| F-06 | High | Dashboard child and telemetry paths break outside the repository CWD | All runtime paths must resolve from the source directory |
| F-07 | Medium | Missing or semantic signal state lets a vehicle proceed | Unknown/missing signal state must fail safe; behavior must not depend on RGB |
| F-08 | Medium | Old telemetry is displayed as `LIVE` indefinitely | Dashboard must distinguish LIVE, PAUSED, STALE, and CONNECTING/ERROR |
| F-09 | Medium | One telemetry read/schema error permanently stops polling | Polling must reschedule after every recoverable error |
| F-10 | Medium | A car in another lane blocks bus dispatch | Bus entry clearance must be lane-aware |
| F-11 | Medium | Simulation time depends on Tk callback count, not elapsed time | Physics accumulator must use monotonic elapsed time with bounded catch-up |
| F-12 | Medium | Bus model uses 45 passengers; dashboard uses 40 | Passenger data must have one authoritative source |
| F-13 | Medium | One bus at Node A extends green at Node B | TSP state and requests must be node-specific |
| F-14 | Low | `approaching_buses` includes departed buses with negative distance | Separate active buses from genuinely approaching buses |
| F-15 | Low | Headway OFF freezes rather than resets accumulated time | Choose, implement, and document explicit OFF semantics |
| F-16 | Low | Teaching guide contains outdated embedded source and omits the dashboard | Regenerate documentation only after production fixes pass |
| F-17 | Critical | DBL does not alter signals, while TSP extends whichever phase happens to be green, including a conflicting phase | Every eligible bus request must use a per-node, route-leg-aware interlock that safely terminates conflicts before granting an exclusive priority phase |
| F-18 | High | The three-test suite reports `xxX`; accepted failures and stale mocks do not validate current semantic signals or production callback signatures | Replace stale `xfail` tests with passing assertions for current interfaces, all six routes, priority transitions, and collision safety |

### Reproduction evidence

#### F-01: DBL deadlock

```text
After 400 frames:
car: x=115.0, speed=0.0
bus: x=73.35, speed=0.0
bumper gap=11.65
DBL active=true
```

#### F-02: spillback collision

```text
phase=NS_GREEN / EW_RED
NB vehicle center=(267.0, 280.5)
stopped EB vehicle center=(266.0, 267.0)
result=overlapping rectangles
```

#### F-03: turn teleport

At `max_speed=1.0`:

```text
EB: (285,245) -> (245,244), direction NB
WB: (315,355) -> (355,356), direction SB
NB: (245,315) -> (244,355), direction WB
SB: (355,285) -> (356,245), direction EB
```

#### F-04: blocked merge

```text
Node B intersection bounds: x=634..766
bus stopped at x=685.0, y=256.5, lane_index=1, speed=0.0
```

#### F-05: wrong-lane DBL

```text
route=R2_EB_B_NB
node=300
target turn=STRAIGHT
bus lane=1
DBL eligible=true
lane-2 car stopped=true
```

#### F-17: DBL/TSP can preserve or extend the conflicting phase

Fresh 2026-08-25 probes against the current production interfaces produced:

```text
TSP request: route=R2_EB_B_NB, target node=300, approach=EB
before phase=NS_GREEN
after one update phase=NS_GREEN, tsp_extension_timer=1
Node A signals: EB=RED, WB=RED, NB=GREEN, SB=GREEN
Node B signals: identical to Node A

DBL request: route=R1_EB_A_NB, target node=300, approach=EB
DBL eligible=true and Node A DBL lamp EB=true
before signals: EB=RED, WB=RED, NB=GREEN, SB=GREEN
after signals:  EB=RED, WB=RED, NB=GREEN, SB=GREEN

R2 straight leg at Node A: lane=1, DBL eligible=false
```

This confirms three separate defects:

1. TSP is direction-blind. An EB request can lengthen a conflicting NS green.
2. TSP is global. A request for Node A changes the shared timer used by Node B.
3. DBL is only a lane-state predicate/lamp. It has no signal request or interlock, and its hardcoded `lane_index == 2` rejects valid straight route legs.

#### F-18: the current test result is not a passing safety gate

```text
pytest -q result: xxX
2 expected failures, 1 unexpected pass
```

The F-01 mock replaces `is_bus_dbl_eligible` with a two-argument lambda while production can call it with three arguments. The F-02 test supplies RGB tuples even though production now consumes semantic strings. F-05 unexpectedly passes only because DBL was hardcoded to lane 2; that does not validate the required per-route-leg behavior. There are no tests for the six route priority transitions, node isolation, yellow/all-red clearance, rear-clear release, or simultaneous requests.

### 2026-08-25 pre-implementation re-audit status

| Finding | Current status | Fresh evidence or source result |
|---|---|---|
| F-01 | Fixed in the focused scenario | After 400 frames the car cleared to `x=515`; the bus remained mobile and there was no overlap or deadlock |
| F-02 | Open, Critical | Three deterministic stress seeds produced 54, 42, and 46 cross-direction overlap ticks; the first overlaps involved an EW through vehicle and a turning bus |
| F-03 | Fixed in focused four-direction probes | EB, WB, NB, and SB left turns changed direction with maximum per-update displacement `1.0 px` |
| F-04 | Focused symptom fixed; retain regression test | A blocked wrong-lane bus now stops upstream rather than at the earlier in-box position; it still needs an explicit merge-hold state and release test |
| F-05 | Superseded by F-17 | Hardcoding lane 2 blocks valid straight legs such as R2 at Node A; authorization must use the active route leg |
| F-06 | Open | `main.py` still launches `telemetry_dashboard.py` and writes telemetry using caller-relative paths |
| F-07 | Partially fixed | Known semantic states work, but an `INVALID` EB state let a vehicle pass the stop bar at speed `1.0` |
| F-08 | Open | Any readable JSON is still labeled `LIVE`; timestamp age is not checked |
| F-09 | Open | Poll rescheduling is not protected by `finally`; an unexpected read/schema/UI error can stop the callback chain |
| F-10 | Open | General spawning is lane-aware, but bus entry clearance checks direction and longitudinal distance only |
| F-11 | Open | The accumulator still adds a constant `1/60 * sim_speed` per Tk callback rather than monotonic elapsed time |
| F-12 | Open | `Bus.passengers=45`; dashboard estimates each bus as 40 passengers |
| F-13 | Open and prerequisite for F-17 | The controller still owns one global phase, timer, and TSP extension for both nodes |
| F-14 | Open | Exporter still places every active bus in `approaching_buses`, including negative stop-bar distances |
| F-15 | Open | A zero headway neither increments nor explicitly resets its retained counter |
| F-16 | Open | The guide omits `telemetry_dashboard.py` and embeds older controller source |
| F-17 | Open, Critical | DBL leaves a conflicting NS green unchanged; an EB TSP request increments the NS extension timer |
| F-18 | Open, High | Current suite is only three `xfail`-marked tests and reports `xxX` |

Current baseline checks:

```text
py_compile: PASS for all seven production modules
pytest -q: xxX (not accepted as a release pass)
static callback reference scan: every local control callback/factory is registered or invoked
LLM model selector: UI-only callback preserved
RUN LLM: deliberate no-op preserved
```

Fresh deterministic stress evidence:

```text
seed 0: 54 cross-direction overlap ticks
  first: phase EW_GREEN, EB through Vehicle at (257.5,267.0)
         versus R1 turning Bus, now NB, at (245.0,244.5)
seed 1: 42 cross-direction overlap ticks
  first: phase EW_GREEN, WB through Vehicle at (742.2,333.0)
         versus R5 turning Bus, now SB, at (755.0,355.8)
seed 2: 46 cross-direction overlap ticks
  first: phase EW_GREEN, WB through Vehicle at (343.0,333.0)
         versus R4 turning Bus, now SB, at (355.0,355.3)
```

These are rectangle overlaps after movement updates, not merely two vehicles occupying a broad intersection region. The continuous-turn change fixed teleportation but did not provide movement-level conflict protection between an EW through vehicle and an EW-origin left-turn bus.

## 4. Step-by-step implementation plan

Complete these steps in order. Later steps depend on contracts introduced by earlier steps.

### Step 1 — Add a focused regression-test suite before changing behavior

Create a test directory and encode the verified failures as deterministic tests. Tests must not require visible GUI windows.

Minimum test groups:

```text
tests/
  test_signal_contract.py
  test_intersection_safety.py
  test_turning.py
  test_dbl.py
  test_bus_routes.py
  test_dispatch.py
  test_timing.py
  test_telemetry.py
  test_callbacks.py
```

Required tests to reproduce open findings and lock in confirmed fixes:

- Preserve the fixed DBL car-ahead case: the car and bus must both clear without overlap or deadlock.
- Prove R2 lane-1 at Node A requests only its actual straight route leg and protected phase.
- Reproduce and then eliminate turning-bus/through-vehicle cross-direction overlap.
- Preserve bounded per-frame displacement for all four left-turn directions.
- Preserve upstream holding for a lane-blocked R2 bus, then prove it resumes when a safe gap opens.
- Invalid, missing, `None`, and malformed semantic signal values must fail safe as red.
- A different-lane car blocks bus entry.
- A stale telemetry snapshot is labeled live.
- A malformed telemetry schema prevents the next poll callback.
- Passenger total is inconsistent.
- Launch paths are not source-relative.
- An EB TSP request during NS green extends the conflicting NS phase.
- A DBL request changes its lamp but does not request a safe signal phase.
- Node A and Node B share one phase and extension timer.
- Each row of the Step 13 six-route matrix must transition through yellow/all-red and end with only the requested approach green.

Tests that reproduce current failures may initially be marked expected failures, but all must become ordinary passing tests before completion. Do not weaken assertions merely to make the suite green.

### Step 2 — Centralize geometry and introduce semantic signal states

Files:

- `canvas_gemini.py`
- `signal_controller.py`
- `vehicle.py`
- `main.py`
- `telemetry_exporter.py`

Required changes:

1. Establish one authoritative geometry source for:

   ```python
   INT_X = [300, 700]
   H_Y = 300
   LANE = 22
   ROAD_W = 132
   STOP = 10
   ```

2. Remove independent `[300, 700]`, `300`, and `700` decision literals where a shared value can be used safely.
3. Define semantic signal states, preferably constants or an enum:

   ```python
   RED = "RED"
   YELLOW = "YELLOW"
   GREEN = "GREEN"
   ```

4. `SignalController.get_all_signals()` must return semantic states.
5. `Vehicle.update()` must stop on red or yellow according to the existing stop policy.
6. Missing nodes, missing directions, `None`, or unknown signal values must be treated as red.
7. `canvas_gemini.py` alone must map semantic states to display colors.
8. Remove the conflicting green definitions `(50, 220, 50)` versus `(50, 200, 50)`.

Acceptance tests:

- EB, WB, NB, and SB stop on semantic red and yellow.
- All four proceed on green when otherwise safe.
- Missing and invalid signal data stop safely without exceptions.
- All six signal phases render correctly.
- Node A and Node B maps contain all four directions.

### Step 3 — Replace coordinate teleportation with explicit route-leg and turn state

Files:

- `vehicle.py`
- Possibly `main.py` for initialization metadata
- `telemetry_exporter.py` for state visibility

Required changes:

1. Add explicit state for turn progression, for example:

   ```text
   APPROACHING -> TURNING -> DEPARTING -> COMPLETE
   ```

2. Track the current route leg/node explicitly. Do not infer route completion solely from a 10-pixel coordinate threshold.
3. Implement a continuous turn trajectory. An arc, interpolated curve, or bounded piecewise path is acceptable.
4. Per-frame displacement must not exceed the vehicle's allowed speed, except for a documented negligible numerical tolerance.
5. Preserve the vehicle as a leader/conflict participant throughout the turn. Changing direction must not make following vehicles forget it immediately.
6. Mark a node passed only after the rear bumper clears the conflict area.
7. Preserve the current final directions:

   ```text
   R1 -> NB
   R2 -> NB
   R3 -> EB
   R4 -> SB
   R5 -> SB
   R6 -> WB
   ```

Acceptance tests:

- All four left-turn directions are continuous.
- Maximum displacement per update is bounded.
- Two following left-turn vehicles retain a safe physical gap.
- No overlap occurs during direction transition.
- All six bus routes complete without skipping or repeating route legs.
- Node A is recorded as cleared on R2/R3 straight-through movement before Node B becomes the active leg.

### Step 4 — Add conflict-box and downstream spillback protection

Files:

- `vehicle.py`
- `signal_controller.py`
- `main.py` if a shared occupancy snapshot is needed

Required changes:

1. Define a conflict box for each intersection from shared geometry.
2. Before a vehicle crosses a stop bar, verify:

   - Its movement is permitted by the signal.
   - The relevant conflicting paths are clear.
   - Sufficient downstream receiving space exists for the whole vehicle.

3. A green indication must not authorize entry into a blocked intersection.
4. Track turning vehicles as occupying their complete movement path.
5. Before switching to a conflicting phase, ensure the conflict box is clear or maintain all-red until it clears.
6. Avoid updating safety based on order-dependent partially updated positions. Prefer a snapshot/decision stage followed by a movement stage.

Acceptance tests:

- Reproduce the audited EB spillback setup; NB must remain upstream without overlap.
- No cross-direction rectangle overlap during long seeded runs.
- A vehicle already inside an intersection may clear it safely.
- A downstream queue does not propagate vehicles into the conflict box.
- All-red clearance remains present between conflicting green phases.

### Step 5 — Redesign DBL as a specific, conflict-safe grant

Files:

- `signal_controller.py`
- `vehicle.py`
- `control_panel.py` only if configuration metadata must be extended
- `canvas_gemini.py`
- `telemetry_exporter.py`

Required DBL grant fields:

```text
bus_id
route_id
route_leg_index
node_x
direction
movement
lane_index
state
```

Suggested states:

```text
INACTIVE
REQUESTED
ACTIVE
CLEARING
EXPIRED
DENIED
```

Required behavior:

1. Require an actual `Bus`, not merely `is_heavy=True`.
2. Verify bus ID, route, active route leg, target node, direction, intended movement, and lane.
3. A lane-1 straight bus must not activate lane-2 DBL.
4. Vehicles already ahead of the granted bus must be allowed to discharge.
5. Do not stop vehicles in place in front of the priority bus.
6. Prevent new general traffic from entering the controlled lane behind the boundary while the grant is active.
7. If lane evacuation is modeled, require a safe adjacent-lane gap; do not teleport laterally.
8. End the grant after the bus's rear bumper clears the controlled area.
9. DBL must submit a request to the shared priority arbiter. It must never directly override a primary signal; the arbiter must implement and test the fully interlocked exclusive bus phase required by Step 13.
10. Render only the node, approach, and lane actually receiving the grant.

Acceptance tests:

- The audited car-ahead scenario lets both the car and bus eventually clear.
- A car behind the control boundary is prevented from entering the reserved lane.
- R2 in lane 1 at Node A can request priority for its actual straight route leg but cannot reserve or control an unrelated lane-2 movement.
- A heavy non-bus cannot receive DBL eligibility even if given a route-like attribute.
- A DBL grant at Node A does not activate Node B.
- Disabling DBL live prevents new grants and lets an active movement clear safely.
- DBL state, bus ID, lane, node, and lifecycle are visible in telemetry.

### Step 6 — Hold buses upstream when required lanes are unavailable

Files:

- `vehicle.py`

Required changes:

1. Define a merge decision point upstream of each stop bar.
2. Determine the required lane from the active route leg.
3. Check the full lateral transition path, not only the final lane center.
4. If a safe merge cannot finish before the stop bar, stop at the upstream merge hold point.
5. Never wait for the required lane while occupying the intersection.
6. Resume merging when a safe gap exists.

Acceptance tests:

- With lane 2 continuously occupied, R2 stops upstream of Node B's stop bar.
- The bus never reaches the intersection bounds while in the wrong lane.
- When a safe gap opens, the bus merges, turns, and completes its route.
- The merge does not overlap a vehicle in either the source or target lane.

### Step 7 — Make signals and TSP node-specific

Files:

- `signal_controller.py`
- `main.py`
- `canvas_gemini.py`
- `telemetry_exporter.py`

Required changes:

1. Store phase, phase timer, clearance, and TSP extension per node.
2. A request at Node A must not automatically extend Node B.
3. Associate TSP with a specific bus and active route leg.
4. Define explicit request, eligible, granted, active, clearing, expired, and denied states.
5. Preserve conflict safety and mandatory yellow/all-red intervals.
6. Implement early/priority green through the Step 13 interlock. A request must never extend a phase that is incompatible with the bus approach and movement.
7. Hold a grant until the rear bumper clears or until a safe timeout/denial transition.

Acceptance tests:

- A bus at Node A affects only Node A.
- A bus at Node B affects only Node B.
- Simultaneous requests are resolved deterministically.
- Maximum extension is enforced separately at each node.
- TSP never skips yellow or all-red clearance.
- No TSP state permits a conflicting movement.

### Step 8 — Correct lane-aware bus dispatch and headway semantics

Files:

- `main.py`
- `control_panel.py`

Required changes:

1. Bus entry-clearance checks must include the intended lane coordinate, matching the general spawner's lateral tolerance.
2. A vehicle in another lane must not block dispatch.
3. A vehicle in the same lane inside the required safe distance must block dispatch.
4. Preserve one-shot manual dispatch retry when the correct lane is blocked.
5. Decide and document what headway `OFF` means:

   - Recommended: reset accumulated headway when set to zero, so re-enabling starts a fresh interval.
   - Alternative: preserve elapsed time, but label the control as paused rather than off.

6. Define whether manual dispatch resets automatic headway. Recommended: reset it so an automatic bus cannot immediately follow a manual bus.

Acceptance tests:

- Different-lane entrance vehicle does not block the bus.
- Same-lane entrance vehicle blocks and sets one retry.
- All six simultaneous manual requests eventually dispatch once each.
- Turning routes enter the correct initial lane.
- OFF and manual-dispatch headway behavior matches the documented rule.

### Step 9 — Use real elapsed time with bounded fixed-step catch-up

Files:

- `main.py`

Required approach:

1. Use `time.perf_counter()` or another monotonic clock.
2. On each GUI callback, compute elapsed real time since the previous callback.
3. Clamp an excessive elapsed interval to prevent a spiral of death after window dragging or debugger pauses.
4. Add `elapsed * sim_speed` to the physics accumulator.
5. Consume the accumulator in `1/60` fixed physics steps.
6. Set a maximum number of catch-up steps per GUI callback.
7. When paused, do not accumulate a backlog. Reset the previous-time reference appropriately on resume.

Acceptance tests with a fake clock:

- 1.0 seconds at 1x produces 60 physics steps within tolerance.
- 1.0 seconds at 0.5x produces 30 steps.
- 1.0 seconds at 2.5x produces 150 steps over bounded callbacks.
- A delayed callback catches up only to the configured safe limit.
- Pause followed by resume does not cause a large burst of steps.
- Signal time, headways, spawners, and frame count advance from physics steps only.

### Step 10 — Make runtime paths independent of the caller's CWD

Files:

- `main.py`
- `telemetry_dashboard.py`

Required changes:

1. Derive the application directory from `Path(__file__).resolve().parent`.
2. Build the telemetry path from that directory.
3. Launch the dashboard using its absolute script path.
4. Pass or share the exact telemetry path so exporter and dashboard cannot diverge.
5. Check whether the child process exits immediately and report the failure.
6. On shutdown, terminate and wait for the child with a timeout; avoid leaving a process behind.

Acceptance tests:

- Launch from the repository directory.
- Launch `main.py` by absolute path from its parent directory.
- Launch from a temporary working directory.
- All cases use the same telemetry file beside the application.
- Dashboard process starts in all cases.
- Main shutdown terminates the dashboard process.

### Step 11 — Harden telemetry schema and dashboard polling

Files:

- `telemetry_exporter.py`
- `telemetry_dashboard.py`
- `vehicle.py` for authoritative passenger data

Required telemetry changes:

1. Add a schema/version identifier.
2. Export actual passenger totals from active bus objects.
3. Separate `active_buses` from genuinely `approaching_buses`, or rename the existing collection accurately.
4. Do not classify a bus with a negative stop-bar distance and cleared final node as approaching.
5. Export per-node signal state after Step 7.
6. Export DBL/TSP request/grant lifecycle after Steps 5 and 7.
7. Keep atomic replacement and visible exporter warnings.
8. Rename `export_interval_frames` if it remains based on render calls, or schedule export from actual physics-frame/time criteria.

Required dashboard changes:

1. Validate top-level and nested payload types before using them.
2. Catch recoverable `OSError`, Unicode, JSON, and schema errors.
3. Schedule the next poll in `finally` so one failure cannot terminate polling.
4. Use timestamp age and frame progression to distinguish:

   ```text
   CONNECTING
   LIVE
   PAUSED
   STALE
   DATA ERROR
   ```

5. Do not mark old but valid JSON as live.
6. Display the actual passenger total.
7. Bound rolling histories so memory usage remains constant.

Acceptance tests:

- Missing file reschedules polling.
- File deleted between existence check and open reschedules polling.
- Truncated JSON reschedules polling.
- Wrong nested types display DATA ERROR and reschedule polling.
- Fresh advancing data displays LIVE.
- Fresh paused data displays PAUSED.
- Old or non-advancing data displays STALE.
- Passenger display matches `Bus.passengers` totals.
- Departed buses are absent from `approaching_buses`.

### Step 12 — Clean low-risk inconsistencies after behavioral fixes

Files as applicable:

- Remove the unused `control_panel` import from `vehicle.py` if it remains unused.
- Remove or implement `Vehicle.is_turning`; do not retain misleading dead state.
- Remove unused `canvas_gemini.GREEN` after semantic color mapping is centralized.
- Remove unused dispatch-time copies of `tsp_enabled` and `dbl_enabled` from `route_info`, or label them explicitly as immutable dispatch snapshots.
- Trim unused heavy dependencies from `requirements.txt` unless they are intentionally retained for a documented optional analysis environment.
- Initialize control-panel widget appearance from current shared state so recreating the dashboard cannot show false values.
- Correct DBL pause/resume flashing with monotonic accumulated animation time if phase-preserving flashing is required.

Acceptance tests:

- Static reference scan reports no unintended dead fields or imports.
- Recreating the control window reflects retained pause, speed, route, and approach states.
- Pausing and resuming DBL flashing preserves phase according to its documented rule.

### Step 13 — Implement conflict-safe route-leg DBL/TSP signal priority

Files:

- `signal_controller.py`
- `vehicle.py`
- `control_panel.py` only for declarative route-leg metadata
- `main.py`
- `canvas_gemini.py`
- `telemetry_exporter.py`
- `telemetry_dashboard.py`
- `tests/`

#### Required interpretation

Turning on a DBL or TSP toggle must not switch a signal when no eligible bus exists. When an eligible bus approaches a route leg, DBL and/or TSP must create a priority request. DBL controls access to the bus lane; TSP controls signal priority. Both submit to one safety arbiter per node. Neither feature may write signal colors directly.

The safest contract for the current approach-level signals is an **exclusive originating-approach bus phase**. The priority approach may be green; all other approaches at that node must be red. This is necessary because the simulator does not have separate protected-turn arrows: leaving the opposite approach green would also release its left turns.

#### Mandatory per-node state machine

```text
NORMAL
  -> REQUESTED
  -> CONFLICT_YELLOW
  -> ALL_RED_CLEARANCE
  -> PRIORITY_ACTIVE
  -> PRIORITY_CLEARING
  -> RECOVERY_ALL_RED
  -> NORMAL
```

Rules:

1. Store this state independently for Node A (`300`) and Node B (`700`).
2. `REQUESTED` records the bus and active route leg but does not instantly change green to red.
3. If a conflicting approach is green, show its normal yellow for the configured minimum time.
4. Follow yellow with the configured all-red minimum.
5. Do not enter `PRIORITY_ACTIVE` until the minimum clearance has elapsed **and** the node conflict box is clear of conflicting vehicles.
6. During `PRIORITY_ACTIVE`, only the bus's originating approach is green. Every other approach at that node is red.
7. Hold the grant until the priority bus's rear bumper clears the conflict area. Do not release merely because its front crosses a point.
8. Use a recovery all-red before returning to normal coordination.
9. Never skip, shorten, or run yellow/all-red backward when a request arrives near a normal phase boundary.
10. If the bus disappears, the route/feature is disabled, or a safe maximum wait expires, cancel or deny through an all-red-safe transition; never strand a priority green.
11. One node's request must not mutate the other node's phase, timer, clearance, or queue.
12. For simultaneous incompatible requests at one node, use a deterministic queue ordered by request time and then bus ID. Add a bounded-wait/starvation rule. Do not grant both.
13. Treat compatible concurrent grants as unsupported until a formal movement conflict matrix and tests prove them safe; default to one exclusive grant per node.

#### Required grant identity

Each request/grant must expose at least:

```text
request_id
bus_id
route_id
route_leg_index
node_x
originating_approach
movement                 # STRAIGHT or LEFT
entry_lane
exit_direction
conflicting_approaches
requested_at_frame
state
expires_at_frame
denial_or_cancel_reason
```

Do not infer the active route leg only from the bus's current direction after a turn. Give `Bus` an explicit ordered leg index or equivalent route-progress identity.

#### Six-route priority matrix

`GREEN` below means the exclusive protected priority approach. Every approach listed under `Forced RED` must remain red for the whole active grant.

| Route | Active route leg | Protected priority state | Forced RED at that node |
|---|---|---|---|
| `R1_EB_A_NB` | Node A: EB left to NB | Node A `EB=GREEN` | Node A `WB`, `NB`, `SB` |
| `R2_EB_B_NB` | Node A: EB straight | Node A `EB=GREEN` | Node A `WB`, `NB`, `SB` |
| `R2_EB_B_NB` | Node B: EB left to NB | Node B `EB=GREEN` | Node B `WB`, `NB`, `SB` |
| `R3_EB_ONLY` | Node A: EB straight | Node A `EB=GREEN` | Node A `WB`, `NB`, `SB` |
| `R3_EB_ONLY` | Node B: EB straight | Node B `EB=GREEN` | Node B `WB`, `NB`, `SB` |
| `R4_WB_A_SB` | Node B: WB straight | Node B `WB=GREEN` | Node B `EB`, `NB`, `SB` |
| `R4_WB_A_SB` | Node A: WB left to SB | Node A `WB=GREEN` | Node A `EB`, `NB`, `SB` |
| `R5_WB_B_SB` | Node B: WB left to SB | Node B `WB=GREEN` | Node B `EB`, `NB`, `SB` |
| `R6_WB_ONLY` | Node B: WB straight | Node B `WB=GREEN` | Node B `EB`, `NB`, `SB` |
| `R6_WB_ONLY` | Node A: WB straight | Node A `WB=GREEN` | Node A `EB`, `NB`, `SB` |

This matrix directly covers the requested example: when an R2 EB bus requests its Node A leg while Node A NB/SB is green, Node A NB **and** SB must first receive yellow, then all-red; only after clearance may Node A EB receive the exclusive green. Node B must remain unchanged until that same bus approaches its Node B leg and submits a separate request.

#### DBL lane rule

Remove the global `lane_index == 2` assumption. The required lane is derived from the active route leg:

- straight legs use their configured through/bus lane (currently lane 1 for the six bus routes);
- left-turn legs use the configured left-turn/bus lane (currently lane 2);
- DBL reserves only that node, approach, lane, and bus grant;
- a bus may not receive a Node B grant while it is still on or clearing its Node A leg.

#### Acceptance tests

For every row in the six-route matrix:

1. Start from each of the six normal signal phases, including conflicting green and yellow.
2. Submit an eligible DBL-only request, TSP-only request, and combined DBL+TSP request.
3. Assert no instantaneous conflicting green, no skipped yellow, and the full all-red minimum.
4. Seed a vehicle inside the conflict box and assert priority waits until its rear clears.
5. Assert the protected approach is the only green during the grant.
6. Assert the other node's state is byte-for-byte unchanged.
7. Assert the grant remains until the bus rear clears, then recovers through all-red.
8. Test live disable, bus removal, timeout, two conflicting buses, and deterministic queue order.
9. Run multi-seed traffic with DBL/TSP active on all six routes and assert zero right-angle or turning/through overlaps.

Telemetry and rendering must display the request state, grant identity, node, active route leg, protected approach, forced-red approaches, wait duration, and cancel/deny reason. A boolean `tsp_active_triggered` or flashing DBL lamp alone is not sufficient evidence of a safe grant.

### Step 14 — Refresh the coding-LLM documentation last

File:

- `TRAFFIC_SIMULATOR_GUIDE_AND_DOCUMENTATION.md`

Do this only after all production changes and tests pass.

Required changes:

1. Include `telemetry_dashboard.py`.
2. Replace every embedded source block with the complete final source.
3. Document semantic signal contracts.
4. Document explicit route-leg and turning state.
5. Document conflict-box behavior.
6. Document DBL and TSP grant lifecycles.
7. Document telemetry schema and status freshness rules.
8. Retain the statement that the LLM controls are placeholders.
9. Verify every embedded source block matches its file exactly.

## 5. Callback requirements

No callback is currently missing, so preserve the complete callback graph while refactoring.

### Control panel callbacks that must remain operational

- `WM_DELETE_WINDOW -> on_close`
- Pause/resume
- Reset vehicles
- Green-time slider
- Simulation-speed slider
- Manual route dispatch
- Six route activation toggles
- Six headway sliders
- Six TSP toggles
- Six DBL toggles
- Six traffic-source activation toggles
- Six arrival-model selectors
- Six inflow-rate sliders
- Six straight/left split sliders
- Six heavy-vehicle sliders
- LLM model display selector, UI-only
- `RUN LLM`, deliberate no-op

### Runtime callbacks that must remain operational

- Initial scheduling of `simulation_step()`
- Recurring scheduling of `simulation_step()`
- Pygame close handling
- `atexit` dashboard cleanup
- Initial telemetry poll
- Recurring telemetry poll, including after recoverable errors

### Callback acceptance gate

Use a headless widget harness to invoke every button, scale, and combobox callback. Assert:

- No signature errors
- No late-binding route/approach errors
- Only the intended configuration key changes
- LLM placeholder callbacks do not mutate simulation state
- Reset is consumed once
- Pause prevents physics accumulation
- Closing either application does not leave the telemetry child running

## 6. Final verification gate

The coding agent must provide the command and result for every applicable check.

### Compilation and imports

```powershell
.\myenv\Scripts\python.exe -m py_compile canvas_gemini.py control_panel.py main.py signal_controller.py telemetry_dashboard.py telemetry_exporter.py vehicle.py
```

Import every production module in a clean process.

### Focused deterministic tests

Run the full test suite and report totals:

```powershell
.\myenv\Scripts\python.exe -m pytest -q
```

The suite must cover:

- Four directions on red, yellow, green, missing, and invalid signal states
- Six signal phases at both nodes
- Four continuous left turns
- Following through direction changes
- Conflict-box and spillback behavior
- Six bus routes
- Lane-blocked bus holding
- DBL correct bus/lane/node/leg authorization
- DBL car-ahead clearance
- Heavy non-bus rejection
- Node-specific TSP
- All Step 13 route-leg priority matrix rows from all six starting phases
- DBL-only, TSP-only, and combined requests
- Priority conflict yellow, minimum all-red, occupied-box wait, exclusive green, rear-clear release, and recovery all-red
- Simultaneous conflicting priority requests, deterministic ordering, cancellation, timeout, and live disable
- Manual dispatch and headway behavior
- Pause/resume and reset
- 0.5x, 1x, and 2.5x elapsed-time behavior
- Source-relative launch paths
- Telemetry atomic export and schema
- Dashboard live/paused/stale/error states
- Callback reachability and isolation
- LLM placeholder non-mutation

### Adversarial simulation run

Run multiple deterministic random seeds for a long enough interval to exercise queues and turns. At minimum assert:

- Zero rectangle overlaps
- Zero conflicting conflict-box entries
- Zero vehicles entering blocked downstream space
- Zero red-light entries
- No permanently stationary bus caused by DBL
- DBL/TSP enabled separately and together for all six routes
- Zero conflicting green indications during any priority transition or active grant
- Every priority grant follows yellow and minimum all-red clearance
- No right-angle or turning/through overlap during an active or recovering priority grant
- No bus stopped inside an intersection while waiting for a lane
- Every dispatched bus eventually exits or has a documented congestion reason
- Queue and active-vehicle counts remain internally consistent

### Rendering

Run headless Pygame rendering for:

- All signal phases
- Each node independently
- DBL inactive, requested, active, clearing, and expired
- Conflict-yellow, all-red-clearance, priority-active, priority-clearing, and recovery-all-red
- Every row of the six-route protected-signal matrix
- Paused and running states
- Mixed node-specific TSP/DBL states

### End-to-end launch

Perform a safe launch from:

1. The repository directory
2. The parent directory using an absolute script path
3. A temporary working directory

Verify:

- Control panel starts
- Pygame canvas starts
- Telemetry dashboard starts
- Telemetry updates at the shared absolute path
- Pause and reset work
- Manual bus dispatch works
- TSP/DBL toggles create safe requests only when an eligible bus exists
- All six routes receive the correct node-by-node exclusive protected phase without changing the other node
- LLM controls remain placeholders
- Closing the main application terminates the dashboard child

## 7. Completion report required from the coding agent

The final response must contain:

1. A concise summary of the implemented architecture changes.
2. A file-by-file change list.
3. A mapping from every finding ID `F-01` through `F-18` to:

   ```text
   FIXED
   DOCUMENTED INTENTIONAL BEHAVIOR
   NOT FIXED, with exact reason
   ```

4. Test commands and exact pass/fail totals.
5. Evidence from the adversarial multi-seed run.
6. Confirmation that the LLM controls remain placeholders and do not mutate simulation state.
7. Confirmation that no stale embedded source remains in the teaching guide.
8. Any remaining limitations clearly separated from confirmed defects.

Do not report the project as complete if any critical or high-severity acceptance test is failing.

## 8. Implementation completion record — 2026-08-25

The plan above has now been applied to the production modules while preserving the existing GUI and intentional LLM placeholders.

### Finding disposition

| Finding | Final status | Implemented result |
|---|---|---|
| F-01 | FIXED | DBL identifies the active grant/lane and lets traffic already ahead discharge; regression test proves no car-ahead deadlock |
| F-02 | FIXED | Per-node movement reservations and exclusive left turns prevent perpendicular, opposing-turn, and turning/through overlap; accelerated seeded congestion tests pass |
| F-03 | FIXED | All four left turns use bounded continuous movement; maximum test displacement is no greater than vehicle speed |
| F-04 | FIXED | Buses that cannot reach their required route-leg lane hold upstream of the stop bar |
| F-05 | FIXED | DBL derives lane, node, movement, and leg from the active route leg; R2 Node A correctly uses lane 1 |
| F-06 | FIXED | Dashboard and telemetry paths resolve from `Path(__file__).resolve().parent` |
| F-07 | FIXED | Missing, unknown, RGB, malformed, and non-string signal values fail closed as red in vehicle physics |
| F-08 | FIXED | Dashboard classifies fresh running, fresh paused, stale, connecting, and error states |
| F-09 | FIXED | Telemetry polling reschedules from `finally`, including after unexpected errors |
| F-10 | FIXED | Bus entry clearance checks both longitudinal gap and intended lane coordinate |
| F-11 | FIXED | The loop accumulates bounded monotonic elapsed time multiplied by simulation speed |
| F-12 | FIXED | `Vehicle.passengers`/`Bus.passengers` are authoritative and exporter supplies `passenger_volume` |
| F-13 | FIXED | Node A and Node B own independent phase, timer, queue, clearance, reservation, and priority state |
| F-14 | FIXED | Telemetry separates all `active_buses` from non-negative-distance `approaching_buses` |
| F-15 | FIXED | Headway zero resets retained time; a manual dispatch restarts the automatic clock |
| F-16 | FIXED | The seven-section teaching guide is mechanically regenerated from all seven production files, including the dashboard |
| F-17 | FIXED | DBL/TSP share a per-node route-leg arbiter with conflict yellow, minimum all-red, occupied-box wait, exclusive priority green, rear-clear hold, and recovery all-red |
| F-18 | FIXED | Stale `xfail` tests were replaced with current-interface deterministic, integration, six-route, and adversarial tests |

### Implemented conflict-safe priority contract

All ten route legs in the six-route matrix are tested in DBL-only, TSP-only, and combined modes. Requests starting from all six normal phases are also tested. During an active grant, only the originating approach is green; all other approaches at that node are red. The other node remains in independent normal operation.

The priority lifecycle is now:

```text
NORMAL
  -> CONFLICT_YELLOW
  -> ALL_RED_CLEARANCE
  -> PRIORITY_ACTIVE
  -> PRIORITY_CLEARING
  -> RECOVERY_ALL_RED
  -> NORMAL
```

### Final verification evidence

```text
py_compile: PASS
pytest: 78 passed in 7.89s
seeded accelerated congestion: 2 seeds passed with zero cross-direction rectangle overlaps
isolated route completion: all 6 routes passed with combined DBL/TSP
priority matrix: all 10 route legs passed in DBL-only, TSP-only, and combined modes
headless Pygame rendering: all 6 normal phases rendered at 1000x600
teaching guide: 7 sections, all 7 embedded production sources exact-match current files
LLM selector: UI-only
RUN LLM: intentional no-op
```

The full test suite is the authoritative repeatable gate. Do not remove the movement reservation or priority-transition tests when extending signal behavior.

---

## Part II — Post-Fix Audit and Verification

Audit date: 2026-08-29  
Project: `C:\Users\ascen\python_projects\traffic_simulator`  
Entry point: `main.py`

### II.1 Executive verdict

The current simulator is substantially safer and more internally consistent than the pre-fix checkout. Compilation, imports, 98 committed tests, all six isolated bus routes, all ten DBL/TSP route legs, callback invocation, headless rendering, source-relative paths, atomic telemetry, and production-function congestion tests passed.

No current collision or unsafe priority-phase failure was reproduced. In particular:

- DBL/TSP requests transition through conflict yellow and all-red before an exclusive priority green.
- Node A and Node B have independent phase, timer, request, reservation, and priority state.
- Every active priority grant shows only the originating approach green.
- Normal left turns use exclusive movement reservations until the rear clears.
- The independent production-function stress run completed 2,500 ticks, dispatched 27 buses, granted four priority phases, reached 142 simultaneous vehicles, and produced zero rectangle overlaps.
- The LLM selector and `RUN LLM` remain intentional placeholders and do not control the simulation.

The audit and follow-up observations confirmed five defects. All five were corrected and regression-tested on 2026-08-30:

| ID | Severity | Original finding | Current disposition |
|---|---|---|---|
| A-01 | Medium | North/south vehicles falsely marked the other intersection as passed | **Resolved** — vertical traffic is pinned to one physical node at spawn/turn |
| A-02 | Medium | Expired requests silently renewed and lacked durable outcomes | **Resolved** — stable IDs, attempt counts, eligibility-edge retry, wait age, and terminal history added |
| A-03 | Medium | Pending priority was reported/rendered as active | **Resolved** — pending, active, and clearing states are now distinct |
| A-04 | Medium | Compatible vehicles could wait under green until a long left-turn reservation cleared the entire node | **Resolved** — all-red remains the empty-box gate; same-green release now uses the actually shared corner lane |
| A-05 | Medium | The control panel required awkward repeated drag attempts and forced the canvas underneath it | **Resolved** — normal window stacking restored and simulation work per Tk callback bounded |

There are no open confirmed Critical, High, or Medium defects in the tested current checkout. The remaining items in Section 8 are coverage and hardening recommendations, not reproduced failures.

### II.2 Scope and files audited

Production files read in full or structurally inspected:

- `canvas_gemini.py` — geometry and Pygame rendering
- `control_panel.py` — Tkinter UI, shared configuration, and callbacks
- `main.py` — executable loop, spawning, dispatch, timing, child lifecycle
- `signal_controller.py` — per-node phases, reservations, DBL/TSP arbitration
- `vehicle.py` — vehicle/bus physics, turns, following, spillback, route legs
- `telemetry_exporter.py` — schema and atomic JSON export
- `telemetry_dashboard.py` — freshness, polling, metrics, node diagrams
- `traffic_state_telemetry.json` — current schema-v2 runtime artifact
- `requirements.txt` — runtime dependency declaration
- `tests/` — 98-test regression suite and guide generator

The existing GUI design and intentional LLM placeholders were preserved. The audit was followed by a focused production-code remediation pass, regression-test additions, this report update, and regeneration of the source-synchronized documentation artifact.

### II.3 Current architecture and communication audit

```text
control_panel.py
  global_config / approach_configs / bus_routes_config
                        |
                        v
main.py owns vehicles and monotonic fixed-step execution
       |                |                  |
       v                v                  v
SignalController    Vehicle / Bus     canvas_gemini
per-node phases     route physics     semantic rendering
       |                |
       +--------+-------+
                v
       telemetry_exporter.py
                |
        atomic schema-v2 JSON
                |
                v
       telemetry_dashboard.py
```

Verified communication contracts:

1. `main.py` remains the sole owner of the live vehicle list and simulation stepping.
2. Control-panel callbacks mutate configuration/request flags; they do not run vehicle physics.
3. Signals cross the controller/vehicle/canvas boundary as `RED`, `YELLOW`, and `GREEN` strings.
4. Invalid, missing, RGB, numeric, and malformed signal values fail closed as red in physics.
5. DBL/TSP reads live route toggles rather than stale dispatch-time copies.
6. Each priority request identifies bus, route, route-leg index, node, approach, movement, lane, exit direction, request frame, expiry, and feature type.
7. Telemetry includes independent string-keyed node records for `300` and `700`.
8. Dashboard and telemetry paths resolve from the module directory when imported from an unrelated working directory.
9. Telemetry writes through a temporary file, flushes and fsyncs it, then atomically replaces the destination.

### II.4 Traffic-rule and priority audit

#### Normal traffic rules

Verified:

- Red and yellow stop upstream vehicles.
- Unknown states fail safe.
- Following uses oriented vehicle extents and a minimum gap.
- Spillback blocks entry when downstream storage is unavailable.
- Intersection reservations prevent perpendicular straight conflicts.
- Every left turn is exclusive because the current square-corner path can sweep an adjacent lane or preceding turn path.
- Four-direction left-turn displacement remains bounded by vehicle speed.
- A bus unable to reach its required lane holds upstream.
- Traffic already ahead of a DBL bus can discharge, preventing the historical DBL car-ahead deadlock.

#### Six-route DBL/TSP matrix

All ten physical route legs are covered in DBL-only, TSP-only, and combined modes:

| Route | Node/leg | Required protected state | Audit result |
|---|---|---|---|
| `R1_EB_A_NB` | A, EB left to NB | EB green; WB/NB/SB red | Pass |
| `R2_EB_B_NB` | A, EB straight | EB green; WB/NB/SB red | Pass |
| `R2_EB_B_NB` | B, EB left to NB | EB green; WB/NB/SB red | Pass |
| `R3_EB_ONLY` | A, EB straight | EB green; WB/NB/SB red | Pass |
| `R3_EB_ONLY` | B, EB straight | EB green; WB/NB/SB red | Pass |
| `R4_WB_A_SB` | B, WB straight | WB green; EB/NB/SB red | Pass |
| `R4_WB_A_SB` | A, WB left to SB | WB green; EB/NB/SB red | Pass |
| `R5_WB_B_SB` | B, WB left to SB | WB green; EB/NB/SB red | Pass |
| `R6_WB_ONLY` | B, WB straight | WB green; EB/NB/SB red | Pass |
| `R6_WB_ONLY` | A, WB straight | WB green; EB/NB/SB red | Pass |

Requests from all six normal starting phases were tested. An occupied conflict box holds the request in all-red clearance. A live feature disable before grant enters safe recovery; a disable during an active grant lets the bus clear; removal of the granted bus forces recovery all-red.

### II.5 Findings and verified dispositions

#### A-01 — Vertical vehicles falsely complete the other node — RESOLVED

Severity: Medium  
Status: Resolved and regression-tested  
Files changed: `vehicle.py`, `main.py`, `tests/test_vehicle_safety.py`

Original root cause:

For NB/SB traffic, `get_next_target_node()` selects the nearest unfinished x-coordinate. After a vehicle physically clears its assigned node, that node is added to `passed_nodes`. The only unfinished candidate is then the other node, even though the vehicle's x-coordinate and vertical path have not changed. Because both intersections share horizontal center `y=300`, the generic vertical clearance test immediately marks the remote node as cleared as well.

Original audit reproduction:

```text
Vertical NB car physically at Node A:
tick=396, position=(267.0,223.0), passed_nodes=[300,700]

R1 bus that turned north at Node A:
tick=88, position=(245.0,211.0), direction=NB, passed_nodes=[300,700]
```

Pre-fix impact:

- Current six bus routes terminate after a vertical turn, so this did not create a collision in the tested network.
- `passed_nodes` is nonetheless factually wrong.
- Future vertical bus routes, route telemetry, node-specific statistics, reservation cleanup, or multi-node vertical networks would use corrupted progress.

Implemented correction:

1. Every spawned NB/SB approach receives an explicit assigned node x-coordinate.
2. That node remains the target after clearance instead of switching to the remote intersection.
3. A horizontal vehicle that turns vertical retains the exact turn-node identity.
4. Tests cover A_NB, A_SB, B_NB, B_SB and require exact (not merely subset) route completion for all six routes, including R1, R2, R4, and R5.

Verified acceptance gate:

```text
A vertical vehicle at x≈300 may add 300 but never 700.
A vertical vehicle at x≈700 may add 700 but never 300.
A turned route may add its turn node only.
```

#### A-02 — Priority timeout and fairness state is renewable and non-auditable — RESOLVED

Severity: Medium  
Status: Resolved and regression-tested  
Files changed: `signal_controller.py`, `tests/test_signal_priority.py`

Original root cause:

- `_collect_priority_requests()` constructs a new request before checking whether the key already exists. This increments `_request_sequence` and allocates a discarded request every eligible frame.
- `_priority_update()` filters expired queued requests out of the list.
- On the next collection pass, the still-eligible bus receives a brand-new request with a new `requested_at_frame` and `expires_at_frame`.
- Dropped queued requests are not retained in a terminal event/history collection, so their `REQUEST_TIMEOUT` reason disappears from telemetry.
- Resetting request age weakens the intended bounded-wait/starvation contract.

Original audit reproduction with a deliberately short timeout:

```text
initial active bus=A, queued buses=[B]
after expiry/recollection:
  controller=RECOVERY_ALL_RED for A with REQUEST_TIMEOUT
  B reappears as request PRIORITY_000010, state=REQUESTED,
  with a fresh expiry instead of a terminal timeout record
```

Pre-fix safety impact:

The controller fails closed through recovery all-red, so this audit did not find a collision path. The defect affects fairness, timeout meaning, stable request identity, performance, and telemetry auditability.

Implemented correction:

1. The canonical `(bus_id, node_x, route_leg_index)` key is checked before request construction, so duplicate frames do not consume IDs.
2. Each live queue/active request retains its original ID, request frame, expiry, and wait age.
3. Timeout is a terminal denial. The same eligibility edge is suppressed; retry requires leaving/re-entering eligibility and receives an incremented attempt number and separate request ID.
4. Each node retains the latest 50 completed, denied, or cancelled events with reason, terminal frame, and wait age.
5. FIFO ordering remains based on original request time and bus ID rather than renewable retry age.
6. Node status and telemetry now expose wait age, attempt count, and terminal events.

Verified acceptance gate:

- A duplicate eligible frame does not consume a new request ID.
- A timed-out queue entry does not silently reappear as a first request.
- A waiting bus either receives a grant within the documented bound or has one durable terminal denial event.
- Sustained higher-priority arrivals cannot starve an older request indefinitely.

#### A-03 — Requested priority is reported as active priority — RESOLVED

Severity: Medium  
Status: Resolved and regression-tested  
Files changed: `telemetry_exporter.py`, `telemetry_dashboard.py`, `signal_controller.py`, `canvas_gemini.py`, `tests/test_runtime_and_telemetry.py`

Original root cause:

The exporter sets `tsp_active_triggered` and `dbl_active_triggered` whenever a matching priority request exists, regardless of whether its state is `REQUESTED`, `CONFLICT_YELLOW`, `ALL_RED_CLEARANCE`, `PRIORITY_ACTIVE`, or `PRIORITY_CLEARING`. The dashboard sums those booleans under labels `Active TSP` and `Active DBL`. The canvas DBL boolean also becomes true during pre-grant transition states.

Original audit reproduction:

```text
controller priority_state=CONFLICT_YELLOW
telemetry tsp_active_triggered=true
dashboard Active TSP count=1
```

The bus does not yet have a priority green in this state, so the metric is semantically false.

Implemented correction:

Expose separate fields:

```text
priority_requested
priority_transitioning
priority_granted
priority_clearing
priority_terminal
```

Count `Active TSP/DBL` only for `PRIORITY_ACTIVE` and, if documented, `PRIORITY_CLEARING`. Render requested/transitioning DBL with a distinct visual state rather than the active lamp.

Verified acceptance gate:

- A queued, yellow-transition, or all-red request does not increment Active TSP/DBL.
- The dashboard separately displays pending requests.
- The active count equals the number of controller grants, not the number of interested buses.

#### A-04 — Green traffic over-waits for whole-node clearance — RESOLVED

Severity: Medium  
Status: Resolved and regression-tested  
Files changed: `signal_controller.py`, `tests/test_signal_priority.py`, `tests/test_vehicle_safety.py`

Observed behavior:

A left-turn reservation was treated as conflicting with every other movement until the turner's rear cleared the complete intersection rectangle. Consequently, a compatible through vehicle could remain stopped while its signal was green, even after the turner had cleared the only lane the two paths actually shared.

Implemented correction:

1. Normal all-red phases still refuse to advance to green while any vehicle occupies the intersection box.
2. Priority all-red clearance retains the same empty-box requirement.
3. Perpendicular axes always conflict.
4. Opposing movements on the same green axis use their physically separated paths.
5. A same-approach long left turn protects its adjacent corner lane only until the turner's bounds clear that lane; compatible through traffic can then enter while the turner is still elsewhere in the wider node box.
6. Two left-turners sharing the same approach/path remain serialized only through the shared corner. The follower is released before whole-node clearance once the leader has cleared that corner and ordinary following can maintain separation on the exit lane.

Verified acceptance gate:

- Green begins only after the preceding all-red interval observes an empty box.
- Compatible through traffic starts under green before the entire node becomes empty again.
- An R1 EB→Node A→NB bus can follow another R1 left-turner before the leader clears the entire node, complete its turn, and maintain non-overlap throughout.
- The original long-bus left-turn/through overlap regression remains collision-free.
- Two seeded 3,000-tick congestion runs retain zero cross-direction rectangle overlaps.

#### A-05 — Control-panel dragging and forced canvas stacking — RESOLVED

Severity: Medium  
Status: Resolved and regression-tested  
Files changed: `control_panel.py`, `main.py`, `tests/test_runtime_and_telemetry.py`

Root causes:

1. The Tk control panel explicitly enabled `-topmost=True`, forcing the Pygame canvas below it whenever the two windows overlapped.
2. The fixed-step catch-up loop could execute as many as 45 simulation ticks in one Tk callback at 3× speed after a 250 ms scheduling delay. That delayed the return to the native window event loop and made dragging/focus changes feel unresponsive.

Implemented correction:

1. The control panel now explicitly uses normal desktop stacking (`-topmost=False`) and retains its native title bar.
2. Normal 60 Hz simulation timing and the 3× speed multiplier are unchanged.
3. Each 16 ms Tk callback may execute at most six simulation steps, sufficient for normal 3× operation with headroom.
4. Excess catch-up debt after a window move or scheduler delay is discarded rather than freezing the UI to replay old wall time.

Verified acceptance gate:

- No production code enables topmost or borderless control-panel behavior.
- The Pygame canvas can participate in normal desktop z-order instead of being forced below the control panel.
- Simulation work has a documented per-callback upper bound.
- Compilation and all 92 automated tests pass.

### II.6 Callback audit

A fresh mocked Tkinter construction captured and invoked the current callback graph:

```text
buttons=28
scales=26
comboboxes=8
window protocols=1
callbacks invoked=62
exceptions=0
```

Verified callback families:

- close, pause/resume, and reset
- green-time and simulation-speed controls
- manual dispatch
- six route activation, headway, TSP, and DBL controls
- six source activation, model, rate, split, and heavy-ratio controls
- LLM display selector
- intentional no-op `RUN LLM`
- recurring main simulation and telemetry polling registrations by source inspection/tests

The committed suite does not contain the full 62-callback fake-widget harness used by this audit. Add it as a regression test so future UI edits cannot lose this coverage.

### II.7 Verification evidence

#### Compilation and imports

```powershell
.\myenv\Scripts\python.exe -m py_compile canvas_gemini.py control_panel.py main.py signal_controller.py telemetry_dashboard.py telemetry_exporter.py vehicle.py
```

Result: PASS.

All seven production modules also imported in a clean process: PASS.

#### Committed tests

```powershell
.\myenv\Scripts\python.exe -m pytest -q
```

Result:

```text
98 passed in 14.28s
0 failed
0 xfailed
0 skipped
```

#### Independent production-function stress run

The audit used the real `main.try_spawn_vehicle()`, `main.check_and_dispatch_buses()`, `SignalController`, and `Vehicle.update()` functions with:

- all six sources enabled at 60 vehicles/minute each;
- all six routes enabled at five-second headways;
- DBL and TSP enabled for every route;
- 2,500 fixed simulation ticks;
- rectangle checks across every live vehicle pair after every movement update.

Result:

```text
ticks completed=2500
first collision=[]
maximum simultaneous vehicles=142
priority grants=4
buses dispatched=27
```

#### Rendering

Headless Pygame result:

```text
normal phases rendered=6
priority states rendered=CONFLICT_YELLOW, ALL_RED_CLEARANCE, PRIORITY_ACTIVE
surface=1000x600
errors=0
```

#### Source-relative paths

From an unrelated temporary working directory, all four resolved paths pointed to the repository:

```text
main.DASHBOARD_PATH -> ...\traffic_simulator\telemetry_dashboard.py
main.TELEMETRY_PATH -> ...\traffic_simulator\traffic_state_telemetry.json
TelemetryExporter default -> ...\traffic_simulator\traffic_state_telemetry.json
TelemetryDashboard file -> ...\traffic_simulator\traffic_state_telemetry.json
```

#### Telemetry

Verified:

- schema version 2;
- both node keys present;
- authoritative passenger volume;
- atomic write and valid JSON readback;
- LIVE/PAUSED/STALE/CONNECTING classifications;
- poll rescheduling after unexpected errors.

### II.8 Coverage limitations and recommendations

These are not reproduced production failures:

1. The audit did not open the real multi-window Tkinter/Pygame application interactively. GUI rendering and callbacks were tested headlessly/mocked.
2. The full 62-callback harness is not committed as a repeatable test.
3. The independent production-function stress probe used one deterministic seed. The committed accelerated adversarial test uses two additional seeds.
4. `main.cleanup()` calls `dashboard_proc.terminate()` but does not `wait()` for confirmed child exit and suppresses cleanup exceptions. Add a short bounded wait followed by kill/logging only if required.
5. No performance benchmark currently enforces an upper bound for the O(vehicles²) following and collision/reservation scans under extreme demand.

Recommended additions:

- commit the callback harness;
- add a subprocess launch/close test from repository, parent, and temporary working directories;
- add five-to-ten long deterministic seeds with production spawning and all six priority routes;
- record collision, red-entry, queue, wait, denial, and grant invariants as machine-readable test results;
- add a high-load timing benchmark before increasing demand beyond the current UI ranges.

### II.9 Completed remediation plan

1. **Completed:** fixed A-01 with assigned physical-node identity at spawn and turn.
2. **Completed:** fixed A-02 with pre-construction duplicate checks, stable request/attempt identity, durable terminal history, and eligibility-edge retry policy.
3. **Completed:** fixed A-03 by separating pending, granted, and clearing state in telemetry, dashboard metrics, and DBL rendering.
4. **Completed:** fixed A-04 with an empty-box all-red gate plus geometry-aware release of compatible traffic during green.
5. **Completed:** fixed A-05 by restoring normal desktop stacking and bounding fixed-step catch-up work per Tk callback.
6. **Completed:** added twenty regression cases, bringing the suite from 78 to 98 tests.
7. **Recommended hardening:** commit the full 62-callback fake-widget harness and add a child-process lifecycle test.
8. **Completed:** repeated compilation and the full suite, including all route legs, route completion, multi-seed adversarial congestion, telemetry validation, and rendering-related tests.

Do not weaken same-path left-turn serialization, yellow/all-red timing, occupied-box clearance before green, per-node isolation, or rear-clear release while implementing future changes.

### II.10 Congestion Peak extension

Added on 2026-08-30 in response to the inability of Random, Poisson, Binomial, and Negative Binomial demand at the existing 1–30 vehicles/minute slider range to reliably oversaturate the network.

#### Operator behavior

Select `Congestion Peak` independently for any source in the existing Generation Model dropdown. The normal rate slider remains the recovery-period rate.

Each selected source starts with:

```text
30 simulated seconds PEAK
30 simulated seconds RECOVERY
repeat
```

Peak demand is:

```text
max(configured rate × 4, 90 vehicles/minute)
```

Recovery demand returns to the configured 1–30 vehicles/minute value.

#### Backlog and crash-safety behavior

- Demand arrivals are counted before physical spawn admission.
- A blocked spawn boundary does not discard Congestion Peak demand.
- Pending demand is stored as an integer counter, not thousands of off-screen `Vehicle` objects.
- Each source backlog is capped at 5,000; additional requests increment `overflow_arrivals` rather than growing unbounded state.
- A vehicle object is created only after the normal lane-specific 40-pixel entry-clearance check succeeds.
- Pressing Reset clears vehicles, burst state, counters, and congestion backlog.
- Changing generation model clears stale model-specific state on the next active update.

#### Telemetry and dashboard

Telemetry now exports per source:

```text
configured_rate_vpm
effective_rate_vpm
peak_active
pending_arrivals
requested_arrivals
admitted_arrivals
overflow_arrivals
```

The dashboard `Road/Demand Queue` card displays `physical road queue / pending source demand`.

#### Verification

- A blocked peak arrival remains pending and is consumed only after safe admission.
- Peak/recovery rate switching is deterministic and tested.
- Backlog overflow is capped and counted.
- A 30-second production-spawner peak produces a visible stopped road queue.
- All six sources were run simultaneously in Congestion Peak for 1,200 ticks with bounded backlog and zero cross-direction rectangle overlaps.
- Compilation and all 98 automated cases pass.

### II.11 Final assessment

The simulator is currently suitable for continued development and demonstration of the two-node DBL/TSP concept, including controlled oversaturation scenarios. The audited safety core passed all present collision and phase-interlock gates, and A-01 through A-05 are corrected. Route progress, same-green traffic flow, priority telemetry, desktop window stacking, and bounded congestion demand now have explicit tested semantics; the remaining Section 8 work concerns additional automation, lifecycle hardening, and performance coverage.

---

## Part III — Network Gridlock Discharge Recovery (2026-09-03)

### III.1 Implemented operator controls

The control panel now provides:

```text
Discharge Mode:
- Auto (Recommended)
- Eastbound Corridor
- Westbound Corridor
- Node A Northbound
- Node A Southbound
- Node B Northbound
- Node B Southbound

[ START DISCHARGE ]  [ SAFE STOP ]
```

The live control-panel and telemetry-dashboard messages expose:

```text
Selected
Status
Reason
Recommended first action
Current stage
Vehicles discharged
```

An unavailable selection remains all-red and reports the physical reason. For
example:

```text
Selected: Eastbound Corridor
Status: WAITING
Reason: Node B eastbound exit has insufficient storage
Recommended first action: Discharge Node B Northbound
```

### III.2 Safety and sequencing contract

- Recovery suspends new arrivals, pending-demand admission, automatic/manual
  bus dispatch, and new DBL/TSP arbitration.
- An existing priority request is cancelled into durable terminal history with
  reason `NETWORK_DISCHARGE_STARTED`.
- An active priority green first changes to yellow; it is never cut directly to
  a conflicting recovery green.
- Every recovery stage uses transition yellow and minimum all-red.
- A discharge green is granted only when its target conflict box is physically
  clear and at least one waiting vehicle has downstream receiving space.
- Only the selected approach can be green at a node. Other approaches remain
  red, while the other node is either all-red or serving the same coordinated
  corridor.
- Intersection reservations, collision checks, following distance, turn
  serialization, and spillback prevention remain authoritative.
- `SAFE STOP` returns to normal control only after yellow, minimum all-red, and
  both physical conflict boxes are clear.

The corridor staging is downstream-first:

```text
Eastbound: Node B downstream -> Node B and Node A coordinated
Westbound: Node A downstream -> Node A and Node B coordinated
```

### III.3 Automatic selection

`Auto (Recommended)` is deterministic. It ranks only current candidate
movements, prioritizing:

1. A downstream corridor required by an existing intersection reservation.
2. A candidate with a clear conflict box and receiving storage.
3. The number of vehicles that the plan can discharge.
4. Time since that plan was last served, providing bounded fairness.

Conditions are recalculated after every protected stage. If no safe movement
is available, Auto remains all-red and explains that arrivals must remain
suspended while downstream traffic drains. It does not delete, teleport, or
move vehicles through occupied space.

### III.4 Recovery completion and demand restoration

Manual mode completes after the selected plan drains. Auto continues until no
vehicle remains upstream of a controlled intersection and both conflict boxes
are clear. Normal timing then resumes from a canonical EW phase after a final
all-red clearance.

Congestion Peak backlog is preserved rather than silently discarded. For ten
simulated seconds after recovery:

- admission attempts occur at most once per source per simulated second;
- automatic and manual bus dispatch remain suspended; and
- telemetry reports `post_recovery_metering=true`.

This prevents the retained backlog from immediately recreating saturation.

### III.5 Verification

The implementation added 12 focused recovery tests. Verified behaviors include:

- downstream-first EB staging and coordinated corridor operation;
- deterministic Auto selection;
- no more than one green approach per node;
- explicit insufficient-storage waiting diagnostics and recommendations;
- active-priority yellow before discharge all-red;
- durable cancellation of pending priority requests;
- safe stop remaining all-red while either conflict box is occupied;
- arrival and bus-dispatch suspension;
- bounded post-recovery demand metering without backlog loss;
- telemetry and dashboard recovery fields;
- reset cleanup; and
- a 1,200-tick six-source Congestion Peak buildup followed by 3,600 recovery
  ticks, with a reduced upstream queue and zero cross-direction rectangle
  overlaps.

Final repeatable result:

```text
py_compile: PASS for all seven production modules
pytest -q: 115 passed in 39.39s
control-panel hidden GUI smoke: PASS
telemetry recovery-card construction and message formatting: PASS
```

### III.6 Remaining preventive work

This feature provides a safe operator/automatic escape path after saturation.
It does not replace the preventive receiving-lane capacity reservation and
network-level storage-admission improvements described in the separate
gridlock incident report. Those controls should still be implemented if the
goal is to prevent the deadlock from forming rather than recover after it forms.
