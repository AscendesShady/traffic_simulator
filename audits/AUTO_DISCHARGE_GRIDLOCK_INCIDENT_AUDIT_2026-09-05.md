# Gridlock Discharge Failure — Live Incident Audit

**Incident date:** 2026-09-05  
**Primary capture:** 2026-09-05 12:29:19 +06:00 (Asia/Dhaka)  
**Project:** `traffic_simulator`  
**Severity:** **Critical — network recovery unavailable**  
**Audit type:** Read-only live telemetry capture, source trace, and existing-test review  
**Source changes made during this audit:** None

## 1. Executive verdict

The gridlock is confirmed. The simulator is still running and exporting fresh telemetry, but traffic has stopped and the discharge controller is making no recovery progress.

At the primary capture:

- The simulation was running at `1.0x`, not paused.
- The controller was in `NETWORK_DISCHARGE_WAITING`.
- Every approach at Node A and Node B was red.
- `152` vehicles were present and `147` were counted as queued (`96.7%`).
- Recent throughput was `0.0 passengers/minute` over the 30-second trend window.
- Discharge reported `0` discharged vehicles and `0` completed cycles.
- Node A retained `1` intersection reservation; Node B retained `4`.
- A Route R2 bus was stationary while still geometrically overlapping Node A's conflict box.
- Pending demand was `296`, entirely from the westbound Congestion Peak source. New admission was correctly suspended during discharge.

The immediate failure is a circular wait:

```text
Discharge waits for Node A's conflict box to become empty
                         |
                         v
WAITING gives no green to any approach at either node
                         |
                         v
The in-box eastbound bus cannot advance through its downstream queue
                         |
                         v
Node A never becomes empty; WAITING has no timeout or recovery branch
                         |
                         +-----------------------------> repeats forever
```

There is also a vital mode discrepancy. The operator reported that **Auto Discharge** was being used, but the captured controller state says:

```json
"mode": "Westbound Corridor",
"selected": "Westbound Corridor"
```

Therefore, this exact capture is a **manual Westbound Corridor discharge attempt**, not a controller-confirmed Auto run. Either Westbound was selected when `START DISCHARGE` was pressed, or the control panel displayed/accepted Auto without delivering it to the controller. The available telemetry cannot distinguish those two possibilities. This discrepancy must be reproduced and tested before claiming that the captured run proves only an Auto-selection failure.

## 2. Evidence preserved from the live run

### 2.1 Capture integrity

| Field | Captured value |
|---|---:|
| Telemetry file | `traffic_state_telemetry.json` |
| Schema | `3` |
| SHA-256 of bytes parsed | `26e4c4a8913202c859255867a516c078938bc62f75f162f8546e9123f171bd01` |
| Telemetry age when parsed | `0.283 s` |
| Frame | `69,415` |
| Simulation time | `1,156.917 s` (`19:16.917`) |
| Simulation paused | `false` |
| Simulation speed | `1.0` |

The low telemetry age and advancing frame/simulation clocks confirm that this was a live capture, not a stale dashboard file.

### 2.2 Discharge state

| Field | Captured value |
|---|---|
| Active | `true` |
| Mode | `Westbound Corridor` |
| Selected plan | `Westbound Corridor` |
| Controller/display state | `WAITING` |
| Stage | `Node A downstream` (index `0`) |
| Reason | `Node A intersection is still occupied` |
| Recommendation | `Maintain arrival suspension while downstream traffic drains` |
| Vehicles discharged | `0` |
| Completed cycles | `0` |
| Arrivals suspended | `true` |
| TSP/DBL priority suspended | `true` |

Two priority requests were canceled with `NETWORK_DISCHARGE_STARTED` at frame `46,812`. Because `_start_discharge()` cancels priority as it starts recovery, this is the earliest controller-backed start marker for this discharge. By frame `69,415`, the discharge had existed for at least `22,603` frames, or approximately **376.7 simulated seconds (6 minutes 16.7 seconds)**, with zero reported discharge progress.

