# Gridlock Audit Report — 2026-09-08 21:44:14 BDT

## Technical summary

**Verdict: confirmed total network gridlock (critical severity, high confidence).** At the latest captured state, 147 of 149 vehicles were queued (98.7%), all eight buses were stopped, both intersections had remained all-red for more than four minutes, and no passenger had completed a trip for 249.2 simulated seconds.

This incident was **not caused by an active LLM, TSP, DBL, or discharge grant**. The agent was armed with model `None`; all 125 recorded turns were `HELD_ALL_OFF`; every route flag was false; both nodes reported `priority_state=NORMAL`; and network discharge remained `INACTIVE` for the entire final state.

The immediate deadlock mechanism is:

1. A vehicle remains inside each intersection conflict box because downstream storage is unavailable.
2. The normal signal controller is in all-red and refuses to advance until the conflict box becomes completely empty.
3. The in-box vehicles cannot clear because downstream queues cannot move without coordinated downstream service.
4. No automatic trigger transfers control to the existing network-discharge controller.

The primary engineering fix should therefore be **downstream-storage admission control before intersection entry**, backed by a **bounded all-red/gridlock watchdog that automatically invokes downstream-first recovery**. The recently added priority feasibility and active-grant watchdogs should remain, but they cannot resolve this incident because no priority request was active.

## The run stopped making progress at 376 seconds

The telemetry log contains 546 samples covering frame 64 through frame 37,477. The live snapshot was written at 21:44:14 BDT and reports frame 37,511 (625.183 simulated seconds). It also reports `simulation_running=false`, so this is a fresh, stopped incident snapshot rather than a currently advancing run.

| Milestone | Frame | Sim time | Served passengers | Recent pax/min | Vehicles | Queued |
|---|---:|---:|---:|---:|---:|---:|
| Node A enters its final sustained all-red interval | ~21,242 | ~354.0 s | ~4,675 | ~790 | ~70 | ~39 |
| Node B enters its final sustained all-red interval | ~22,301 | ~371.7 s | ~4,828 | ~557 | ~74 | ~59 |
| Last observed throughput increase | 22,560 | 376.0 s | 4,885 | 558.8 | 82 | 73 |
| Recent throughput reaches zero | 24,357 | 405.95 s | 4,885 | 0.0 | 137 | 134 |
| Network first reaches 149 vehicles | 28,908 | 481.8 s | 4,885 | 0.0 | 149 | 146 |
| Final logged sample | 37,477 | 624.617 s | 4,885 | 0.0 | 149 | 147 |

The exact final phase timers provide higher-resolution onset estimates than the five-second agent samples:

- Node A: all-red phase timer `16,269` frames = **271.15 seconds** continuously waiting.
- Node B: all-red phase timer `15,210` frames = **253.50 seconds** continuously waiting.
- No completed trip after 376.0 seconds: **249.18 seconds without throughput** at the 625.183-second snapshot.

The network continued accepting or retaining traffic after throughput stopped: occupancy rose from 82 vehicles at the last completion to 149 vehicles, while the queue rose from 73 to 147.

## Every approach is saturated

Final queue counts are network-wide rather than isolated to one corridor:

| Approach | Queued vehicles | Estimated queued passengers |
|---|---:|---:|
| Eastbound corridor | 27 | 108 |
| Westbound corridor | 34 | 136 |
| Node A northbound | 21 | 84 |
| Node A southbound | 22 | 88 |
| Node B northbound | 21 | 84 |
| Node B southbound | 22 | 88 |
| **Total** | **147** | **588** |

The run used a combined configured demand of **146 vehicles/minute** before scheduled buses:

- EB 22 veh/min (Poisson)
- WB 23 veh/min (Binomial)
- Node A NB 26 veh/min (Binomial)
- Node A SB 25 veh/min (Poisson)
- Node B NB 22 veh/min (Binomial)
- Node B SB 28 veh/min (Negative Binomial)

This is a severe oversaturation test. The demand level explains why downstream storage disappeared, but a safe simulator should degrade into controlled queuing or recovery rather than an indefinite all-red deadlock.

## Conflict-box occupancy is holding both nodes all-red

The final controller state at both intersections is:

| Field | Node A (x=300) | Node B (x=700) |
|---|---:|---:|
| Phase index | 2 | 2 |
| Displayed phase | ALL_RED | ALL_RED |
| Priority state | NORMAL | NORMAL |
| Active priority request | None | None |
| Queued priority requests | 0 | 0 |
| Reservations | 1 | 1 |
| All-red timer | 16,269 frames | 15,210 frames |

In `SignalController._normal_phase_update`, phases 2 and 5 cannot advance while `is_intersection_clear(node_x, vehicles)` is false. That check rejects any vehicle rectangle overlapping the conflict box and has no maximum wait or escalation path. Therefore its timers can grow indefinitely.

The telemetry directly identifies one blocked conflict-box occupant:

- `BUS_R4_WB_A_SB_048` is stopped inside Node B.
- Position `(630.5, 333.0)`, speed `0.0`.
- `leg_state=IN_INTERSECTION`.
- Stop-bar distance `-166.5 px`, confirming that its front has passed the stop line.
- Node B still holds one reservation.

No bus overlaps Node A's conflict box in the exported bus list. Because Node A nevertheless remains all-red and has one retained reservation, its blocking occupant is most likely a car or truck. The current telemetry does not export individual non-bus positions or reservation identities, so the exact Node A vehicle cannot be named from saved evidence.

## The LLM and priority controller did not cause or mitigate this incident

The agent evidence is unambiguous:

- 125 logged turns, numbered 1–125.
- Model was `None` on every turn.
- Status was `HELD_ALL_OFF` on every turn.
- Zero turns contained an active TSP or DBL flag.
- Final `decision.json` has all twelve route-feature booleans false.
- The control file says `armed=true`, `model=None`, `tick_seconds=5`.

The label `armed=true` is therefore misleading operationally: the process was armed, but no model was selected and the fail-safe correctly emitted all-off decisions. This should be treated as a UI/configuration validation issue, not as the gridlock cause.

The discharge subsystem also remained idle:

- `network_discharge.active=false`
- `controller_state=INACTIVE`
- `vehicles_discharged=0`
- arrivals and priority were not suspended

“Auto (Recommended)” is only the selected discharge mode; it is **not an automatic gridlock detector**. The operator must still press START DISCHARGE. In this run, no recovery request reached the controller.

## Root-cause assessment

### Confirmed causal chain

```text
Very high demand fills downstream lanes
                ↓
Vehicles enter conflict boxes without durable exit storage
                ↓
At least one vehicle stalls inside each node
                ↓
Normal phase reaches all-red and waits for an empty box
                ↓
Downstream queues require coordinated green service to move
                ↓
No automatic recovery takeover occurs
                ↓
Both all-red timers grow indefinitely; throughput becomes zero
```

### What the evidence establishes

- Gridlock is network-wide and persistent, not a temporary red interval.
- Both nodes are blocked by physical conflict-box occupancy.
- Downstream saturation is the most likely reason the occupants cannot clear.
- The normal controller has no bounded escape from occupied all-red.
- No LLM priority action contributed to this specific incident.

### What the evidence cannot establish

- The identity, type, movement, and downstream blocker of Node A's occupant.
- The exact vehicle-to-vehicle dependency chain outside the eight exported buses.
- Whether a manual Auto discharge, if started before STOP, would have completed or entered `RECOVERY_FAILED`.
- The configured random seed; it is not exported in the telemetry snapshot.

## Recommended correction plan

### P0 — prevent vehicles entering without exit storage

Strengthen intersection admission before `request_intersection_entry` succeeds:

1. Determine the vehicle's actual exit direction and destination lane for the current movement.
2. Require enough receiving-lane storage beyond the conflict-box boundary for the vehicle's full length plus safe following gap.
3. Reserve that downstream slot together with the intersection movement reservation.
4. Reject entry while the slot is unavailable, even if the approach signal is green.
5. Release both reservations only after the vehicle's rear clears the conflict box and occupies the receiving lane.

This is the primary prevention mechanism. A green authorizes movement only when the vehicle can clear the intersection; it must not authorize entry into a box with no usable exit.

### P0 — add bounded all-red escalation

Add a normal-controller watchdog for phases 2 and 5:

- Start tracking when all-red has met its normal clearance duration but the box remains occupied.
- If occupancy persists for a bounded interval (for example 180 frames / 3 seconds), classify the node as blocked rather than waiting forever.
- Suspend new arrivals and priority requests.
- Identify the blocking occupant's entry movement, exit direction, and downstream dependency from its reservation.
- Transfer control to the existing protected network-discharge state machine, selecting the downstream movement required to create storage first.
- Never jump directly from occupied all-red to a conflicting green.
- If no safe recovery stage makes geometric progress within the existing discharge stall limit, expose `RECOVERY_FAILED` and require RESET.

### P0 — automatically trigger recovery for objective gridlock

Add a conservative network detector. A recommended trigger is all of:

- recent passenger throughput is zero for at least 30 seconds;
- at least one node is beyond its bounded all-red occupancy threshold;
- queued vehicles exceed 80% of network vehicles or a calibrated absolute threshold;
- the simulation is running and not paused;
- discharge is not already active.

This avoids treating every ordinary clearance interval as gridlock while guaranteeing that a 250-second zero-throughput condition cannot remain unattended.

### P1 — meter demand before physical storage is exhausted

At 146 configured veh/min plus buses, this network reaches its 149-vehicle practical storage limit. Introduce network admission metering based on receiving-lane occupancy, not only spawn-point clearance. Excess demand should remain in an external/source backlog counter rather than materializing inside the finite road geometry.

### P1 — export blocker identities and dependency information

For every node, telemetry should include:

- conflict-box occupant vehicle ID and type;
- entry approach and movement;
- intended exit direction/lane;
- current speed and last-progress frame;
- reservation age;
- downstream-storage-blocked boolean and blocking vehicle ID;
- continuous all-red blocked duration.

This would turn the Node A diagnosis from an inference into a directly auditable fact.

### P1 — reject “armed with no model”

The control panel should either prevent arming while model=`None` or display a clear `BASELINE / NO MODEL` state. The guard behaved safely, but 125 failed model turns add noise to the research log and can mislead the operator into believing AI control is active.

### P2 — add a seeded gridlock regression gate

Create a deterministic high-demand test that asserts:

- no vehicle remains inside either conflict box beyond a bounded duration;
- normal all-red never exceeds its clearance plus watchdog threshold without recovery activation;
- automatic recovery either restores throughput or reaches explicit `RECOVERY_FAILED`;
- no conflicting movements receive green during recovery;
- passenger throughput resumes after successful recovery;
- no collisions or overlapping reservations occur.

Run this test with TSP/DBL both disabled and enabled. The present incident occurred with all flags off, so a priority-only regression is insufficient.

## Immediate operator action

1. **Export the session before pressing START or RESET.** The simulator is currently stopped and the captured logs are intact.
2. Treat this run as a failed oversaturation episode beginning around 354–376 simulated seconds.
3. Start the next run with a fixed seed and reduced aggregate demand while the prevention/recovery fixes are pending.
4. For a deliberate stress test, monitor all-red duration and invoke Auto discharge before stopping the run; record whether it recovers or reports `RECOVERY_FAILED`.

Because START is being changed to create a clean run, START should not be used as a way to resume this frozen incident after export—it will intentionally clear the run.

## Further questions for the corrective build

- Which exact vehicle and downstream lane held Node A's reservation?
- Does Auto discharge select the correct downstream-first dependency when both nodes contain occupants simultaneously?
- What sustainable demand envelope keeps the two-node network below physical storage saturation?
- Should excess demand be represented as source backlog rather than additional on-canvas vehicles?

## Evidence scope

This report uses the following artifacts as captured on 2026-09-08 at 21:44:14 BDT:

- `traffic_state_telemetry.json` — current authoritative snapshot.
- `telemetry_log.jsonl` — 546 session samples.
- `agent_turn_log.jsonl` — 125 agent turns.
- `decision.json` — final all-off decision.
- `ai_control.json` — armed/no-model/stopped control state.
- `signal_controller.py`, `vehicle.py`, and `control_panel.py` — current state-machine, movement, and recovery logic.

This is a descriptive and diagnostic audit of one captured run. It does not prove that the recommended fixes are collision-free; those changes require implementation plus seeded adversarial simulation tests.
