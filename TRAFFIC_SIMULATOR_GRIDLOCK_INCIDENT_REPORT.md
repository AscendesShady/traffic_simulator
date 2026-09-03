# Traffic Simulator Gridlock Incident Report

**Incident date:** 30 August 2026  
**Project:** Two-node Pygame/Tkinter traffic simulator  
**Severity:** Critical  
**Diagnostic confidence:** High  
**Purpose:** Give a coding LLM enough evidence and implementation guidance to reproduce, diagnose, and safely correct the gridlock without weakening collision protection.

Companion documents:

- [Simulator Guide and Source Documentation](TRAFFIC_SIMULATOR_GUIDE_AND_DOCUMENTATION.md)
- [Audit and Step-by-Step Fix Report](TRAFFIC_SIMULATOR_AUDIT_AND_STEP_BY_STEP_FIX_REPORT.md)

---

## 1. Technical summary

The live simulator entered a persistent, network-wide **block-the-box deadlock**. The simulation loop and telemetry remained active, but traffic discharge stopped:

- 157 vehicles were active.
- 154 vehicles, or 98.1%, were stopped upstream of a node.
- All six buses were stationary.
- Node A and Node B were both held in normal `ALL_RED`.
- Node A retained one intersection-entry reservation.
- Node B retained two intersection-entry reservations.
- No DBL or TSP request was active or queued.
- The three outstanding reservations matched the difference between total vehicles and upstream queued vehicles: `157 - 154 = 3`.

The controller was not frozen. Simulation frames and phase timers continued increasing. It was deliberately refusing to grant a conflicting green because physical vehicles had not cleared the conflict boxes.

The likely circular wait was:

```text
All-red waits for every intersection occupant to clear
                         |
                         v
Box occupants wait for downstream receiving space
                         |
                         v
Downstream queues wait for a signal green
                         |
                         v
Both nodes remain all-red, so no receiving space is created
```

The all-red safety interlock is doing its intended job and must not simply be bypassed. The primary repair should prevent vehicles from entering without guaranteed exit storage, followed by a collision-safe recovery strategy and better deadlock telemetry.

---

## 2. Instructions for the coding agent

Treat this report as a starting diagnosis, not permission to apply an unverified shortcut.

Required workflow:

1. Read all relevant production and test files before changing code.
2. Reproduce the deadlock deterministically in a new automated test.
3. Confirm which exact vehicles and receiving lanes form the circular dependency.
4. Implement receiving-lane capacity protection before adding recovery behavior.
5. Preserve all-red and movement-conflict safety invariants.
6. Add telemetry that makes future holds directly diagnosable.
7. Run the existing complete regression suite plus new adversarial congestion tests.

Do not:

- Replace all-red clearance with an unconditional timeout-to-green.
- Grant a conflicting movement while a vehicle occupies the node.
- Delete a reservation only because it is old while its vehicle remains in the box.
- Treat `pending_demand = 0` as proof that no arrival pressure exists.
- Assume DBL/TSP caused the current deadlock merely because priority was used earlier.
- Fix only the dashboard indicator while leaving the physics/controller deadlock intact.

---

## 3. Files and responsibilities

| File | Relevant responsibility | Important locations |
|---|---|---|
| `signal_controller.py` | Physical box clearance, normal phase progression, movement reservations, priority arbitration | `is_intersection_clear()` near line 170; `_cleanup_reservations()` near line 293; `_normal_phase_update()` and its all-red hold near lines 512–525 |
| `vehicle.py` | Downstream spillback test, signal compliance, entry reservation request, following behavior and box clearance | `is_spillback_blocked()` near line 95; upstream signal handling near line 177; entry decision near line 194; following stop near line 208 |
| `telemetry_exporter.py` | Queue definition, node state, timing, reservations and active-bus export | `compute_queue_counts()` near line 24; payload construction near line 100 |
| `main.py` | Stochastic demand, boundary admission, simulation update order and reset | Congestion state near lines 19–136; `try_spawn_vehicle()` near line 183; fixed-step loop later in the file |
| `traffic_state_telemetry.json` | Latest live snapshot only; not a historical event log | `network_summary`, `signal_state.nodes`, `demand_generation`, `active_buses` |
| `tests/` | Existing safety, priority, runtime and telemetry regressions | Add deterministic gridlock and recovery tests here |