### 2.3 Signal and reservation state

| Node | EB | WB | NB | SB | Reservations |
|---|---|---|---|---|---:|
| Node A (`x=300`) | RED | RED | RED | RED | 1 |
| Node B (`x=700`) | RED | RED | RED | RED | 4 |

The controller has no active or queued priority request at either node. Current TSP/DBL arbitration is therefore not holding a priority green; discharge owns the signal schedule.

### 2.4 Network load

| Approach/source | Queued vehicles | Estimated queued passengers | Demand model | Pending demand |
|---|---:|---:|---|---:|
| Eastbound corridor | 24 | 96 | Neg Binomial, 12 vpm | 0 |
| Westbound corridor | 35 | 140 | Congestion Peak, 12 vpm | 296 |
| Node A northbound | 22 | 88 | Poisson, 8 vpm | 0 |
| Node A southbound | 21 | 84 | Poisson, 8 vpm | 0 |
| Node B northbound | 22 | 88 | Poisson, 8 vpm | 0 |
| Node B southbound | 23 | 92 | Poisson, 8 vpm | 0 |
| **Total** | **147** | **588 estimated** | — | **296** |

Additional captured totals:

- Vehicles in network: `152`
- Active buses: `2`
- Passenger volume currently in network: `690`
- Lifetime served: `456 vehicles` / `3,464 passengers`
- Lifetime average at capture: `179.6 passengers/minute`
- Recent 30-second rate: `0.0 passengers/minute`

The queued-passenger column is the exporter's four-passengers-per-queued-vehicle estimate and is not an exact occupancy count. The separate network passenger volume uses each vehicle's actual simulated passenger count.

### 2.5 Stationary buses

| Bus | Route/position | State | Speed | Relevant fact |
|---|---|---|---:|---|
| `BUS_R2_EB_B_NB_045` | R2, eastbound, `(380.5, 267.0)` | `IN_INTERSECTION`, target Node A | `0.0` | Stop-bar distance is `-177.5 px`; its 42 px body still overlaps Node A's east edge by about `6.5 px`. |
| `BUS_R5_WB_B_SB_042` | R5, westbound, `(900.1, 355.0)` | `APPROACHING`, target Node B | `0.0` | `103.1 px` upstream of its stop bar. |

The R2 bus is the directly visible reason Node A fails `is_intersection_clear()`. It needs only a small forward movement to clear the geometric box, but its position and speed remained unchanged throughout repeated live samples.

### 2.6 No-progress observation interval

From frame `63,205` to frame `63,584`, followed by a confirming capture at frame `64,074`:

- Simulation time advanced from `1,053.417 s` to `1,067.900 s`.
- Discharge stayed `WAITING` on `Node A downstream`.
- Discharged count stayed `0`; cycle count stayed `0`.
- Total vehicles stayed `152`; queued vehicles stayed `147`; pending demand stayed `296`.
- Recent passenger throughput stayed `0.0`.
- Node reservations stayed at A=`1`, B=`4`.
- Both bus positions and speeds remained unchanged.
- Both nodes stayed all-red.

This rules out a merely slow animation or stale telemetry. The simulation clock advances while the physical network and recovery state do not.

## 3. Confirmed source-code findings

### F-01 — Critical: `WAITING` can persist indefinitely with every approach red

**Status:** Confirmed by live evidence and source trace.

`SignalController._stage_readiness()` rejects a stage before any green is issued if the target conflict box is occupied (`signal_controller.py`, lines 316–320). `_activate_current_stage()` then sets `WAITING` and empties the discharge green map (`signal_controller.py`, lines 470–487).

`_discharge_signals_for_node()` initializes every approach to red and adds a green only in `DISCHARGE_ACTIVE` (`signal_controller.py`, lines 768–786). `WAITING` therefore means all-red at both nodes.

