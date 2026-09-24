# Simulation Input Parameters

Source: `control_panel.py` (as of the current working tree). These are the default configuration values the simulator loads at launch and uses to drive vehicle generation, turn behavior, and bus dispatch. All values are live-editable through the control panel UI while the sim is stopped or running; the tables below reflect the code defaults.

## 1. Runtime / global state (`global_config`)

| Key | Default value | Meaning |
|---|---|---|
| `is_paused` | `False` | Freeze/resume flag, independent of run lifecycle |
| `sim_speed` | `1.0` | Simulation speed multiplier (range 0.5x–3.0x) |
| `green_time` | `240` | Signal green phase duration, in frames |
| `random_seed` | `None` | `None` = OS entropy (non-reproducible); integer = reproducible traffic generation |
| `reset_triggered` | `False` | One-shot flag to clear vehicles mid-run |
| `start_requested` | `False` | One-shot flag: START begins a fresh run from frame zero |
| `is_running` | `False` | Sim launches idle; becomes `True` only after START |
| `run_has_started` | `False` | Distinguishes "never started" from "stopped after a run" |
| `discharge_selection` | `DISCHARGE_AUTO` | Default discharge-mode corridor selection |
| `discharge_start_requested` | `False` | Discharge-mode start flag |
| `discharge_stop_requested` | `False` | Discharge-mode stop flag |
| `vehicle_speed_scale` | `0.5` | Desired-speed multiplier applied on START; sets the ITE change intervals too |
| `movement_model` | `"idm"` | Car-following/lane-change engine: `"idm"` (calibrated, default) or `"legacy"` (comparison only) |
| `signal_change_intervals` | `"ite"` | Yellow/all-red from the ITE (2020) formulas at the 85th-percentile desired speed, MUTCD-bounded; `"legacy"` = 1 s + 1 s |
| `signal_coordination` | `"coordinated"` | Common cycle + progression offset + per-cycle transition; `"independent"` = own Webster cycle per node |
| `coordination_direction` | `"EB"` | Direction the Node A to Node B offset progresses |
| `bus_dwell` | door 4.0 s, board 3.0 s/pax, alight 2.0 s/pax, mean 4 on / 4 off, 2 doors | TCQSM 3rd ed. Ch. 6 dwell model at each route stop |
| `fcd_period_s` | `0` | Floating-car-data sampling period (sim s); `0` = off |
| `priority_eligibility_px` | `400` (100 m) | TSP eligibility zone before a node; panel slider 250–1400 px (62.5–350 m), clamped to the 500 m link |
| `max_cycle_sec` | `150.0` | Longest cycle Webster may choose, at any flow ratio (NCHRP 812 large-intersection range; 120 s cost 3-10 % more person-hours at 0.7-1.0x campaign demand, 180 s gained under 1 %) |
| `warmup_discard_frames` | `36000` (600 s) | Discarded before steady-state DVs; end of the network fill by MSER-5 (campaign 3e9df990) |

### `discharge_runtime` (nested)

| Key | Default value |
|---|---|
| `active` | `False` |
| `selected` | `DISCHARGE_AUTO` |
| `status` | `"IDLE"` |
| `reason` | `"Normal signal control is active"` |
| `recommendation` | `"Select Auto or a corridor, then start discharge"` |
| `stage` | `""` |
| `vehicles_discharged` | `0` |

### `ai_runtime` (nested)

| Key | Default value |
|---|---|
| `armed` | `False` |
| `model` | `"None"` |
| `tick_seconds` | `10` (`DEFAULT_TICK_SECONDS`; slider 2–120 s on both run cards; an arm skipping > 5 % of its decision points is flagged) |
| `last_status` | `"INACTIVE"` |
| `last_turn` | `0` |

## 2. Vehicle flow / distribution per approach (`approach_configs`)

Drives per-source arrival generation each frame, independently (Poisson, Binomial, Neg Binomial or Congestion Peak; the processes are defined in `TRAFFIC_SIMULATOR_METHODOLOGY_AND_ARCHITECTURE.md` §7).