The coding agent should also inspect:

- `tests/test_adversarial_simulation.py`
- `tests/test_signal_priority.py`
- `tests/test_vehicle_safety.py`
- `tests/test_runtime_and_telemetry.py`
- `tests/helpers.py`

---

## 4. Evidence captured from the live run

### 4.1 Initial fresh snapshot

The first inspected telemetry file was approximately 0.19 seconds old, so the dashboard source was fresh.

At frame 52,624:

| Metric | Value |
|---|---:|
| Simulation time | 877.067 s |
| Simulation paused | No |
| Simulation speed | 3.0x |
| Total vehicles | 156 |
| Total buses | 6 |
| Stopped upstream queue | 153 |
| Pending source backlog | 0 |
| Node A phase | `ALL_RED` |
| Node A all-red timer | 21,357 frames |
| Node A reservations | 1 |
| Node B phase | `ALL_RED` |
| Node B all-red timer | 28,501 frames |
| Node B reservations | 2 |

Every signal at both nodes was red. Neither node had an active or queued priority request.

### 4.2 Six consecutive observations

Six read-only samples were collected at approximately one-second wall-clock intervals.

| Sample | Frame | Simulation s | Vehicles | Stopped queue | Node A all-red frames | Node B all-red frames | Stopped buses |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 54,124 | 902.067 | 157 | 154 | 22,857 | 30,001 | 6 |
| 2 | 54,244 | 904.067 | 157 | 154 | 22,977 | 30,121 | 6 |
| 3 | 54,364 | 906.067 | 157 | 154 | 23,097 | 30,241 | 6 |
| 4 | 54,484 | 908.067 | 157 | 154 | 23,217 | 30,361 | 6 |
| 5 | 54,544 | 909.067 | 157 | 154 | 23,277 | 30,421 | 6 |
| 6 | 54,664 | 911.067 | 157 | 154 | 23,397 | 30,541 | 6 |

Confirmed observations:

- Frames and timers advanced, proving that the runtime loop was alive.
- Vehicle count did not fall.
- Queue count did not fall.
- Every approach queue remained unchanged.
- Every bus coordinate and speed remained unchanged.
- Reservation counts remained unchanged.
- Both nodes remained all-red.

This is evidence of zero observable discharge during the sample window, not merely slow-moving congestion.

### 4.3 Later persistence confirmation

The network was inspected again at frame 130,744.

| Metric | Value |
|---|---:|
| Simulation time | 2,179.067 s |
| Total vehicles | 157 |
| Stopped upstream queue | 154 |
| Stopped share | 98.1% |
| Buses stopped | 6 of 6 |
| Node A phase | `ALL_RED` |
| Node A timer | 99,477 frames / 1,658.0 s |
| Node A reservations | 1 |
| Node B phase | `ALL_RED` |
| Node B timer | 106,621 frames / 1,777.0 s |
| Node B reservations | 2 |
| Active or queued priority requests | 0 |

The configured all-red minimum was only 60 frames, or one simulated second.

Estimated all-red entry frames:

- Node B: `130,744 - 106,621 = 24,123`
- Node A: `130,744 - 99,477 = 31,267`

Node B entered the unrecoverable hold first. Node A later entered the same state, converting a partial network obstruction into a two-node lock.

### 4.4 Queue distribution

| Approach | Stopped vehicles | Share of stopped queue |
|---|---:|---:|
| EB | 41 | 26.6% |
| WB | 26 | 16.9% |
| A_NB | 21 | 13.6% |
| A_SB | 22 | 14.3% |
| B_NB | 22 | 14.3% |
| B_SB | 22 | 14.3% |
| **Total** | **154** | **100.0%** |

The blockage was network-wide. No approach was empty enough to provide an obvious independent recovery path.

---

## 5. Metric definitions and interpretation constraints

### Stopped upstream queue

`TelemetryExporter.compute_queue_counts()` counts a vehicle only when:

- Its speed is below 0.25.
- Its front bumper is still upstream of its next target node.

