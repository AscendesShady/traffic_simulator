# Priority-Green Gridlock Incident Report — 2026-09-07

**Incident capture:** 2026-09-07 02:54:13 +06:00 (Asia/Dhaka)  
**Project:** `traffic_simulator`  
**Severity:** Critical — network saturation and multi-approach starvation  
**Diagnostic confidence:** High for the controller failure; medium-high for the physical blocker at Node B  
**Audit mode:** Read-only telemetry/log analysis and source trace; no simulator code changed

## 1. Technical summary

The network is in a **priority-green starvation lock**, not the older all-red
clearance deadlock.

At the captured frame, `119` of `120` vehicles (`99.2%`) met the telemetry
definition of a stopped upstream queue. Both signalized nodes were held in
`PRIORITY_ACTIVE` with only westbound green. The two buses holding those grants
were stopped immediately upstream of their stop bars despite having green:

- Node A: `BUS_R4_WB_A_SB_016`, 34.5 px from the stop bar, speed `0.0`.
- Node B: `BUS_R4_WB_A_SB_020`, 25.0 px from the stop bar, speed `0.0`.

The active grants had persisted for approximately 614.6 and 599.2 seconds.
`signal_controller.py` checks the request deadline before activation, but once
the state is `PRIORITY_ACTIVE` it only releases the grant after the bus crosses
the stop line. It has no active-state timeout or no-progress watchdog.

The LLM amplified but did not originate the safety defect. The agent repeatedly
requested TSP for R1 and R4, while the controller/guard correctly prevented it
from changing an already granted route. However, the model sees a free-flow ETA
of about 0.4–0.6 seconds and the phrase `GRANTED - do not change`, even though
the same buses have speed `0.0` and a capped live ETA of `100.0` seconds.

Automatic discharge has **not** failed in this capture; it was never started.
Telemetry reports `network_discharge.active=false`, `status=IDLE`, and
`controller_state=INACTIVE`. Selecting `Auto (Recommended)` chooses the recovery
plan, but recovery begins only after the operator presses `START DISCHARGE`.

## 2. Capture integrity and metric definitions

| Evidence source | Coverage | Freshness / caveat |
|---|---|---|
| `traffic_state_telemetry.json` | Full point-in-time vehicle, bus, signal, priority and discharge state | Frame 46,924; simulation paused at 782.067 s |
| `telemetry_log.jsonl` | 718 approximately one-second session samples | Last sample frame 46,905, only 19 frames before the snapshot |
| `agent_turn_log.jsonl` | 56 surviving agent turns, turns 12–67 | The run was armed through the logged traffic period; the current control file was changed later |
| `ai_control.json` | Latest operator state | Currently disarmed; it does not describe the earlier logged interval |
| `decision.json` | Latest decision | Turn 67 is `HELD_ALL_OFF`; the paused snapshot causes the agent freshness node to hold safely |
| Simulator source | State-transition and prompt logic | Current checkout at the time of capture |

Snapshot SHA-256:
`A3B68D818B65C0A4B59CF29985E8AFFBBC6730032AC545119BD84C862F75B552`.