| Approach | Node / direction | Active | Arrival model | Rate (veh/min) | Straight | First left | Second left | Heavy-vehicle ratio |
|---|---|---|---|---|---|---|---|---|
| `EB` | EB Corridor | Yes | Binomial | 34 | 75% | 14% (left @A) | 11% (left @B) | 10% |
| `WB` | WB Corridor | Yes | Binomial | 32 | 80% | 10% (left @B) | 10% (left @A) | 10% |
| `A_NB` | Node A (NB) | Yes | Poisson | 24 | 75% | 25% (left @A) | — | 15% |
| `A_SB` | Node A (SB) | Yes | Poisson | 27 | 75% | 15% (left @A) | 10% (left @A then @B) | 15% |
| `B_NB` | Node B (NB) | Yes | Poisson | 25 | 75% | 15% (left @B) | 10% (left @B then @A) | 15% |
| `B_SB` | Node B (SB) | Yes | Poisson | 22 | 75% | 25% (left @B) | — | 15% |

Field definitions:
- **rate** — vehicles/minute spawned on that approach; inter-arrival times drawn from a Poisson process. Adjustable 0–60 v/min in the panel.
- **turn_split** — straight share of arrivals. **left_far_share** — share taking the approach's *second* left option (`control_panel.APPROACH_TURN_OPTIONS`: the left at the far node for EB/WB, a double left for A_SB/B_NB); the rest take the first left option. Set together on one three-segment bar in the panel (two thumbs; single-option approaches A_NB/B_SB have one thumb and no far share). A far-node left-turner runs with through traffic and merges into lane 2 within 250 px of its node; a double-left car exits its first turn already in the exit road's lane 2.
- **heavy_ratio** — probability a spawned vehicle is a truck (heavy) instead of a car. Adjustable 0–50%.
- **active** — whether the approach is currently generating traffic (toggle in panel).

## 3. Vehicle passenger occupancy (`vehicle.py`)

| Vehicle type | Passengers |
|---|---|
| Car | 4 |
| Truck (heavy) | 1 |

## 4. Bus routes (`bus_routes_config`)

6 fixed routes, each with a scheduled dispatch headway and fixed waypoint/lane geometry through the two signal nodes (Node A and Node B as listed in NETWORK_GEOMETRY.md).

<!-- BEGIN GENERATED ROUTES -->
| Route ID | Name | Origin to destination | Waypoints | Lanes | Stops | Active | Headway | TSP | DBL | Manual dispatch |
|---|---|---|---|---|---|---|---|---|---|---|
| `R1_EB_A_NB` | EB → Node A (NB) | EB to NODE_A_NB | 1400: LEFT | 1400: 2 | 1400: far-side | Yes | 180s | No | No | No |
| `R2_EB_B_NB` | EB → Node B (NB) | EB to NODE_B_NB | 1400: STRAIGHT, 3400: LEFT | 1400: 1, 3400: 2 | 1400: far-side | Yes | 240s | No | No | No |
| `R3_EB_ONLY` | EB Corridor (Straight) | EB to EB_CORRIDOR | 1400: STRAIGHT, 3400: STRAIGHT | 1400: 1, 3400: 1 | 1400: far-side | Yes | 300s | No | No | No |
| `R4_WB_A_SB` | WB → Node A (SB) | WB to NODE_A_SB | 3400: STRAIGHT, 1400: LEFT | 3400: 1, 1400: 2 | 3400: far-side | Yes | 240s | No | No | No |
| `R5_WB_B_SB` | WB → Node B (SB) | WB to NODE_B_SB | 3400: LEFT | 3400: 2 | 3400: far-side | Yes | 240s | No | No | No |
| `R6_WB_ONLY` | WB Corridor (Straight) | WB to WB_CORRIDOR | 3400: STRAIGHT, 1400: STRAIGHT | 3400: 1, 1400: 1 | 3400: far-side | No | 180s | No | No | No |
<!-- END GENERATED ROUTES -->

Field definitions:
- **waypoints** — for each signal node the route passes through, the turn move the bus makes there (`STRAIGHT`/`LEFT`).
- **lanes** — the lane index the bus occupies at each node.
- **headway_sec** — scheduled interval, in seconds, between automatic dispatches on that route (panel slider 0–600 s, `MAX_HEADWAY_SEC`; 0 = no automatic dispatch).
- **tsp_enabled** — Transit Signal Priority; grants the bus priority green requests at nodes (off by default).
- **dbl_enabled** — Dedicated Bus Lane behavior (off by default).
- **manual_dispatch** — if `True`, the route only dispatches on manual trigger rather than automatically on `headway_sec`.

5 of the 6 routes are active by default; the westbound straight-through route (R6) starts disabled.