`_update_discharge()` handles `WAITING` only by calling `_choose_or_wait_for_discharge()` again and returning (`signal_controller.py`, lines 724–726). The stall timeout is checked only later in the `ACTIVE` branch (`signal_controller.py`, lines 748–766). Consequently:

- `discharge_stall_time=180` frames does **not** protect the `WAITING` state.
- There is no maximum wait, dependency escalation, alternative-plan switch, or terminal failure state.
- An occupied box that cannot drain naturally creates an unbounded all-red wait.

This is the primary controller defect exposed by the incident.

### F-02 — Critical: the chosen manual plan conflicts with the dependency that must drain first

**Status:** Confirmed incident mechanism; the exact lead vehicle is not exported.

The selected stage is westbound at Node A, but the conflict-box occupant is an eastbound R2 bus leaving Node A toward Node B. The bus is already downstream of Node A's stop bar, so `vehicle.py` does not stop it because Node A is red: signal stopping is applied only while `upstream` (`vehicle.py`, lines 176–202).

Its zero speed is instead produced by the vehicle-following path (`vehicle.py`, lines 204–215), consistent with the stationary eastbound queue toward Node B. Because every Node B approach is also red, that downstream queue is not being actively drained. The selected Westbound/Node-A stage waits for the eastbound bus to clear, while the eastbound bus needs downstream eastbound storage to clear.

The missing recovery behavior is **dependency-first draining**: recognize the in-box vehicle's exit direction and safely drain the receiving corridor/node before attempting the operator's target stage. Clearing Node A by ignoring the overlap would be unsafe and is not recommended.

### F-03 — High: Auto mode latches an unready plan instead of continuously replanning

**Status:** Confirmed source defect, but not proven as the trigger for this manual-mode capture.

When Auto has no ready candidate, `_choose_or_wait_for_discharge()` stores the highest-ranked unready candidate in `self.discharge_plan_name` and returns `WAITING` (`signal_controller.py`, lines 514–538). On later calls, ranking is skipped because `discharge_plan_name` is no longer `None`; the controller retries only the latched plan (`signal_controller.py`, lines 551–556).

Thus Auto does not actually reevaluate all candidates while waiting. Even if another plan becomes ready or is the only route capable of breaking a dependency, the stored unready plan can remain latched. The documentation statement that Auto "reevaluates after every protected stage" is not sufficient for a pre-stage deadlock.

### F-04 — High: reported Auto intent and controller mode disagree

**Status:** Confirmed discrepancy; cause not established by current artifacts.

The control panel callback reads the combobox at button press and writes it to `global_config["discharge_selection"]` (`control_panel.py`, lines 443–457). The controller consumes that value and stores it as `discharge_mode` (`signal_controller.py`, lines 626–649). Telemetry publishes `discharge_mode` separately from the resolved `selected` plan (`signal_controller.py`, lines 224–241).

Because both captured fields say `Westbound Corridor`, this run did not reach the controller as Auto. Required follow-up evidence is a screen recording or automated callback test that:

1. selects `Auto (Recommended)`,
2. presses `START DISCHARGE`, and
3. asserts telemetry `mode == "Auto (Recommended)"` while `selected` may resolve to a corridor.

### F-05 — Medium: the displayed recommendation is not actionable and may be false-progress advice

**Status:** Confirmed.

The recommendation remained `Maintain arrival suspension while downstream traffic drains`, but no downstream signal was green and no position changed. Arrival suspension prevents new demand from worsening the problem, but it cannot drain already admitted traffic by itself.

The controller should report the blocking vehicle/reservation, waiting duration, required downstream movement, candidate readiness reasons, and the action it is taking. If it cannot take an action, it should explicitly report `STALLED` or `RECOVERY_FAILED` rather than repeat a passive recommendation indefinitely.

### F-06 — Medium: current automated tests do not cover the live deadlock topology

**Status:** Confirmed.

Command run during this audit:

```powershell
.\myenv\Scripts\python.exe -m pytest tests\test_network_discharge.py -q
```

Result: `12 passed in 27.30s`.

The passing saturated-network test proves one randomized seed can recover safely; it does not prove recovery from every gridlock topology. The suite lacks a deterministic case with:

- a vehicle partly inside a conflict box,
- a stationary downstream chain at the other node,
- all-red discharge waiting,
- no naturally moving vehicle,
- and a bounded-time assertion that recovery either progresses or reports a safe failure.

### F-07 — Informational: LLM control is not the immediate signal owner

**Status:** Confirmed for the captured files.

`ai_control.json` reports `"armed": false`. `main.py` merges `decision.json` only while the in-process AI runtime is armed (lines 521–524). The discharge status also reports priority suspension, and neither node has an active priority request. Although the persisted `decision.json` contains TSP/DBL flags for R2 and R4, there is no evidence that the LLM control loop is causing the present all-red `WAITING` state.

## 4. Root-cause classification

| Category | Verdict | Evidence strength |
|---|---|---|
| Simulation process crash | No | Strong: live frame/time and fresh telemetry |
| Telemetry/dashboard stale | No | Strong: 0.283-second age and changing frames |
| Demand still entering during discharge | No | Strong: every source reports admission suspended |
| TSP/DBL currently holding signals | No | Strong: priority suspended; no active requests |
| Conflict box genuinely occupied | Yes | Strong: R2 bus bounds overlap Node A |
| Existing in-box vehicle stopped by red itself | No | Strong source evidence: signal stop applies upstream only |
| Existing in-box vehicle blocked by downstream traffic | Very likely | Strong behavioral/source inference; lead vehicle identity is not exported |
| Discharge `WAITING` has a timeout/escape path | No | Strong source evidence |
| Captured discharge run was Auto | No | Strong telemetry contradiction |
| Auto has an independent replanning defect | Yes | Strong source evidence |

## 5. Required correction plan

The fixes should be implemented in this order. Safety invariants—no conflicting greens, yellow/all-red transitions, spillback checks, and reservations—must remain mandatory.

### Step 1 — Add complete incident telemetry before changing selection logic

Export:

- requested mode (`Auto` versus manual), resolved plan, and plan-selection frame;
- `waiting_since_frame`, waiting seconds, and last-progress frame;
- every conflict-box occupant: stable vehicle/bus ID, node, bounds, speed, approach, movement, exit direction, reservation age, and lead gap;
- ranked discharge candidates with `ready`, score, queue count, and rejection reason;
- the detected downstream dependency chain.

This makes future failures diagnosable without attaching a debugger to the live Pygame process.

### Step 2 — Make Auto re-rank throughout `WAITING`

Do not permanently store an unready Auto candidate. While waiting, either leave `discharge_plan_name=None` or clear it before each evaluation. Re-rank all candidates on a controlled interval and immediately choose a newly ready safe stage.

Acceptance condition: if candidate A is unready and candidate B becomes ready, Auto must transition through the required clearance and activate B within a bounded number of frames.

### Step 3 — Add dependency-first recovery

When a conflict box is occupied by a stopped vehicle:

1. Keep conflicting new entries red.
2. Determine the occupant's exit direction and downstream target.
3. Identify which downstream protected movement creates receiving space.
4. Drain that dependency first through normal yellow/all-red and exclusive-green safety rules.
5. Recheck the original box and resume the requested corridor only after it clears.

For this incident, the R2 eastbound vehicle at Node A indicates that the eastbound receiving corridor toward/through Node B must be assessed before a Node-A westbound stage can become ready.

### Step 4 — Add a bounded `WAITING` watchdog

Track meaningful progress, not only elapsed green time. Progress can be a blocker moving, a reservation being released, a vehicle passing a node, queue reduction, or a readiness change.

After a configured no-progress interval:

- Auto: recompute dependencies and switch to the best safe recovery action.
- Manual: keep the requested objective, but propose and optionally execute a clearly labeled dependency prerequisite.
- If no safe action exists after bounded retries: enter `RECOVERY_FAILED`, preserve all-red, identify the blockers, and tell the operator that a simulation reset is required.

Do not silently override collision, receiving-space, or reservation rules.

### Step 5 — Resolve and test the Auto/manual command discrepancy

Add a callback-to-controller integration test proving that the combobox selection at click time becomes telemetry `mode`. The dashboard must display both:

- `Requested mode: Auto (Recommended)`
- `Resolved action: Eastbound Corridor` (example)

This prevents an automatically resolved plan from being mistaken for a manual selection and exposes any stale UI value.

### Step 6 — Improve operator messaging

Replace the passive message with information such as:

```text
Requested mode: Auto (Recommended)
Resolved plan: Eastbound Corridor
Status: WAITING — STALLED 24.3 s
Blocker: BUS_R2_EB_B_NB_045 partially occupies Node A
Dependency: eastbound receiving space toward Node B
Next safe action: drain Node B eastbound, then retry the requested stage
```

### Step 7 — Add deterministic regression and long-run tests

Minimum new tests:

1. `test_waiting_watchdog_does_not_remain_all_red_forever`
2. `test_auto_reranks_when_latched_candidate_is_unready`
3. `test_dependency_drain_releases_vehicle_partly_inside_node_a`
4. The mirrored Node B case.
5. EB and WB corridor spillback cases.
6. All four vertical approaches.
7. Manual selection preserving its final objective while allowing safe prerequisite stages.
8. Auto combobox → callback → controller → telemetry mode integration.
9. No perpendicular rectangle overlap during every recovery transition.
10. Reservation cleanup and `passed_nodes` consistency after dependency draining.
11. A multi-seed, high-demand soak test with a bound on continuous all-red waiting.
12. A fail-safe test where no safe movement exists and `RECOVERY_FAILED` is reported without collision.

## 6. Acceptance gates for the eventual fix

The incident is not resolved merely because unit tests pass. A corrected build must satisfy all of the following:

- No `WAITING` state can continue beyond its configured watchdog without a replan, visible progress, or explicit safe failure.
- Auto mode remains identified as Auto in telemetry while its resolved plan is reported separately.
- A vehicle already in a conflict box is never abandoned behind an indefinite all-red dependency cycle.
- The controller never authorizes conflicting approaches at one node.
- Receiving-space and spillback protection remain active.
- Yellow and all-red transitions occur before changing protected movements.
- Arrival and priority suspension remain active for the entire recovery operation.
- Reservations are released only after the corresponding vehicle safely clears or is removed.
- The exact topology captured here recovers in a deterministic test.
- High-demand multi-seed tests show decreasing queue/occupancy or a bounded, diagnosable safe failure—not an infinite wait.

## 7. Immediate operational conclusion

The current run will not recover on its own under the observed state unless some already admitted vehicle can begin moving without a signal change. More than six simulated minutes of zero discharge and unchanged vehicle positions show that this is not occurring.

`SAFE STOP` is not guaranteed to recover this topology because the stop path also waits for occupied conflict boxes to clear. The only guaranteed operator escape in the present implementation is `RESET VEHICLES`, which intentionally destroys the current simulation state. Preserve this report and any desired screenshots before resetting.

Do **not** fix this by declaring the slightly overlapping R2 bus "clear" or by globally overriding conflict rules. The correct remedy is bounded, dependency-aware, protected draining plus explicit failure handling.

---

## Audit boundary

This report distinguishes direct evidence from inference. The telemetry does not export all individual cars, lead gaps, blocker IDs, or Auto candidate rankings, so the exact car immediately ahead of R2 cannot be named. The stationary downstream-chain conclusion follows from the live zero-speed observation and the only applicable in-vehicle stopping path, but the proposed telemetry enrichment is required to make that dependency fully explicit in future captures.
