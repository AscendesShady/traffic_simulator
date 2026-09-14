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
| `tick_seconds` | `5` |
| `last_status` | `"INACTIVE"` |
| `last_turn` | `0` |

## 2. Vehicle flow / distribution per approach (`approach_configs`)

Drives Poisson vehicle spawning per approach, each frame, independently.

| Approach | Node / direction | Active | Arrival model | Rate (veh/min) | Turn split | Heavy-vehicle ratio |
|---|---|---|---|---|---|---|
| `EB` | EB Corridor | Yes | Poisson | 12 | 80% | 10% |
| `WB` | WB Corridor | Yes | Poisson | 12 | 80% | 10% |
| `A_NB` | Node A (NB) | Yes | Poisson | 8 | 75% | 15% |
| `A_SB` | Node A (SB) | Yes | Poisson | 8 | 75% | 15% |
| `B_NB` | Node B (NB) | Yes | Poisson | 8 | 75% | 15% |
| `B_SB` | Node B (SB) | Yes | Poisson | 8 | 75% | 15% |

Field definitions:
- **rate** — vehicles/minute spawned on that approach; inter-arrival times drawn from a Poisson process. Adjustable 1–30 v/min in the panel.
- **turn_split** — probability of the primary turn move vs. the alternate move at the downstream node. Adjustable 0–100%.
- **heavy_ratio** — probability a spawned vehicle is a truck (heavy) instead of a car. Adjustable 0–50%.
- **active** — whether the approach is currently generating traffic (toggle in panel).

## 3. Vehicle passenger occupancy (`vehicle.py`)

| Vehicle type | Passengers |
|---|---|
| Car | 4 |
| Truck (heavy) | 1 |

## 4. Bus routes (`bus_routes_config`)

6 fixed routes, each with a scheduled dispatch headway and fixed waypoint/lane geometry through the two signal nodes (Node A = intersection 300, Node B = intersection 700).

| Route ID | Name | Origin → Destination | Waypoints (node: move) | Lanes (node: lane#) | Active | Headway | TSP enabled | DBL enabled | Manual dispatch |
|---|---|---|---|---|---|---|---|---|---|
| `R1_EB_A_NB` | EB → Node A (NB) | EB → NODE_A_NB | 300: LEFT | 300: 2 | Yes | 30s | No | No | No |
| `R2_EB_B_NB` | EB → Node B (NB) | EB → NODE_B_NB | 300: STRAIGHT, 700: LEFT | 300: 1, 700: 2 | Yes | 45s | No | No | No |
| `R3_EB_ONLY` | EB Corridor (Straight) | EB → EB_CORRIDOR | 300: STRAIGHT, 700: STRAIGHT | 300: 1, 700: 1 | No | 30s | No | No | No |
| `R4_WB_A_SB` | WB → Node A (SB) | WB → NODE_A_SB | 700: STRAIGHT, 300: LEFT | 700: 1, 300: 2 | Yes | 30s | No | No | No |
| `R5_WB_B_SB` | WB → Node B (SB) | WB → NODE_B_SB | 700: LEFT | 700: 2 | Yes | 45s | No | No | No |
| `R6_WB_ONLY` | WB Corridor (Straight) | WB → WB_CORRIDOR | 700: STRAIGHT, 300: STRAIGHT | 700: 1, 300: 1 | No | 30s | No | No | No |

Field definitions:
- **waypoints** — for each signal node the route passes through, the turn move the bus makes there (`STRAIGHT`/`LEFT`).
- **lanes** — the lane index the bus occupies at each node.
- **headway_sec** — scheduled interval, in seconds, between automatic dispatches on that route.
- **tsp_enabled** — Transit Signal Priority; grants the bus priority green requests at nodes (off by default).
- **dbl_enabled** — Dedicated Bus Lane behavior (off by default).
- **manual_dispatch** — if `True`, the route only dispatches on manual trigger rather than automatically on `headway_sec`.

4 of the 6 routes (R1, R2, R4, R5) are active by default; the two straight-through corridor-only routes (R3, R6) start disabled.