Queue means a vehicle with speed below `0.25` whose front bumper is still
upstream of its next node's stop bar
([telemetry_exporter.py](../telemetry_exporter.py#L24)). It is not merely the
number of vehicles physically present on an approach.

`queues_passengers_est` uses a flat four-person multiplier. It is suitable only
as an approximate pressure indicator; the current queue includes buses with 45
passengers and trucks with one passenger.

The time-series log does not contain signal or priority fields. Signal-state
history is reconstructed by combining the current request ages with the queue
onset, so exact grant-transition timestamps are approximate. The point-in-time
stuck-state evidence is direct.

## 3. The network filled immediately after the two R4 requests

The controller's internal frame domain is 11,300 frames ahead of the session
frame domain, consistent with a prior RESET that reset the simulation counter
but not the `SignalController.frame_number`. Mapping the request frames into the
current session places the Node A request near 165.5 s and the Node B request
near 180.3 s.

| Simulation time | Vehicles | Stopped queue | Queued share | Passengers served | Recent pax/min |
|---:|---:|---:|---:|---:|---:|
| 150.5 s | 36 | 12 | 33.3% | 1,140 | 512.3 |
| 165.2 s | 27 | 11 | 40.7% | 1,364 | 703.6 |
| 179.8 s | 29 | 19 | 65.5% | 1,435 | 609.0 |
| 200.0 s | 43 | 41 | 95.3% | 1,510 | 220.6 |
| 215.8 s | 68 | 61 | 89.7% | 1,514 | 114.4 |
| 233.9 s | 89 | 82 | 92.1% | 1,530 | 40.1 |
| 242.2 s | 106 | 104 | 98.1% | 1,579 | 130.7 |
| 251.7 s | 114 | 112 | 98.2% | 1,587 | 138.2 |
| 326.8 s | 121 | 119 | 98.3% | 1,717 | 115.1 |
| 781.8 s | 120 | 119 | 99.2% | 2,368 | 119.0 |

The most important movement is not the lifetime throughput total. The network
went from 19 stopped vehicles to 104 in about 62 seconds, then remained at a
119-vehicle stopped plateau for more than 7.5 simulated minutes.

Throughput did not become exactly zero because the permanently green westbound
movement occasionally discharged a vehicle. That does not make the state
healthy: five competing approach movements were red, their queues filled to
stable storage limits, and virtually the whole network remained stopped.

A separate line chart was omitted because the exact onset-and-plateau table is
more auditable for this single incident and avoids implying precision for the
inferred priority-start timestamps.

## 4. Point-in-time evidence

### 4.1 Network state

| Metric | Captured value |
|---|---:|
| Simulation frame / time | 46,924 / 782.067 s |
| Simulation state | Paused at 1.0x |
| Vehicles / buses | 120 / 7 |
| Stopped upstream queue | 119 |
| Stopped share | 99.2% |
| Passenger volume in network | 725 |
| Lifetime passengers served | 2,368 |
| Recent passenger throughput | 111.0 pax/min |
| Pending source demand | 0 |
| Discharge state | `INACTIVE` / `IDLE` |

### 4.2 Every competing approach is saturated or nearly saturated

| Approach | Stopped vehicles | Estimated queued passengers |
|---|---:|---:|
| EB corridor | 20 | 80 |
| WB corridor | 12 | 48 |
| Node A northbound | 20 | 80 |
| Node A southbound | 23 | 92 |
| Node B northbound | 22 | 88 |
| Node B southbound | 22 | 88 |
| **Total** | **119** | **476 approximate** |

### 4.3 Both nodes are held by stopped R4 buses

| Node | Signal output | Active bus / movement | Distance | Speed | Active timer |
|---|---|---|---:|---:|---:|
| A (`x=300`) | WB green; EB/NB/SB red | `BUS_R4_WB_A_SB_016`, WB left to SB | 34.5 px | 0.0 | 36,874 frames / 614.6 s |
| B (`x=700`) | WB green; EB/NB/SB red | `BUS_R4_WB_A_SB_020`, WB straight | 25.0 px | 0.0 | 35,954 frames / 599.2 s |

The Node A bus is in lane index `1`, while its active left-turn leg requires
entry lane `2`. `Bus.update()` sets `must_hold_for_lane` while it cannot reach
the required lane and the base vehicle logic stops it near the stop bar
([vehicle.py](../vehicle.py#L359), [vehicle.py](../vehicle.py#L207)). This is a
directly supported blocker.

The Node B bus is in the correct straight-through lane but still has speed
`0.0`. The current telemetry does not identify its exact lead vehicle or
receiving-space predicate, so downstream spillback is the strongest explanation
but is not proven to the same standard as the Node A lane mismatch.

## 5. Confirmed failure chain

```text
R4 bus approaches each node and receives TSP
                      |
                      v
Node A bus cannot complete its required lane change;
Node B bus is stopped behind traffic / without usable receiving space
                      |
                      v
Both buses remain upstream even though WB is green
                      |
                      v
PRIORITY_ACTIVE has no deadline or no-progress escape
                      |
                      v
Both nodes retain WB green; EB, NB and SB remain red
                      |
                      v
119 of 120 vehicles become stopped upstream
                      |
                      v
Agent sees optimistic free-flow ETA and a locked grant,
so it cannot withdraw the active priority
                      |
                      v
Auto discharge remains IDLE because START DISCHARGE was never pressed
```

### 5.1 The request timeout stops applying after activation

Queued and transitioning requests are validated by `_request_is_live()`, which
checks `expires_at_frame`. In `_priority_update()`, however, active and clearing
requests only check whether the bus still exists. The `PRIORITY_ACTIVE` branch
waits indefinitely for `is_front_bumper_upstream()` to become false
([signal_controller.py](../signal_controller.py#L1418)).

This is the primary controller defect. The two active requests are well past
their original request deadlines, but expiration is deliberately bypassed once
the grant becomes active.

### 5.2 The agent is locked to the controller's unsafe terminal condition

`read_minimap()` labels an accepted grant `GRANTED - do not change`; then
`check_locked()` and `anti_cheat()` overwrite the model's output for that route
with the current live flags ([agent.py](../agent.py#L334),
[agent.py](../agent.py#L633)). That protects a bus already entering an
intersection from an unsafe mid-movement revocation, but it assumes every
accepted grant will terminate. The assumption is false here.

The agent made 56 logged decisions (turns 12–67). It enabled R4 TSP on 46 turns
and R1 TSP on 44 turns; turns 43–64 continuously requested both. This reinforced
the queue, but a safe controller must remain recoverable even under repetitive
valid requests.

The last three `HELD_ALL_OFF` turns are not evidence of a new Ollama failure.
The current telemetry is paused, and `load_save()` intentionally treats paused
telemetry as stale and writes the all-off hold. `ai_control.json` was then set to
disarmed. These actions happened after the gridlock was already established.

### 5.3 “Auto” is selection, not automatic detection

`control_panel.py` sets `discharge_start_requested=true` only when the operator
presses `START DISCHARGE` ([control_panel.py](../control_panel.py#L555)). The
controller consumes that command and calls `_start_discharge()`, which safely
cancels priority before recovery ([signal_controller.py](../signal_controller.py#L505),
[signal_controller.py](../signal_controller.py#L771)).

There is no automatic gridlock detector that starts this sequence merely because
the dropdown contains `Auto (Recommended)`. Therefore the captured
`active=false` state is expected from the present code, even though the label can
reasonably lead an operator to expect autonomous activation.

## 6. Immediate operational countermeasure

1. **Export the incident before RESET.** The current implementation now deletes
   both session JSONL logs on RESET. Use `EXPORT ALL` while the incident is still
   preserved.
2. Keep the simulator paused while selecting `Auto (Recommended)` and press
   **START DISCHARGE**.
3. Resume at `1.0x`. A discharge command cannot progress while paused because
   its transition timers advance only in simulation updates.
4. Verify telemetry changes to `network_discharge.active=true` and the controller
   moves through `TRANSITION_YELLOW` / all-red into a protected stage. The agent
   should stand down automatically while recovery owns the network.
5. Confirm `vehicles_discharged` increases and the 119-vehicle queue falls.
6. If recovery reaches `RECOVERY_FAILED`, or remains without progress beyond its
   configured stall interval, use RESET as the last resort. Do not force a green
   across an occupied conflict box.
7. Re-arm the LLM only after discharge completes and normal phase cycling has
   resumed.

For this westbound lock, the existing `Westbound Corridor` plan is structurally
appropriate because it starts with **Node A downstream**, then coordinates Node
A and Node B westbound. `Auto` should select the same downstream-first logic
from the live blockage.

## 7. Code countermeasures, in priority order

### P0 — Add a safe active-priority no-progress watchdog

Add two limits to each accepted request:

- an absolute maximum `PRIORITY_ACTIVE` duration; and
- a shorter no-progress interval based on the holder bus's stop-bar distance.

When either expires, do **not** jump directly to another green. Mark the request
with a specific terminal reason such as `ACTIVE_STALL_TIMEOUT`, cancel through
`RECOVERY_ALL_RED`, wait for the conflict box to clear, and then return to normal
or hand ownership to discharge.

The progress measure should be geometric distance reduction, not speed alone,
because a bus can legitimately stop briefly while still completing a safe
movement.

### P0 — Reject an unusable grant before making it active

Before transitioning from clearance to `PRIORITY_ACTIVE`, confirm:

- the bus is in the required entry lane for the current leg;
- the target lane change is complete;
- the downstream receiving path has storage; and
- the intended movement can actually enter under current reservations.

If the checks fail, retain all-red only for the bounded safety clearance, deny or
defer the request, and serve a movement that creates the required receiving
space. This prevents a green being dedicated to a bus that is physically unable
to use it.

### P0 — Trigger recovery from controller evidence, not model judgment

Automatic recovery should start when the signal controller observes a strong
condition such as:

- active priority exceeds its no-progress limit; or
- at least four approaches remain stopped near storage capacity for a sustained
  interval while the active holder makes no geometric progress.

Recent network throughput alone is not a sufficient trigger: this incident
still reports 111 pax/min because westbound occasionally discharges while every
other movement is starved.

The automatic trigger should invoke the existing collision-safe discharge entry
path. It must never bypass yellow/all-red or reservation checks.

### P1 — Give the agent live obstruction context

Change the minimap to include:

- `eta_to_stop_bar_sec_live` alongside free-flow ETA;
- current speed and lane index;
- required entry lane;
- active-priority age; and
- an explicit `stalled_priority_holder` field from the controller.

Do not allow the LLM to break an active grant directly. Instead, replace the
unqualified `GRANTED - do not change` message with a state that tells the model
that safety ownership belongs to the controller watchdog/recovery path.

### P1 — Reset the complete signal controller state on RESET

The current RESET path calls only `signals.reset_discharge()`. It does not reset
the controller frame counter, per-node priority state, queues, reservations, or
request sequence. The 11,300-frame offset in this capture is direct evidence
that the simulation and signal-controller clocks were not restarted together.

Add a full controller reset API that recreates clean `NodeState` objects, clears
priority and reservation state, resets controller/discharge clocks and counters,
and republishes an idle status. This is required for a truly reproducible
same-seed armed-vs-disarmed benchmark.

### P2 — Clarify the recovery UI

Rename `Auto (Recommended)` to something like `Automatic Plan Selection`, or add
help text stating: `Selects the recovery sequence; press START DISCHARGE to run`.
If autonomous detection is implemented, expose a separate toggle such as
`AUTO-START ON GRIDLOCK` so operator expectations match behavior.

## 8. Required regression tests

1. `test_active_priority_stall_times_out_safely` — a granted bus remains
   upstream and motionless; the controller enters recovery all-red within the
   configured bound rather than holding green indefinitely.
2. `test_active_priority_progress_resets_stall_watchdog` — slow but measurable
   movement does not cause a false cancellation.
3. `test_tsp_not_activated_until_bus_is_in_required_lane` — the R4 Node A bus
   cannot monopolize WB while still blocked in lane 1 for a lane-2 left turn.
4. `test_priority_waits_for_downstream_storage` — a bus with no receiving space
   does not receive an unusable active green.
5. `test_directional_starvation_starts_auto_discharge` — several saturated
   approaches plus a stalled priority holder trigger the existing safe recovery
   entry path.
6. `test_auto_recovery_preserves_conflict_safety` — no conflicting movement is
   green and no vehicle is authorized into an occupied conflict box during the
   watchdog transition.
7. `test_agent_minimap_surfaces_live_eta_and_priority_age` — the prompt no longer
   presents a stopped bus as 0.4 seconds from service without the 100-second live
   ETA beside it.
8. `test_full_reset_restarts_signal_controller_state` — frame count, priority,
   queues, reservations, discharge state and request sequence return to their
   initial values.

## 9. Verdict

The current network is effectively gridlocked because two westbound TSP grants
have no active-state termination path when their buses cannot cross the stop
bars. The permanent greens create directional starvation, fill almost every
approach, and keep 99.2% of vehicles stopped. The LLM's locked-route behavior and
optimistic ETA presentation sustain the condition, while the available discharge
controller remains idle until explicitly started.

The safest immediate action is **EXPORT ALL → START DISCHARGE with Auto selected
→ resume at 1.0x**. The durable fix is a controller-owned, collision-safe
priority stall watchdog combined with lane/downstream feasibility checks and a
full signal-state reset.