Therefore, a vehicle inside or beyond an intersection box is not counted in the upstream queue.

### All-red hold duration

All-red hold seconds are calculated as:

```text
phase_timer_frames / 60
```

The controller permits all-red to exceed its configured minimum while the physical intersection remains occupied.

### Pending demand

`pending_demand` is not total traffic demand. It is the retained source backlog used principally by the `Congestion Peak` model.

At observation time:

- Every approach reported the `Poisson` model.
- EB and WB were configured at 12 vehicles/minute each.
- Each north/south source was configured at 8 vehicles/minute.
- Total configured source rate was 56 vehicles per simulated minute.

For Poisson demand, `pending_demand = 0` does not mean that sources are inactive or that no further arrivals can enter.

---

## 6. Confirmed findings

### GL-01 — The network reached a hard, persistent gridlock

**Severity:** Critical  
**Confidence:** High  
**Status:** Confirmed

Evidence:

- 154 of 157 vehicles were stopped upstream.
- The remaining count difference was three vehicles.
- Both node phases remained all-red through repeated samples.
- All six buses remained stationary at identical coordinates.
- The same total, queue and reservation values persisted through frame 130,744.

Impact:

- Network throughput reached effectively zero.
- Waiting did not recover the run.
- New signal cycles could not begin.

### GL-02 — Normal all-red clearance is holding indefinitely

**Severity:** Critical  
**Confidence:** High  
**Status:** Confirmed

`SignalController._normal_phase_update()` increments the node timer but refuses to leave phase 2 or 5 when `is_intersection_clear()` is false.

The configured all-red minimum is one second, but the observed holds exceeded 27 and 29 simulated minutes.

This is not a timer failure. It is a physical-clearance condition that never becomes true.

### GL-03 — Three reserved vehicles are likely trapped in the conflict boxes

**Severity:** Critical  
**Confidence:** High, but inferential  
**Status:** Strongly supported

Evidence relationship:

```text
157 total vehicles - 154 upstream queued vehicles = 3 non-upstream vehicles
1 Node A reservation + 2 Node B reservations = 3 reservations
```

Upstream vehicles cancel their entry reservation when their signal is no longer green. Reservation cleanup retains a vehicle while it remains live and has not completed the node.

The strongest explanation is one trapped entrant at Node A and two at Node B.

Limitation: ordinary-car coordinates and reservation occupant identities are not present in telemetry, so this equality is not identity-level proof.

### GL-04 — Entry protection does not guarantee full downstream storage

**Severity:** High  
**Confidence:** High  
**Status:** Confirmed in code; role in this incident is strongly inferred

`Vehicle.is_spillback_blocked()` performs a local, point-in-time check for a nearby stopped downstream vehicle. A vehicle can enter when this check is clear and later stop inside the 132-pixel conflict box because:

- Downstream conditions can change during crossing.
- The receiving space is not reserved.
- The final exit lane is not protected for the entrant until rear clearance.
- Normal following logic stops the vehicle if its leader becomes too close.

The entry reservation protects movement conflicts. It is not a receiving-lane storage reservation.

### GL-05 — There is no network-level deadlock recovery protocol

**Severity:** High  
**Confidence:** High  
**Status:** Confirmed

The controller safely waits for the conflict box to clear, but it has no logic to:

- Identify a cycle of blocking dependencies across Node A and Node B.
- Close all new entries while preserving an escape path.
- Select and release the downstream-most compatible movement.
- Classify a prolonged hold as gridlock.

A safe local rule therefore becomes part of a network-level circular wait.

### GL-06 — Arrival admission can continue amplifying saturation

**Severity:** Medium  
**Confidence:** High  
**Status:** Confirmed

All six source approaches remained active. Non-Congestion-Peak models attempt new vehicle creation whenever their stochastic condition and spawn-boundary gap allow it.

The simulation does not apply a network-storage or zero-throughput metering rule. A gap at the external spawn point can admit another vehicle even when internal discharge is zero.

### GL-07 — Active DBL/TSP is not the immediate cause

**Severity:** Informational  
**Confidence:** High  
**Status:** Confirmed

At the incident snapshots:

