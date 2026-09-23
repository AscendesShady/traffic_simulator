# Traffic Simulator — Consolidated Context

> Generated reference distilled from every `.py` module and every file in `docs/`, current as of the `src/` package layout (post `c6b1d6c` restructure). Treat this as a fast-loading map of the codebase; where it and the actual source disagree, the source wins. For narrative depth beyond this file, see `docs/TRAFFIC_SIMULATOR_METHODOLOGY_AND_ARCHITECTURE.md` (methodology in Part I, architecture in Part II).

## 1. What this project is

A desktop traffic and transit simulation of two connected signalized intersections — **Node A** at `x=800` and **Node B** at `x=1600` (`canvas_gemini.INT_X`, on a 2400×600 px surface — never hard-code a node x) — built on Pygame (rendered offscreen into a Tk canvas), Tkinter, and an optional separate-process LLM supervisor (LangGraph over local Ollama, or the Gemini/OpenAI/xAI Grok APIs). It models:

- stochastic demand generation on six approaches (EB, WB, and NB/SB at each node),
- six fixed bus routes with Transit Signal Priority (TSP) and a Dynamic Bus Lane (DBL),
- Webster-derived baseline signal timing, calibrated per run from a measured saturation flow,
- conflict-safe intersection entry (reservations, spillback checks, all-red clearance),
- gridlock discharge (manual or automatic recovery when the network locks up),
- passenger-throughput accounting (car=4, truck=1, bus=45 passengers) as the real objective, not raw vehicle count.

This is a **simulation and research tool**, never a certified traffic-signal controller.

## 2. Repository layout

```
run.py                          # entry point: `from src.core.main import main`
src/
  core/
    main.py                     # composition root, fixed-step loop, spawning, export
    signal_controller.py        # per-node phases, TSP/DBL state machine, discharge, safety
    vehicle.py                  # Vehicle/Bus kinematics, lane changes, spillback
    webster.py                  # pure Webster cycle/green-split math
    guard.py                    # strict validation boundary for LLM output
  ui/
    control_panel.py            # global_config / approach_configs / bus_routes_config + Tk widgets
    canvas_gemini.py             # network geometry constants + Pygame rendering
    telemetry_dashboard.py      # in-process Tk dashboard (Summary/Trends/LLM/Units tabs) + Excel export
  agents/
    agent.py                    # separate-process LangGraph turn loop (Ollama/Gemini/OpenAI/Grok/rule)
    rule_controller.py          # deterministic non-LLM TSP/DBL comparator
  telemetry/
    telemetry_exporter.py       # atomic traffic_state_telemetry.json writer
    bus_event_log.py            # per-bus lifecycle tracking -> Bus Events export sheet
    real_world_units.py         # sim-unit -> HCM real-world unit conversion (display only)
    measure_saturation.py       # offline HCM-style saturation-flow calibration script
  experiments/
    batch_runner.py             # standalone (import-nothing-from-project) batch sweep engine
tests/                          # pytest suite, run with -c tests/pytest.ini
docs/                           # architecture guide, source guide, parameters, UI rulebook, audits
data/, logs/, results/, runtime/  # all gitignored, runtime-generated
```

Every runtime module resolves `BASE_DIR` as `Path(__file__).resolve().parents[2]` and asserts `requirements.txt` exists there, so the package works whether launched via `run.py`, `python -m src.core.main`, or an IDE's direct-run button (each inserts the repo root onto `sys.path` if missing).

## 3. Process topology

One Tk process owns everything except LLM inference:

- **`main.py`** builds one `tk.Tk()` via `build_main_window()` with a `ttk.PanedWindow` of three panes:
  - **Left** — `control_panel.create_dashboard_window(control_pane)` (Tk controls).
  - **Center** — the simulation: Pygame draws each frame onto an offscreen `pygame.Surface` (no native Pygame window), pushed into a Tk `Canvas`/`PhotoImage` via `build_simulation_canvas()`.
  - **Right** — `TelemetryDashboard`, mounted in-process (not a separate window/process).
- **`agent.py`** is the only child `subprocess.Popen` (`sys.executable -m src.agents.agent`), because model inference latency must never block the 60 Hz sim loop. It communicates purely through files.
- One `WM_DELETE_WINDOW` handler (`on_main_window_close`) stops the run, and an `atexit`-registered `cleanup()` terminates the agent subprocess and builds the combined Excel export (`export_session_excel`) — but **only when the run did not already write its own workbook**. `export_test_workbook` sets the module flag `_run_exported_workbook` (cleared by `perform_full_reset`, so each run decides for itself), and `cleanup()` checks it: one workbook per run. A timed test and every batch run go through `export_test_workbook`, whose sheet set is a superset of the session workbook's, so writing both would leave a thinner duplicate beside the full one. A plain untimed run exports nothing of its own and still gets the session workbook.

Older docs describing the dashboard as a separate window/process predate this consolidation — trust `main.py`.

### Window shapes

`WindowShapeController` (in `main.py`) manages three shapes — `compact` (native 1000×600 canvas, computed side-pane widths), `large` (~75% of screen, centered), `maximized` (OS zoomed) — cycled from the control panel header. It resizes side panes via `PanedWindow.sashpos`, and the canvas via `set_target_size`/`push_frame`'s `pygame.transform.smoothscale`. Above `SCALED_PUSH_SKIP_FACTOR`× native pixels, pushes are throttled to every other frame (~15 Hz) to keep the 60 Hz physics loop responsive.

## 4. Run lifecycle

Driven by flags in `control_panel.global_config`, consumed once per Tk callback in `main.simulation_step()`:

- **START** (`start_requested`) — full reset via `perform_full_reset(vehicles, signals, telemetry)`: clears vehicles, `signals.reset_all_state()`, `telemetry.reset_session()`, `reset_session_logs()` (deletes `telemetry_log.jsonl`/`agent_turn_log.jsonl`/`bus_events.jsonl`), zeroes `network_throughput`, recomputes timed-test checkpoint marks, **calibrates saturation flow and Webster splits** (`calibrate_and_apply_webster`), then re-seeds traffic generation (`reset_traffic_generation`). Frame counter returns to 0 only after calibration — calibration itself is not part of the measured run (the sim clock stays frozen at 0 while `calibrating` is true).
- **STOP** (`request_start_stop`) — halts stepping (`is_running=False`) but preserves vehicles/logs/throughput for export.
- **PAUSE/RESUME** (`request_pause_resume`) — freezes/resumes in place; paused wall time never becomes a catch-up burst.
- **RESET** (`reset_triggered`) — identical full-reset helper as START.