- Both nodes reported `priority_state = NORMAL`.
- Both active-request fields were null.
- Both queued-request collections were empty.
- No active bus reported pending, granted or clearing priority.

The last priority terminal event at each node occurred before the reconstructed start of its all-red hold.

Earlier priority operation could have influenced congestion buildup, but the current deadlock is maintained by normal clearance and downstream blockage.

### GL-08 — Telemetry proves gridlock but cannot identify its exact dependency graph

**Severity:** Medium  
**Confidence:** High  
**Status:** Confirmed

The exporter does not provide, for ordinary vehicles:

- Stable vehicle identifier
- Position and speed
- Reservation details
- Entry approach and movement
- Exit direction and lane
- Leader identifier
- Stop or blocked reason
- Receiving-space estimate
- Time spent in the box

The coding agent must reproduce or add temporary/test-only inspection before claiming the exact occupant chain.

---

## 7. Detailed causal mechanism to verify

The current best-supported mechanism is:

1. Heavy demand fills downstream receiving lanes.
2. A vehicle reaches a green signal and passes the current local spillback check.
3. The controller grants a movement reservation.
4. The vehicle enters the conflict box.
5. Its receiving lane stops or fills before the vehicle's rear clears.
6. Following logic sets its speed to zero inside the box.
7. The current signal reaches yellow and then all-red.
8. The controller sees a physically occupied intersection and holds all-red.
9. The trapped vehicle cannot advance because its downstream leader cannot advance.
10. The downstream leader is itself waiting for a green at the other node or is constrained by another saturated exit.
11. The second node eventually reaches the same occupied all-red state.
12. Both nodes now wait for traffic movement that their own red signals prevent elsewhere in the dependency chain.

The agent must verify the exact vehicle and lane chain before choosing recovery ordering.

---

## 8. Remediation plan

### Phase 0 — Preserve the incident and build a deterministic reproducer

Create a new focused test module, for example:

```text
tests/test_gridlock_recovery.py
```

The fixture should create:

- Saturated receiving lanes.
- At least one entrant associated with Node A.
- At least two entrants or blockers associated with Node B.
- A normal transition into phase-index 2 all-red.
- No active DBL/TSP request for the baseline case.

The test should prove the current failure by observing:

- Continued frame advancement.
- No queue reduction.
- No vehicle completion.
- Prolonged all-red.
- Persistent reservations.

Do not begin with randomized full-network runs alone. First create a small deterministic state that produces the failure consistently.

### Phase 1 — Add receiving-lane capacity reservations

Before granting intersection entry:

1. Resolve the actual movement and exit direction.
2. Resolve the actual receiving lane.
3. Measure usable downstream storage beyond the conflict boundary.
4. Require storage for the complete vehicle length plus the required following gap.
5. Reserve that storage so another entrant cannot consume it while the first vehicle crosses.
6. Release receiving storage only after the vehicle's rear safely clears.

The design must cover:

- Straight movements
- Left turns
- Cars, heavy vehicles and buses
- Both horizontal directions
- Node A and Node B vertical roads
- Consecutive vehicles under the same green
- DBL/TSP-exclusive movements

### Phase 2 — Add collision-safe deadlock recovery

Recovery must not mean “turn something green after a timer.”

Recommended model:

1. Detect a prolonged all-red hold and identify physical occupants.
2. Freeze new conflict-box admissions.
3. Build the blocking relationship from each occupant to its downstream leader or unavailable receiving segment.
4. Select the downstream-most movement that can discharge toward an external boundary.
5. Grant only movements compatible with the trapped dependency being cleared.
6. Continue until all boxes are physically empty.
7. Apply recovery all-red.
8. Resume the normal cycle.

Any recovery proposal must demonstrate that no perpendicular or otherwise conflicting movement can enter during the clearance sequence.

### Phase 3 — Meter arrivals when network storage is exhausted

Choose and document one approach:

- Retain external backlog for every stochastic model, not only Congestion Peak.
- Or stop source admission when a network-storage or zero-throughput threshold is reached.
- Or combine both: meter source admission while preserving rejected demand externally.

Do not silently discard demand unless that is an explicit simulation assumption.

Suggested network metering signals:

- Active vehicle count versus estimated storage
- Percentage stopped
- Zero completed vehicles over a rolling interval
- Prolonged all-red at one or both nodes
- Receiving-lane occupancy near each network boundary

### Phase 4 — Improve telemetry and dashboard diagnosis

Add per-node fields such as:

```json
{
  "gridlock_state": "CLEAR | SUSPECTED | CONFIRMED | RECOVERING",
  "all_red_hold_frames": 0,
  "all_red_hold_reason": "MINIMUM_CLEARANCE | PHYSICAL_OCCUPANCY | RECOVERY",
  "intersection_occupants": [],
  "reservations": [],
  "receiving_lane_capacity": {},
  "network_completed_vehicles": 0,
  "rolling_discharge_rate": 0.0
}
```

For each occupant/reservation, export:

- Stable vehicle ID
- Vehicle type
- Node
- Originating approach
- Movement
- Entry lane
- Exit direction and lane
- Position and speed
- Reservation age
- Leader or blocker ID
- Blocked reason
- Estimated downstream free space

The dashboard may then show `GRIDLOCK SUSPECTED` or `GRIDLOCK CONFIRMED`, but this visualization is secondary to correcting the controller and movement logic.

### Phase 5 — Add watchdog diagnostics without unsafe release

A watchdog may:

- Record prolonged all-red.
- Capture occupant and dependency state.
- Raise a visible diagnostic status.
- Trigger a verified recovery state machine.

A watchdog must not:

- Delete live physical occupants.
- Erase reservations while vehicles remain in the node.
- Force a conflicting green solely because a timer expired.

---

## 9. Required regression and acceptance tests

### 9.1 Receiving-space invariant

No vehicle may enter a node unless its resolved exit lane can store:

```text
full vehicle length + configured safe following gap
```

beyond the conflict boundary.

Test this separately for every straight and left movement.

### 9.2 No block-the-box under oversaturated demand

Use seeded high-demand scenarios. Assert that a stopped vehicle cannot retain physical overlap with a conflict box indefinitely.

Do not rely only on route completion tests at low demand.

### 9.3 Collision-safe recovery

The deterministic gridlock fixture must regain positive discharge without:

- Conflicting greens
- Cross-direction overlap
- Reservation duplication
- Vehicle teleportation
- Skipping all-red recovery

### 9.4 Reservation integrity

For every reservation:

- Exactly one live vehicle owns it.
- Its node, movement and entry approach match the vehicle's actual leg.
- It persists while required for safety.
- It releases after verified rear clearance or actual vehicle removal.
- A stale timer alone cannot remove it while the box remains occupied.

### 9.5 All six bus routes

Repeat congestion and recovery tests for all six route configurations under:

- Priority disabled
- DBL only
- TSP only
- DBL and TSP combined

Include both route legs where applicable.

### 9.6 Simulation-speed equivalence

Run representative recovery cases at:

- 1x
- 3x
- Maximum configured speed

State outcomes should be equivalent apart from wall-clock duration.

### 9.7 Demand-stop recovery

After disabling new source demand in each seeded congestion case:

- Queue count must begin decreasing.
- At least one vehicle must complete within a scenario-defined bound.
- Both nodes must eventually leave extended all-red.
- No collision may occur during clearance.

### 9.8 Telemetry completeness

Force a physical hold and assert that telemetry identifies:

- Node occupant
- Reservation owner and age
- Blocking leader or receiving-capacity reason
- Current and minimum all-red duration
- Rolling completed-vehicle count
- Gridlock classification

### 9.9 Existing regression gate

Retain the existing verification commands:

```powershell
.\myenv\Scripts\python.exe -m py_compile canvas_gemini.py control_panel.py main.py signal_controller.py telemetry_dashboard.py telemetry_exporter.py vehicle.py
.\myenv\Scripts\python.exe -m pytest -q
```

At the time of this incident report, the existing suite contained 103 passing tests. Those tests did not prevent this congestion failure, so new gridlock-specific tests are mandatory.

---

## 10. Definition of done

The issue is not resolved until all of the following are true:

- A deterministic pre-fix test reproduces the deadlock.
- Receiving-lane storage is protected before conflict-box entry.
- The reproducer regains discharge after the fix.
- No conflicting movement is released while a node is occupied.
- No reservation is unsafely timed out.
- Both nodes recover under seeded oversaturated demand.
- All six bus routes pass with priority enabled and disabled.
- Telemetry identifies future hold occupants and block reasons.
- The dashboard can distinguish normal clearance from suspected gridlock.
- Compilation succeeds.
- The full prior regression suite passes.
- New adversarial congestion tests pass repeatedly across fixed seeds.

---

## 11. Immediate operational recovery

The observed run did not recover by waiting.

The existing Reset action clears vehicles and spawner state. On the next signal-controller update, empty boxes allow the over-limit all-red phases to advance. Reset therefore recovers operation but destroys the incident traffic state.

Stopping new arrivals alone is not proven to break the existing circular dependency.

Do not use an unsafe forced-green code change as an operational recovery method.

---

## 12. Open implementation questions

The coding agent should answer these with evidence before finalizing the design:

1. Which exact vehicles, exit lanes and leaders occupied Node A and Node B when the lock began?
2. Should receiving capacity be reserved only beyond the conflict boundary or through a longer downstream storage segment?
3. How should receiving reservations interact with ordinary car-following gaps?
4. What deterministic ordering selects the downstream-most safe escape movement?
5. How should a recovery plan behave when every possible exit lane is physically full to a network boundary?
6. Should external backlog be retained for Poisson, Binomial and Negative Binomial demand?
7. What maximum hold duration should classify `GRIDLOCK SUSPECTED` without acting as an unsafe forced-release timer?
8. Should normal controller timing resume from the interrupted phase or a canonical recovery phase?
9. How should DBL/TSP requests be queued, denied or cancelled while gridlock recovery is active?
10. Which throughput and storage metrics should the telemetry dashboard display?

---

## 13. Evidence limitations

This report uses a latest-snapshot JSON file plus repeated live reads. It is not based on a saved vehicle event log.

Consequences:

- The current lock is confirmed.
- Exact ordinary-car identities and positions are unavailable.
- The three trapped reservation owners are strongly inferred, not directly named.
- The historical demand configuration that first created saturation is unknown.
- The current Poisson configuration may differ from the earlier heavy-congestion setup.
- Endpoint equality across the later recheck does not prove that no individual car moved between every intermediate frame, although the network-level lock persisted.

Future incident telemetry should preserve a bounded diagnostic event when gridlock is first suspected so the initiating sequence can be reconstructed.

---

## 14. Source inventory

Primary evidence and code inspected:

- `traffic_state_telemetry.json`
- `signal_controller.py`
- `vehicle.py`
- `telemetry_exporter.py`
- `main.py`
- Relevant existing tests under `tests/`

No production file was modified while diagnosing the incident or creating this report.

---

## 15. Implementation update — controlled network discharge

Implemented on 3 September 2026 as a collision-safe recovery layer.

The operator can select `Auto (Recommended)`, either horizontal corridor, or
one of the four node-specific vertical approaches. Recovery suspends arrivals,
pending-demand admission, bus dispatch, and DBL/TSP arbitration. It then uses
exclusive protected movements with yellow and all-red transitions.

Auto ranks currently safe discharge plans from live vehicle, reservation,
conflict-box, queue, and receiving-space state. Eastbound is staged through
Node B before Node A is opened; westbound is staged through Node A before Node
B. Manual selections remain in `WAITING` when unsafe and report `Selected`,
`Status`, `Reason`, and `Recommended first action` in both the control panel and
telemetry dashboard.

`SAFE STOP` cannot restore normal greens while either intersection is occupied.
After recovery, retained demand is metered for ten simulated seconds rather
than released immediately.

Verification result:

```text
115 tests passed
12 focused discharge tests passed
1,200 congestion ticks + 3,600 recovery ticks completed
upstream queue reduced
cross-direction rectangle overlaps during recovery: 0
```

Scope limitation: discharge mitigates an established gridlock. It does not
supersede the preventive receiving-lane reservation, source metering, or
enhanced diagnostic telemetry recommended earlier in this report. Auto also
refuses to delete or teleport vehicles; if no collision-safe movement exists,
it remains all-red and explains the blocking condition.