Physics runs a **fixed 60 Hz step** (`dt_step = 1/60`); `sim_speed` (0.5×–3.0×) controls how many fixed steps run per wall-clock Tk callback (bounded by `max_steps_per_callback=6`, catch-up capped at `max_catchup_seconds=0.25`s) — it never changes the timestep itself. The Tk canvas push (visible frame) is throttled to ~30 Hz independent of the 60 Hz physics; `root.after(16, simulation_step)` drives ~60 FPS polling regardless of `is_running`.

### Saturation-flow calibration (`main.py`)

`calibrate_saturation_flow()` runs a **headless HCM queue-discharge experiment** before every run: builds a standing queue on a dedicated EB approach (target 30 queued vehicles, `CALIBRATION_TARGET_QUEUE`), releases it under forced green with no new arrivals, discards the first 4 startup vehicles (HCM startup lost time), and takes `3600 / mean_headway` of the rest as veh/hr/lane. It uses its own seeded RNG (`CALIBRATION_SEED_FALLBACK` if none given) and a throwaway `SignalController`, then hands off to `webster.compute_all_nodes()` to publish per-node `webster_splits` into `global_config`.

## 5. Two independent signal nodes (`signal_controller.py`)

`SignalController.__init__` builds `self.nodes = {node_x: NodeState() for node_x in INT_X}` — Node A and Node B each own their own `phase`, `timer`, `priority_state`/`priority_timer`, `active_request`/`request_queue`, `reservations`, and `terminal_history`, and can diverge freely. Legacy `controller.phase`/`controller.timer` **properties** broadcast a setup value to both nodes on write (`for state in self.nodes.values(): state.phase = ...`) and read Node A on get — they are **not** shared runtime storage; production code goes through `get_node_status(node_x)`.

### Normal per-node cycle

Six-phase index cycle, advanced by `_advance_phase`/`_normal_phase_update`:

```
0 EW_GREEN → 1 EW_YELLOW → 2 ALL_RED → 3 NS_GREEN → 4 NS_YELLOW → 5 ALL_RED → repeat
```

`get_green_time(node_x, phase)` reads `global_config["webster_splits"][node_x]["EW_green_frames"|"NS_green_frames"]`, falling back to the legacy `green_time` (240 frames) if uncalibrated. Yellow/all-red durations are constructor-fixed (`yellow_time=60`, `red_clearance_time=60` frames = 1s each by default).

### Priority (TSP) state machine

Per-node fields drive one bounded adjustment at a time — never a direct phase assignment:

- `PriorityRequest` dataclass: `request_id`, `bus`, `route_id`, `route_leg_index`, `node_x`, `originating_approach`, `movement`, `entry_lane`, `conflicting_approaches`, timestamps, `state` (one of `REQUESTED → ARMED → TSP_EXTENDING|TSP_EARLY_TRUNCATE → COMPLETED|DENIED|CANCELLED`), `tsp_action` (`none|extending|early_green`), `tsp_adjust_frames`.
- `_collect_priority_requests` scans buses each controller tick; a bus becomes a request when `is_bus_tsp_eligible` or `is_bus_dbl_eligible` is true and it isn't already suppressed/queued. Terminal requests are added to `suppressed_keys` so a bus sitting in the eligibility zone cannot instantly re-request after denial.
- `_priority_update` per node: prune dead/finished requests (`_prune_queue`), refresh/arm the head of the queue (`_refresh_active_request`), then either continue an in-flight extension/truncation or evaluate `_request_can_start_tsp`:
  - **Extension** (`_begin_extension`): if the bus's approach's green would end this frame and the bus is still upstream, hold the green up to `cap = green * TSP_MAX_ADJUST_FRACTION` (20% of that phase's Webster green, `_tsp_cap_frames`).
  - **Early green** (`_begin_early_green`): if the conflicting phase is currently running, shorten it — bounded below by `MIN_GREEN_FRAMES` (300 frames = 5s) and above by the same 20% cap — then run `_truncation_update` until `node.timer >= tsp_target_end`.
  - Both actions are single, bounded, and reported via `get_node_status()`'s `tsp_action`/`tsp_adjust_frames`, and remembered as `last_tsp_action`/`last_tsp_adjust_frames` even after the event ends (so a short telemetry sampling window cannot miss it).

### Movement conflicts and intersection entry (the collision-safety core)

- `movements_conflict(approach_a, movement_a, approach_b, movement_b)`: perpendicular axes (EW vs NS) always conflict; same-axis opposing approaches conflict only if either is a LEFT turn (square-corner sweep of the shared area), resolved dynamically by `_left_turn_cleared_adjacent_through_lane` / `_straight_vehicle_cleared_left_turn_corner` geometry checks rather than a blanket ban.
- `request_intersection_entry(vehicle, node_x, vehicles)` is the **sole collision-prevention gate**: checks discharge ownership, existing reservations for conflicting movements, and any unregistered occupant in the box, before granting a reservation keyed by `id(vehicle)`. `vehicle.py`'s green-entry gate (`sig_state=="GREEN" and 0<=dist_to_stop<=25`) calls this before letting a vehicle cross the stop bar; a non-green signal or a stop condition calls `cancel_intersection_entry` to release the reservation while still upstream.
- Reservations are cleaned up (`_cleanup_reservations`) once a vehicle is gone or has passed the node.

### DBL (Dynamic Bus Lane) eligibility

`is_bus_dbl_eligible(bus, target_node, all_vehicles)`: route's live `dbl_enabled` flag is true, bus is on the leg targeting that node, bus is **already in lane index 2** (`DBL_LANE_INDEX`), and within `priority_eligibility_px` (a 250–800px, operator-set, **episode-frozen** value — set only at construction/reset so a mid-run slider drag cannot alter a running benchmark). `SignalController` is the sole authoritative answer, used by both priority requests (`_collect_priority_requests`) and `vehicle.py`'s bus lane-migration logic — one source of truth, never duplicated.

### Network discharge (gridlock recovery)

A parallel state machine (`discharge_state` ∈ `INACTIVE, TRANSITION_YELLOW, ALL_RED, WAITING, DISCHARGING, STAGE_YELLOW, STOPPING_YELLOW, STOPPING_ALL_RED, RECOVERY_FAILED`) that, while active, **owns the network**: `_consume_discharge_commands` cancels all live priority requests, suspends new arrivals (checked via `main.is_discharge_demand_suspended`) and normal signal phase advance, and drives one of seven `DISCHARGE_PLAN_STAGES` plans (`Eastbound Corridor`, `Westbound Corridor`, four single-leg node plans). `DISCHARGE_AUTO` mode rotates clockwise (`DISCHARGE_CLOCKWISE_ORDER = N→E→S→W`, skipping empty legs) via `_next_clockwise_candidate`, scored by `_rank_discharge_candidates` (dependency bonus + queue count + fairness-since-last-served). A leg gets `discharge_min_green`–`discharge_max_green` frames; it ends early if its queue clears or after `discharge_stall_time` (180 frames default) with no progress, and the whole recovery enters `RECOVERY_FAILED` (requiring an operator RESET) if no stage becomes servable within the stall window. `get_discharge_status()`/`_publish_discharge_status()` mirror state into `global_config["discharge_runtime"]` for the UI and telemetry.

## 6. Vehicle model (`vehicle.py`)

- `Vehicle`: position `(x,y)`, `direction` (`EB|WB|NB|SB`), `length`/`width` (18×10px car, 28×12px truck), `speed`/`max_speed`, `lane_index` (0/1 straight, 2 = outer/DBL/left-turn), `target_turn`, `passed_nodes` (set — a node counts as passed only once the vehicle's **rear** clears the conflict box, not the stop bar or center), `leg_state` (`APPROACHING→TURNING/IN_INTERSECTION→DEPARTING→COMPLETE`).
- `Vehicle.update(...)` per frame: (1) upstream DBL-yield logic — once DBL is armed, **every** non-bus vehicle sitting in the reserved approach lane is ordered out immediately, ahead of *or behind* the priority bus (not just the ones near it): a vehicle that cannot safely vacate keeps moving if it's ahead of the bus, or holds if it's behind, and retries every frame. This unconditional clearing (see `vehicle.dbl_lane_is_obstructed`/`dbl_lane_queue_ahead` below) replaced an earlier proximity-gated version that only cleared cars within `DBL_CLEAR_AHEAD_PX` (200px) of the bus and stopped checking once the bus was already in-lane — that gap let a stopped car far ahead, or one that entered the lane after the bus, strand the bus with a "clear" lane behind it; (1b) DBL merge-gap cooperation — a merging bus names the single nearest car in its target-lane corridor as `dbl_merge_blocker`, which either vacates (if ahead) or slows to `DBL_MERGE_YIELD_SPEED_RATIO` (if behind); (2) signal yielding + downstream spillback (`is_spillback_blocked`) + `request_intersection_entry`; (3) car-following kinematics (`SAFE_GAP=12px`, accel/decel 0.05/frame); (4) left-turn pivot geometry (turns at a fixed lane offset from the node center, overshoot carried into the new axis); (5) `passed_nodes` update on rear-clear.
- `vehicle.dbl_lane_queue_ahead(bus, all_vehicles, h_y, target_node)` finds stopped/crawling vehicles ahead of a bus in the DBL lane (bounded to the current leg when `target_node` is given); `vehicle.dbl_lane_is_obstructed(...)` treats any such queue as an obstruction **even when the bus is already in the DBL lane** (a queue ahead means reserving the lane cannot remove a standing vehicle in front of the bus), and otherwise falls back to checking the merge corridor for a bus still outside the lane. `signal_controller.is_dbl_requested_for_bus_leg` gates a live DBL request/grant on this obstruction check, so a request is only armed when the lane would actually let the bus move.
- `Bus(Vehicle)`: 42×14px, 45 passengers, `route_id`, `route_nodes` (sorted node list per direction), `get_active_route_leg(int_x_list)` (movement/entry-lane/exit-direction for the next unfinished leg), `get_following_route_leg` (peek at leg after this one). Overrides `update()` to add:
  - **Route-required lane merges** (multi-leg routes like R2/R4 needing a different lane at the next node): reserves a deterministic post-node merge area (`ROUTE_MERGE_AREA_PX=80px` past the previous node) and checks `target_lane_has_merge_storage` **before entering the upstream node**, so a bus never carries an unresolved lane change into the next stop bar. If blocked by a follower, the bus continues to open the gap (only the follower yields, via `should_yield_for_route_merge`); if blocked ahead, it holds at the merge-area boundary (`route_merge_hold_active`) rather than queuing in the middle lane.
  - **DBL merge with bounded (or immediate, sticky) abandonment**: attempts to migrate to lane 2 as soon as `dbl_enabled_for_leg`; if blocked past `DBL_MERGE_ABANDON_FRAMES` (300 frames ≈ 5s), or the instant a live request is vetoed by `dbl_lane_is_obstructed`/a blocked merge (no waiting out the timer in that case), sets `dbl_merge_abandoned_for_leg=True` and settles back into its normal configured lane — **the core invariant: DBL must never leave a bus worse off than baseline**. The flag is sticky for the rest of the leg so the bus doesn't oscillate between requesting and creeping. Route-required merges are never abandoned (the bus needs that lane to complete its route safely) — only a DBL-only merge (`abandonable` check excludes turn-lane and route-required cases). `is_bus_dbl_eligible` also checks this sticky flag before treating the bus as DBL-eligible again.

## 7. Webster timing (`webster.py`) — pure, no side effects

Two-phase (EW/NS) critical-lane method per node: `y = q_lane/S` where `q_lane` is the busiest lane's flow (`critical_lane_fraction`: straight traffic splits across 2 lanes, so busiest lane carries `max(turn_split/2, 1-turn_split)` of approach flow), `S` is the measured per-lane saturation flow. `compute_node_green_splits` derives Webster's optimal cycle `(1.5*L+5)/(1-Y)`, floored at `MIN_CYCLE_SEC=40s`, capped at `OVERSATURATED_CYCLE_CAP_SEC=120s` when `Y>=1` (oversaturated — the fallback cycle is used and `oversaturated=True` is reported rather than solving an undefined optimum). `compute_all_nodes` derives each node's own `{EW: max(EB,WB), NS: max(node's NB,SB)}` — the EW corridor is shared, but each node's NS pair is independent, so **asymmetric demand across the two nodes yields different cycle lengths**.

## 8. Configuration schemas (`control_panel.py`)

Three module-level dictionaries are the single source of operator config, mutated directly by the UI and read everywhere else:

### `global_config` (excerpt — see file for full set)
```python
{
  "is_running": False, "is_paused": False, "sim_speed": 1.0,
  "random_seed": None,                     # None = OS entropy
  "priority_eligibility_px": 500,          # 250-800, frozen at START/RESET
  "vehicle_speed_scale": 0.5,              # 0.25-1.0, pending; "_active_vehicle_speed_scale" is the frozen runtime value
  "test_duration_sim_seconds": None, "test_running": False, "test_model", "test_seed",
  "batch_start_requested": False, "batch_runtime": {...},
  "measured_saturation_flow": None, "webster_splits": {}, "cycle_time_sec": {},
  "discharge_selection": "Auto (Recommended)", "discharge_runtime": {...},
  "ai_runtime": {"armed": False, "model": "None", "tick_seconds": 5, "last_status": "INACTIVE", "last_turn": 0},
  "window_shape": "compact",
}
```

### `approach_configs` — six keys `EB, WB, A_NB, A_SB, B_NB, B_SB`
```python
{"active": True, "model": "Poisson", "rate": 12, "turn_split": 0.80, "heavy_ratio": 0.10}
```
`model` ∈ `Random, Poisson, Binomial, Neg Binomial, Congestion Peak` (see `main.should_spawn_vehicle` for each arrival process — Congestion Peak cycles a 30s peak at up to 4× rate / min 90 vpm followed by a 30s recovery). `rate` is vehicles/min (1–30 in UI, `INFLOW_MIN/MAX_VPM` bounds are 0/60), `turn_split` is P(straight) vs the alternate LEFT move, `heavy_ratio` is P(truck).

### `bus_routes_config` — six routes, keyed by ID
| Route | Origin→Dest | Waypoints (node:move) | Default active | Headway |
|---|---|---|---|---|
| `R1_EB_A_NB` | EB → Node A NB | `{300: LEFT}` | Yes | 30s |
| `R2_EB_B_NB` | EB → Node B NB | `{300: STRAIGHT, 700: LEFT}` | Yes | 45s |
| `R3_EB_ONLY` | EB straight through both | `{300: STRAIGHT, 700: STRAIGHT}` | **No** | 30s |
| `R4_WB_A_SB` | WB → Node A SB | `{700: STRAIGHT, 300: LEFT}` | Yes | 30s |
| `R5_WB_B_SB` | WB → Node B SB | `{700: LEFT}` | Yes | 45s |
| `R6_WB_ONLY` | WB straight through both | `{700: STRAIGHT, 300: STRAIGHT}` | **No** | 30s |

Each also carries `lanes` (entry lane per node), `active`, `tsp_enabled`/`dbl_enabled` (both default False — the LLM/rule/operator turns these on), `manual_dispatch`. `guard.ROUTE_ORDER = sorted(bus_routes_config.keys())` is the one canonical positional order used everywhere a flat array (not a route-ID-keyed dict) crosses a boundary.

### Decision sources / model selection

`get_decision_sources()` = installed Ollama tags (`ollama list`, with an offline fallback list) plus `RULE_BASED_MODEL = "rule-based"` inserted after `"None"`. `get_api_models()` returns Gemini/OpenAI/Grok model IDs only for providers whose API-key env var (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `GROK_API_KEY`) is set (`API_MODEL_REGISTRY`). `set_active_ai_model` writes the one shared `ai_runtime["model"]` slot — Local and API selectors are mutually exclusive. `write_ai_control()` atomically mirrors `{armed, model, tick_seconds (2-15s), simulation_running}` to `data/ai_control.json` for the agent subprocess.

**Two independent decision intervals.** The Single Run card's slider writes `ai_runtime["tick_seconds"]`; the Batch Run card has its own slider writing `batch_runtime["tick_seconds"]`. `_effective_tick_seconds()` picks between them at write time — the batch value whenever `test_running` is true (set by `request_start_test()`, which is the path *both* a single Benchmark Test and every queued Batch Benchmark run take), otherwise the Single Run value. Because the choice happens inside `write_ai_control()` rather than by mutating `ai_runtime`, an unattended sweep never inherits, nor overwrites, the operator's manual Single Run setting.

### Control panel UI sections (top to bottom, all collapsible)
`Approach Traffic`, `Bus Routes`, `Single Run` (AI/LLM model selectors + its own decision-interval slider, then Start/Pause/Reset, sim speed, seed), `Batch Run` (Benchmark Test duration + **its own independent decision-interval slider** + Start test, then the Batch Benchmark seed/model sweep), `Tuning` (priority eligibility 250-800px slider, vehicle speed scale 0.25-1.0x slider), `Gridlock Discharge` (Auto + 6 corridor/leg options). The panel holds operator *inputs* only — Webster's derived timing output moved to the dashboard's Summary tab (§17). Config-tier cards are white-accented, run-tier amber, intervention-tier red (`SECTION_ACCENT_*`). Widget helpers (`make_button`, `make_toggle_chip`, `add_slider_row` with keyboard-steppable `ttk.Scale`, `make_section` disclosure cards) implement `docs/ui-ux-design-rulebook.md`'s state-matrix/contrast/spacing rules — **any control-panel or dashboard UI change should follow that rulebook**.

## 9. Telemetry (`telemetry_exporter.py` → `data/traffic_state_telemetry.json`)

`TelemetryExporter.export(...)` writes every `export_interval_frames` (10, i.e. ~6/sec) via atomic temp-file + `os.replace`. `schema_version: 3` payload top level: `timestamp, frame_number, simulation_time_seconds, simulation_running, simulation_paused, signal_timing (saturation/cycle/webster_splits), signal_state.nodes["300"|"700"] (phase, signals, queues, queues_passengers_est, queue_length_m, downstream_space_m, downstream_blocked, active_request, terminal_history), network_discharge, network_summary, network_throughput, delay (bus/car passenger-hours + LOS-feeding stopped delay), demand_generation, routes (per-route live flags + nearest-bus ETA/obstruction), active_buses, approaching_buses, bus_distribution, vehicle_positions`.

Key derived per-approach fields, all display/decision-support only (never fed back into physics): `queue_length_m` (furthest-back queued vehicle's stop-bar distance, in meters via `real_world_units`), `downstream_space_m`/`downstream_blocked` (free road on the far side of a node — a green into a `blocked` approach would just stall traffic in the box; threshold `2×` queue spacing). Bus records include TSP/DBL lifecycle booleans (`priority_requested/transitioning/granted/clearing`, `tsp_action`, `tsp_adjust_frames`) built from `SignalController.get_priority_status_for_bus` + `get_latest_terminal_status_for_bus`, so the model/UI can distinguish "queued behind another bus" (`REQUESTED`) from "actively being extended/truncated" (`ARMED`/`TSP_EXTENDING`/`TSP_EARLY_TRUNCATE`).

`bus_distribution` (`TelemetryExporter.compute_bus_distribution`) is a network-wide, same-timestamp snapshot of where every bus currently is: `total_buses`, `buses_by_node_approach` (the same node×approach grid the queue tables use, keyed by the node each bus is next headed to and its current direction — so it counts buses still in transit, not just ones queued at a stop bar), `buses_by_route`, and `buses_in_dbl_lane` (buses whose `lane_index == DBL_LANE_INDEX` right now, whatever the reason).

## 10. Cross-process data contract (`agent.py` ⇄ `main.py`)

| File | Writer | Reader(s) | Notes |
|---|---|---|---|
| `data/ai_control.json` | `control_panel.write_ai_control()` | `agent.py` | armed, model, tick_seconds, simulation_running |
| `data/traffic_state_telemetry.json` | `telemetry_exporter.py` | dashboard, agent | schema_version 3, atomic |
| `data/decision.json` | `agent.py`/`guard.py` | `main.merge_ai_decision()` | route-ID-keyed canonical flags, atomic |
| `logs/telemetry_log.jsonl` | `main.log_telemetry_sample` | dashboard/exports | ~1/sec append-only |
| `logs/agent_turn_log.jsonl` | `agent.log_turn` | dashboard/exports | per-turn decision + performance |
| `logs/bus_events.jsonl` | `BusEventTracker._append` | Bus Events export sheet | per-bus lifecycle on completion |
| `logs/agent_rejects.log` | `guard._record_rejection` | developer | best-effort malformed-output evidence |
| `results/*.xlsx` | main/dashboard | operator | session/timed-test/EXPORT ALL workbooks |
| `results/experiment_summary_<YYYYMMDD>.csv` (`main.experiment_summary_path()`, dated by the campaign's start day) | `main.append_experiment_summary_row` | cross-run analysis | **never cleared by RESET** — append-forever master dataset |

All are gitignored and use atomic replace (temp file + `os.fsync` + `os.replace`) so a reader never observes a half-written file.

`main.merge_ai_decision()`: if `ai_runtime["model"]=="None"` there's nothing to merge (baseline run). Otherwise reads `decision.json`, computes `stale_after = max(tick_seconds*3, 12.0)` seconds from the decision's own timestamp; missing/invalid/stale → **all-off flags** + `last_status` set to `STALE_DECISION`/`INVALID_DECISION`/`WAITING_FOR_DECISION`. Only `guard.validate_flags()`-passing, fresh decisions actually flip `bus_routes_config[route]["tsp_enabled"|"dbl_enabled"]`.

## 11. `guard.py` — the trusted boundary (fail-closed)

The **model-facing** schema (no route IDs — positional only, to prevent an LLM inventing/mistyping a route key):
```json
{"reason": "<=40 words", "tsp": [bool×6], "dbl": [bool×6]}
```
`extract_json` pulls the first balanced `{...}` object out of noisy output (strips `<think>` blocks and code fences first). `validate_flags_positional` requires **exactly** `len(ROUTE_ORDER)` strict-`bool` entries in both arrays (an `int` like `1` or a string `"true"` fails) and maps position→`ROUTE_ORDER[i]`. `safe_decision(raw_text, turn, model)` **always** returns a complete decision: on any parse/validation failure it returns `status="HELD_ALL_OFF"`, `flags=all_off_flags()`, empty reason, and best-effort-appends to `agent_rejects.log`. The **internal** `decision.json` format is route-ID-keyed (`validate_flags`, used only by `main.py` reading the file back) — only the guard is allowed to produce that representation from positional model output.

## 12. `agent.py` — the LLM/rule turn loop (separate process)

A `LangGraph` `StateGraph` with nodes `load_save → check_discharge → read_minimap → check_locked → ai_turn → anti_cheat → write_decision` (plus `hold` for stale/paused telemetry, short-circuited before `check_discharge`). Runs forever in `run_forever()`, polling `ai_control.json` every ~1s when disarmed/stopped, else one graph turn per `tick_seconds` (2–15s).

- **`load_save`**: reads telemetry; `STALE` if missing, `simulation_paused`, or older than `STALE_SECONDS=3.0` → routes straight to `hold` (fresh all-off, no model call).
- **`check_discharge`**: if `network_discharge.active`, stands down with an all-off `STANDDOWN_DISCHARGE` decision, skipping inference entirely.
- **`read_minimap`**: builds a structured-text (not image) observation: `DECISION_LAG_SEC` (the previous turn's measured latency, or `DEFAULT_DECISION_LAG_SEC=8.0` on turn 1 — the rule controller instead uses its own `RULE_DECISION_LAG_SEC=0.0`), per-node phase/signals/waiting-passengers/queue-length/downstream-free-space/actionable-bus summary, then per-route (in `ROUTE_ORDER`) active/tsp/dbl/obstruction/nearest-bus-ETA/`eta_at_decision_land_sec` (ETA minus the decision lag — a bus with `eta_at_decision_land_sec<=0` will already be at the node before the flag lands, so granting it is wasted) and `actionable` (`0 < landed_eta < ACTIONABLE_HORIZON_SEC=45s`).
- **`check_locked`**: routes with a bus the controller holds a request for — `priority_requested` (armed, waiting on the early-green gate), `priority_granted` or `priority_clearing` — are added to `locked_routes` and appended to the minimap as `LOCKED_ROUTES=...`; the model is told to leave them as shown, and `anti_cheat` re-forces those routes' flags to their current live state regardless of what any arm returns. A route flag is a *continuous hold*: `SignalController._request_is_live` re-reads it every frame and cancels the request the frame it drops (`FEATURE_DISABLED`), then suppresses that bus's leg so it never re-requests. Locking only granted/clearing requests let every arm (the deterministic rule included: 9 of its 9 TSP denials) withdraw most of its grants mid-approach as the snapshot changed — cross-street load ticking past 45 pax, or the bus's landed ETA reaching 0 at the bar. Every arm decides *new* requests only.
- **`ai_turn`**: dispatches by model-name prefix — `is_rule_model` (rule_controller), `gemini-*`, `gpt-*`, `grok-*` (openai SDK against `GROK_BASE_URL`), else Ollama. Each backend call runs in a **daemon thread with a hard timeout** (`GEMINI_TIMEOUT_SECONDS=30`, `OPENAI_TIMEOUT_SECONDS=30`, `GROK_TIMEOUT_SECONDS=30`, `OLLAMA_TIMEOUT_SECONDS=45`) and a non-reentrant lock per provider (a still-running call from a timed-out previous turn can't overlap the next); a timeout/exception becomes `status="INVALID"` and a synthetic error string that `guard.safe_decision` will reject to all-off.
- **`anti_cheat`**: `guard.safe_decision(...)`, then the locked-route overlay described above, then a DBL safety overlay: any *new* (non-locked) `dbl=True` grant is forced back to `False` server-side when telemetry shows that route's DBL lane has a queue ahead or is otherwise obstructed (`dbl_lane_queue_ahead`/`dbl_lane_obstructed`) — a model cannot grant DBL into a lane that would just strand the bus, even if it ignores `SYSTEM_PROMPT`'s instruction to the same effect.
- **`write_decision`**: atomic write to `decision.json` + `log_turn` append to `agent_turn_log.jsonl` (includes minimap, raw output, computed `tokens_per_sec`, etc.).

`SYSTEM_PROMPT` is heavily restraint-biased: "Webster is already competent, congestion is failure, default to no grant, at most 1-2 routes per node per turn, compare bus passengers (45) directly against the cross-traffic queue you'd delay, never grant into a blocked downstream, decide fresh each turn from current state only (no carrying flags forward) — except LOCKED_ROUTES, which the controller already holds." `_track_run_boundary` resets the turn counter/decision memory when telemetry's `frame_number` jumps backward (a new run started).

## 13. `rule_controller.py` — deterministic comparator

Selected via the same `ai_runtime["model"]` slot (`RULE_BASED_MODEL = "rule-based"`), so a rule run and an LLM run differ in **nothing except the decision-maker** — same guard, same `decision.json` format, same turn log, same exports (`agent.SYSTEM_PROMPT`/inference is simply swapped for `_call_rule`, which calls `rule_controller.rule_based_decision` and serializes to the same JSON text an LLM would produce).

Logic (conditional actuated, not naive always-grant):
- **DBL**: grant to any route with an approaching bus whose DBL lane isn't obstructed (`dbl_lane_obstructed` telemetry flag) — DBL is a lane reservation, not a signal grant, so no cross-traffic cost gate is needed.
- **TSP**: candidate only if `actionable` (reuses `agent.actionable_buses`/`eta_at_decision_land`/`is_actionable` verbatim, so rule and model judge identically-actionable buses) **and** the conflicting cross-street's queued passengers < `RULE_CROSS_QUEUE_THRESHOLD_PAX=45` (one bus load). Per node, at most `MAX_TSP_GRANTS_PER_NODE=1` grant, ranked by net passenger benefit (`passengers - cross_pax`), then ETA, then route ID for determinism.
- `RULE_DECISION_LAG_SEC=0.0` — the rule decides in microseconds and plans with a near-zero horizon, distinct from a model's measured-latency carry-forward.

## 14. `bus_event_log.py` — per-bus lifecycle instrumentation

`BusEventTracker` builds one record per bus from spawn to network exit (`observe()` called every frame, `complete()` on exit), capturing per-node `arrival_frame`, `tsp_treated`/`tsp_action`/`tsp_adjust_frames` (attributed from the controller's live active request or its `terminal_history` if the grant finalized the same frame the bus cleared), `stop_bar_cross_frame`, `node_wait_frames`. Feeds the "Bus Events" export sheet (`write_bus_events_sheet`, flattened `node1_*`/`node2_*` columns) and `main.build_experiment_summary_row`'s treated-vs-untreated wait-time comparison — the causal evidence that TSP is actually doing something, not just being requested. Every public method swallows its own exceptions so instrumentation can never crash or alter a run.

## 15. `real_world_units.py` — display-only HCM anchoring

Anchors the whole sim to real units on **one measured quantity**: the run's calibrated saturation flow `S_sim` is declared equivalent to `REAL_SATURATION_FLOW_VEH_HR=1900` veh/hr/lane (HCM standard). Everything else derives from `k = S_real/S_sim`: distances via `meters_per_pixel` (one queued-vehicle spacing ≡ `REAL_JAM_SPACING_M=7.5m`), flows/v-c ratio/HCM level-of-service (`hcm_level_of_service`, standard 6th-edition delay thresholds A≤10s...F>80s) as `DERIVED`; speed is `APPROX` (never independently calibrated — "treat as sim-equivalent, not measured"). Each quantity is tagged `EXACT|ANCHOR|DERIVED|APPROX` so a reader knows how much to trust it. Feeds the dashboard's Units tab and the `Unit Conversions` export sheet; **never feeds back into physics**.

## 16. `batch_runner.py` — standalone sweep engine

Deliberately imports nothing from the rest of the project (fully unit-testable without Tk/a live sim). `BatchRunner(set_seed, set_model, start_run)` sequences the `(seed, model)` cross product (seeds outer, models inner — so one seed's full model comparison completes early) through the ordinary single-run timed-benchmark path via injected callbacks; it never reimplements a run, only calls `request_start_test()` and waits for the caller (`main.poll_batch_runner`) to report `report_run_outcome`. A failed run never stops the batch — it's logged and the next run starts — unless it looks like an API rate limit (explicit `rate_limited` flag, or 2 consecutive failures of the same API model), in which case the rest of that model's queued seeds are skipped so a doomed API quota doesn't stall local-model runs. `main.build_batch_runner()` wires the three callbacks by late-bound attribute lookup (not eager binding) specifically so tests can monkeypatch `control_panel.request_start_test` and observe the batch engine calling it.

## 17. `telemetry_dashboard.py` — in-process live monitor + Excel export

Four tabs (custom button-strip tab switcher over a borderless `ttk.Notebook`, since `clam` re-bevels tabs on switch): **Summary** (3×3 KPI grid — vehicles/buses/passengers/queue/delay/congestion/TSP/DBL/timer — plus a gridlock-recovery status card and two live node-phase diagrams), **Trends** (session-only, in-memory bounded time series, cleared on a backward frame/time jump or dashboard rebuild — no historical DB), **LLM** (per-turn status/latency/tokens/throughput cards + optional GPU stats via `poll_gpu_stats`), **Units** (the `real_world_units` conversion table). Polls `traffic_state_telemetry.json` on a timer (`safe_read_telemetry`), reschedules itself in `finally` so one bad read never permanently stops monitoring; `STALE_AFTER_SECONDS=2.0` marks the status line stale.

**Webster timing readout (moved out of the control panel entirely).** The Summary tab owns every derived-timing figure, since it is observed output of the run's calibration rather than an operator input:

- A **WEBSTER SIGNAL TIMING** card (`build_webster_timing_ui`, packed between the phase-cycle chart and the node diagrams) shows the measured lane capacity message and one stacked sub-card per node with its condition (`Optimal`/`Oversaturated`/`Unavailable`), `Cycle length: 62 s · optimal`, `Demand ratio: EW 0.31 · NS 0.20 · Total 0.51`, and — only when oversaturated — the red "Reduce demand or increase vehicle speed." note. Sub-cards are stacked rather than side by side because the demand-ratio line does not fit two-to-a-row in this pane.
- Each node diagram (`create_node_canvas`/`draw_intersection`) carries a **Green time** label directly beneath its signal-color dots, so the signal and its green split stay visually paired.

All of it is refreshed by `_refresh_webster_timing_labels`, called from `draw_node_intersections` on every telemetry poll, and sourced from `control_panel.get_webster_timing_summary()` — still the single source of truth. Labels track their container's live width for wrapping, because Tk's `wraplength` is a fixed pixel budget applied once and does not shrink to fit a card that is later packed narrower.

`export_all()` and `main.py`'s `export_session_excel`/`export_test_workbook` also write an **"AI Decision Audit"** sheet (`main.write_ai_decision_audit_sheet`) alongside the pre-existing "LLM Performance"/"LLM Summary" sheets — see `CLAUDE.md`'s "LLM/rule decision auditing" section for the State→Action+Reasoning→Outcome row shape and the stale-log pitfall its helper (`_add_ai_decision_audit_sheet`) must avoid.

`export_all()` builds one timestamped workbook (`build_excel_export_filename`: `{model}_{runtime}_{seed}seed_{DDMMYYYY_HHMMSS}.xlsx`) with, when data exists: Decisions, Telemetry, LLM Performance, LLM Summary, Control Panel Inputs, Bus Events, Unit Conversions, and native-Excel Session Charts. `main.py`'s own `export_session_excel`/`export_test_workbook` build the same sheet set directly from the crash-safe JSONL logs (so export never depends on the dashboard process being open) — the two paths are kept schema-identical (`DECISION_HEADER_BASE`, `TELEMETRY_HEADER`, `LLM_PERFORMANCE_HEADERS`, `LLM_SUMMARY_HEADERS` duplicated in both files by design).

## 18. `measure_saturation.py`

Offline HCM-style calibration diagnostic — reuses production geometry/vehicle/controller code to sweep speed scales, heavy-vehicle ratios, and seeds and validate the saturation-flow assumption independently. Not part of the runtime import path; run it standalone as a research script, never import it from the live app.

## 19. Safety invariants (do not weaken these in any change)

1. Vehicles act on semantic `RED/YELLOW/GREEN` strings, never rendered RGB.
2. Nodes are independent — never merge Node A/Node B state into one shared clock.
3. Green is necessary but not sufficient — a vehicle also needs downstream storage (`is_spillback_blocked`) and `request_intersection_entry` permission.
4. All-red is a transition, not a direction — it clears conflicting occupants first.
5. Reservations are movement-specific; a new movement enters only if it doesn't conflict with active reservations/occupants.
6. `passed_nodes` means rear-clear completion — never mark it at the stop bar or vehicle center.
7. Priority is a **request** — TSP/DBL never directly sets green; only the state machine executes the bounded adjustment.
8. Discharge owns the network while active — arrivals and LLM priority are suspended until recovery releases control.
9. DBL must never leave a bus worse off than baseline — optional merges have a bounded abandon path; route-required merges never abandon.
10. Model output is untrusted — only strict positional booleans of exactly the canonical route count become canonical flags; anything else is a complete all-off `HELD_ALL_OFF` decision.
11. Freshness is mandatory — a `decision.json` older than `max(3×tick_seconds, 12s)` (or with a missing/invalid timestamp) is rejected to all-off.
12. START/RESET fully clear run state and logs; STOP preserves them for export — distinct, deliberate contracts.
13. Same seed reproduces traffic generation only; an armed run may still diverge because model output can differ.
14. Queue *passenger* counts are estimates (flat per-vehicle-type weighting); *served* passenger counts are exact (each vehicle's real `passengers` value).
15. A route flag is a continuous hold — never narrow `agent.check_locked` back to granted/clearing requests; an armed request whose flag drops is cancelled `FEATURE_DISABLED` and its bus's leg suppressed for good.
16. A summary row that contradicts its own inputs is refused, never written: `main._run_export_assertions` (steady window, passenger/bus/person-hours identities, decision cadence) and `BaselineContaminationError` mark the run `FAILED` and keep the workbook. Person-hours totals are the sum of the rounded parts so the identity holds exactly.
17. Tests never touch a live simulator's files: `conftest.isolate_runtime_files` redirects every `data/`/`logs/`/`results/` path per test, and path defaults are resolved at call time, not bound as default arguments. Still, never run the suite while a batch is in flight.

## 20. Testing

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
.\.venv\Scripts\python.exe -m pytest -c tests\pytest.ini tests -q
.\.venv\Scripts\python.exe -m pytest -c tests\pytest.ini tests -q tests\test_signal_priority.py::test_name
```
If Windows denies pytest's default temp dir: `--basetemp=.\runtime\pytest-temp` (create `runtime/` first).

`tests/conftest.py` provides `base_geometry`, a `signal_system` fixture (a `SignalController` with short 20/3/3-frame timings for fast tests), an autouse `restore_shared_configuration` fixture that deep-copies and restores `control_panel.global_config`/`bus_routes_config`/`approach_configs` around every test — because these are shared mutable module dictionaries, not per-instance state — an autouse `destroy_leftover_tk_root` fixture that destroys any Tk root a test leaves behind, since a live root keeps its interpreter and pending `after` callbacks alive for every later test, and an autouse `isolate_runtime_files` fixture that points every `data/`, `logs/` and `results/` path constant of every module (`main`, `guard`, `agent`, `control_panel`, `bus_event_log`, `telemetry_exporter`, `telemetry_dashboard`) at a per-test temp directory — without it, a test calling `perform_full_reset` deletes the `logs/*.jsonl` of a simulator running in another process and one writing `ai_control.json` flips its agent to observation-only (three of the seven refused rows in the 2026-09-21 batch). Never run the suite while a batch is in flight regardless.

**`--capture=sys` in `tests/pytest.ini` is load-bearing.** pytest's default `--capture=fd` dup2()s file descriptors 1 and 2 onto temp files and swaps them around each test, while Tcl binds its standard channels once per process at first-interpreter init. Swapping the descriptors underneath it dangles that global state, and a later `tk.Tk()` then fails inside its own startup — `couldn't read file .../init.tcl: No error`, `Can't find a usable tk.tcl`, or `invalid command name tcl_findLibrary` — on a random GUI test. Measured on the Tk-heavy subset: 4/10 runs failed under fd capture, 0/15 under sys capture. Never override it back to `--capture=fd`.

Test files map roughly 1:1 to subsystems: `test_vehicle_safety.py` (signals/spillback/turns/lane behavior), `test_signal_priority.py` (TSP/DBL lifecycle), `test_signal_controller_reset.py` (independent-node reset), `test_network_discharge.py` + `test_discharge_clockwise.py` (recovery), `test_adversarial_simulation.py` (cross-module stress), `test_route_completion.py` (six route geometries), `test_runtime_and_telemetry.py`, `test_llm_control_loop.py` (guard/staleness/stand-down/model dispatch), `test_decision_latency.py` (ETA/actionability), `test_dbl_merge_fallback.py`/`test_dbl_obstruction_telemetry.py`, `test_motion_tuning.py`, `test_webster_timing.py`, `test_timed_benchmark.py`, `test_batch_runner.py`, `test_bus_events.py`, `test_rule_controller.py`, `test_real_world_units.py`, plus UI/window-shell tests (`test_control_panel_*`, `test_dashboard_layout.py`, `test_main_window_shell.py`, `test_window_*`, `test_simulation_embedded.py`, `test_telemetry_mounted.py`).

Compile-check (no dedicated lint config):
```powershell
.\.venv\Scripts\python.exe -m py_compile run.py src\agents\agent.py src\core\guard.py src\ui\canvas_gemini.py src\ui\control_panel.py src\core\main.py src\core\signal_controller.py src\ui\telemetry_dashboard.py src\telemetry\telemetry_exporter.py src\core\vehicle.py src\agents\rule_controller.py src\telemetry\real_world_units.py src\telemetry\bus_event_log.py src\core\webster.py
```

## 21. Dependencies

`requirements.txt`: `pygame>=2.5,<3`, `ollama>=0.6,<1`, `google-genai>=1,<2`, `openai>=1,<2` (also serves xAI Grok via its OpenAI-compatible endpoint), `langgraph>=1.2,<2`, `openpyxl>=3.1,<4`, `pytest>=8,<10`. Tkinter ships with the standard Windows Python installer — never `pip install` it. Ollama requires the Ollama service running locally with a pulled model; Gemini/OpenAI/Grok require `GEMINI_API_KEY`/`OPENAI_API_KEY`/`GROK_API_KEY` env vars respectively. The simulator is fully usable with AI disarmed (pure Webster baseline) or with `rule-based` selected in place of any LLM.

## 22. Documentation index

- `docs/TRAFFIC_SIMULATOR_METHODOLOGY_AND_ARCHITECTURE.md` — the single authoritative description: paper-ready methodology (Part I) and implementation reference (Part II). **Read this in full before any non-trivial architectural change.**
- `docs/MODEL_VALIDATION_AND_VERIFICATION_NOTE.md` — validity argument for peer review.
- `docs/SIMULATION_INPUT_PARAMETERS.md` — default config values for demand/approaches/bus routes, with field-by-field definitions.
- `docs/ui-ux-design-rulebook.md` — evidence-based UI/UX rules (contrast, touch targets, state matrices, spacing grid, dark-pattern avoidance); apply when touching `control_panel.py` or `telemetry_dashboard.py`.
- `docs/audits/` — historical incident/audit reports (gridlock, callback/placeholder, priority-starvation, DBL/TSP interaction checks), each dated `YYYY-MM-DD` in the filename. New audits go here; `docs/` root stays current-guides-only.
- `CLAUDE.md` (repo root) — the authoritative, actively-maintained working-agreement file for coding agents in this repo; it is shorter and more prescriptive than this file and should be treated as the final word on process/contracts.
