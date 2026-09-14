# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A desktop traffic and transit simulation of two connected signalized intersections (Node A at x=300, Node B at x=700), built on Pygame (rendered offscreen into a Tk canvas), Tkinter, and an optional separate-process LLM supervisor (LangGraph over local Ollama or the Gemini API). It models demand generation, six fixed bus routes with Transit Signal Priority (TSP) and a Dynamic Bus Lane (DBL), Webster-derived signal timing, conflict-safe intersection entry, and gridlock discharge/recovery. This is a simulation and research tool, not a certified traffic-signal controller — never treat it as one.

## Commands

```powershell
# Setup (Windows; Tkinter ships with the standard Python installer, do not pip-install it)
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Run the app (opens the unified Tk window; spawns src/agents/agent.py as a child process)
.\.venv\Scripts\python.exe run.py

# Full test suite (pytest.ini lives in tests/, so pass -c explicitly from the repo root)
.\.venv\Scripts\python.exe -m pytest -c tests\pytest.ini tests -q

# Single test file / single test
.\.venv\Scripts\python.exe -m pytest -c tests\pytest.ini tests -q tests\test_signal_priority.py
.\.venv\Scripts\python.exe -m pytest -c tests\pytest.ini tests -q tests\test_signal_priority.py::test_name

# If Windows denies pytest its default temp dir, use a project-local one instead
New-Item -ItemType Directory -Force .\runtime | Out-Null
.\.venv\Scripts\python.exe -m pytest -c tests\pytest.ini tests -q --basetemp=.\runtime\pytest-temp

# Compile-check the application modules (no dedicated lint config exists)
.\.venv\Scripts\python.exe -m py_compile run.py src\agents\agent.py src\core\guard.py src\ui\canvas_gemini.py src\ui\control_panel.py src\core\main.py src\core\signal_controller.py src\ui\telemetry_dashboard.py src\telemetry\telemetry_exporter.py src\core\vehicle.py src\agents\rule_controller.py src\telemetry\real_world_units.py src\telemetry\bus_event_log.py src\core\webster.py
```

Ollama (local) requires Ollama installed and running with at least one model pulled; Gemini requires a `GEMINI_API_KEY` environment variable. The simulator is fully usable without either — baseline Webster control runs with AI disarmed, or with `rule_controller.py`'s deterministic TSP/DBL rule selected in place of an LLM.

## Architecture

### Process topology (current — one Tk process + one LLM subprocess)

`main.py` builds **one** Tk window (`build_main_window()`) with three panes, all in a single process:

- **Left pane** — `control_panel.py`'s Tk controls, mounted via `control_panel.create_dashboard_window(control_pane)`.
- **Center pane** — the simulation: Pygame draws each frame onto an offscreen `pygame.Surface` (no native Pygame window is created), which is pushed into a Tk `Canvas`/`PhotoImage` via `build_simulation_canvas()`.
- **Right pane** — `telemetry_dashboard.TelemetryDashboard`, mounted in-process into the pane rather than run as its own Tk root.

Only `agent.py` (the LLM turn loop) still runs as a separate `subprocess.Popen` child, because model inference latency must never block the 60 Hz simulation loop. One `WM_DELETE_WINDOW` handler on the single real window drives shutdown: it stops the run, terminates the agent subprocess, and triggers the combined Excel export via `atexit`.

Note: `docs/TRAFFIC_SIMULATOR_ARCHITECTURE_AND_REUSE_GUIDE.md` and `docs/README.md` predate this consolidation in places (they still describe the telemetry dashboard as a separate child process/window) — trust `main.py` over those documents for process topology. Everything else in that architecture guide (module ownership, safety invariants, data contracts, per-file responsibilities) remains an accurate, detailed reference and is worth reading before non-trivial changes.

### Module ownership (authoritative owner → consumers)

| Concern | Owner | Consumers |
|---|---|---|
| Road geometry | `canvas_gemini.py` | `main.py`, `vehicle.py`, `signal_controller.py`, telemetry |
| Operator configuration (`global_config`, `approach_configs`, `bus_routes_config`) | `control_panel.py` | everything else |
| Vehicle collection & simulation clock | `main.py` | controller, exporter, renderer |
| Vehicle kinematics & route progress | `vehicle.py` | `main.py`, controller, exporter |
| Right-of-way & collision safety | `signal_controller.py` | vehicles, renderer, telemetry |
| Baseline green splits | `webster.py`, applied by `main.py` | controller, telemetry, UI |
| Live state serialization | `telemetry_exporter.py` | dashboard, agent |
| LLM output validation | `guard.py` | agent/main merge boundary |
| LLM policy loop | `agent.py` | writes a suggestion only; never moves vehicles |
| Deterministic non-LLM TSP/DBL comparator | `rule_controller.py` | same decision path as the LLM, selected via `ai_runtime["model"]` |
| Real-world unit conversion (display/export only) | `real_world_units.py` | telemetry dashboard, exports |
| Bus TSP/DBL event tracking for export | `bus_event_log.py` | Excel export |

**The central invariant:** the LLM (or the rule comparator) may only *request* route flags. `SignalController` is the sole safety authority — it decides collision checks, all-red clearance, reservations, and spillback protection. Never let a decision source bypass the state machine to set green directly.

### Two independent signal nodes

`SignalController` holds `self.nodes = {node_x: NodeState() for node_x in INT_X}` — Node A and Node B each have their own phase, timer, clearance, reservations, and TSP/DBL request queue, and can diverge under node-local traffic or priority. The legacy `controller.phase`/`controller.timer` properties broadcast setup values to both nodes but are not shared runtime storage; don't reintroduce a shared clock. Normal cycle per node: `EW_GREEN → EW_YELLOW → ALL_RED → NS_GREEN → NS_YELLOW → ALL_RED → repeat`. Priority (TSP/DBL) is a request that flows through its own state machine (`NORMAL → REQUESTED → CONFLICT_YELLOW → ALL_RED_CLEARANCE → PRIORITY_ACTIVE → PRIORITY_CLEARING → RECOVERY_ALL_RED → NORMAL`), never a direct phase assignment.

### Cross-process data contract (agent.py ⇄ main.py)

- `ai_control.json` (written by `control_panel.py`, read by `agent.py`) — armed state, selected model/rule, tick interval, sim-running flag.
- `traffic_state_telemetry.json` (written atomically by `telemetry_exporter.py`, read by dashboard and agent) — the authoritative snapshot.
- `decision.json` (written by `agent.py`/`guard.py`, read by `main.py`) — the latest guarded, canonical (route-ID-keyed) TSP/DBL flags.
- `telemetry_log.jsonl`, `agent_turn_log.jsonl`, `agent_rejects.log`, `bus_events.jsonl` — append-only observability logs consumed by exports.

All of these plus `excel_exports/*.xlsx` are runtime-generated, gitignored, and use atomic replace (`os.replace` after writing to a temp file) so a reader never observes a half-written file. `main.py` rejects a `decision.json` whose timestamp is missing, invalid, or older than `max(3 * agent_tick_seconds, 12s)`, and falls back to all-off. **Model output is untrusted**: `guard.py` accepts only two boolean lists of exactly the canonical route count (`ROUTE_ORDER = sorted(control_panel.bus_routes_config.keys())`); anything else (wrong length, non-bool, malformed JSON, exceptions) becomes a complete all-off `HELD_ALL_OFF` decision, logged to `agent_rejects.log`. Preserve this fail-closed behavior in any change to the LLM/rule path.

### Run lifecycle

- **START** — full reset (clears vehicles/controller/logs/throughput/spawner state), recalibrates saturation flow and Webster splits, applies seed/speed, begins at frame 0. Shared helper: `perform_full_reset(vehicles, signals, telemetry)`.
- **STOP** — halts stepping but preserves vehicles/logs/throughput for export.
- **PAUSE/RESUME** — freezes/resumes in place.
- **RESET** — same full-reset helper as START.

Physics is a fixed 60 Hz step (`dt_step = 1/60`); `sim_speed` controls how many fixed steps run per wall-clock tick (bounded catch-up), not the timestep itself. Rendering to the Tk canvas is throttled to ~30 Hz independently of the 60 Hz physics.

### Passenger-throughput objective

This simulates passenger throughput, not just vehicle count: a car carries 4 passengers, a truck 1, a bus 45 (`vehicle.py`). Throughput is credited only when a vehicle exits the screen *and* its `passed_nodes` set is non-empty; `passed_nodes` is only marked once a vehicle's rear has cleared the conflict box (not at the stop bar or vehicle center). Preserve this distinction — don't collapse it to a simple vehicle count.

### Six bus routes and DBL/TSP

Routes are defined once in `control_panel.bus_routes_config` (`R1_EB_A_NB`, `R2_EB_B_NB`, `R3_EB_ONLY`, `R4_WB_A_SB`, `R5_WB_B_SB`, `R6_WB_ONLY`; R3/R6 disabled by default). A DBL-eligible bus must have its route flag enabled, be approaching the correct leg/node, occupy the required DBL lane (lane index 2), and be within the configured eligibility distance — `SignalController` is the sole authoritative answer for eligibility, used by both priority requests and bus lane migration. DBL merges that are optional may be abandoned after a bounded wait (DBL must never leave a bus worse off than baseline); route-required merges are never abandoned since the bus needs that lane to complete its route safely.

### Rule-based comparator (`rule_controller.py`)

Selecting the rule model name (`control_panel.RULE_BASED_MODEL`) in the same `ai_runtime["model"]` slot the LLMs use runs a deterministic conditional-actuated TSP/DBL rule instead of a model, through the exact same guard/decision/telemetry/export path — this makes rule-vs-LLM a clean paired comparison of decision quality, not mechanism. Keep any change to the LLM-facing schema mirrored here if it must stay a fair comparator.

## Key files at a glance

| File | Role |
|---|---|
| `main.py` | Composition root: builds the unified Tk window, owns the vehicle list and fixed-step loop, spawns/tears down the agent subprocess, START/STOP/RESET orchestration, decision merge, session export |
| `control_panel.py` | `global_config` / `approach_configs` / `bus_routes_config` schemas and the Tk control widgets |
| `canvas_gemini.py` | Network geometry constants and Pygame rendering (no right-of-way logic) |
| `vehicle.py` | `Vehicle`/`Bus` kinematics, lane changes, spillback checks, route progression |
| `signal_controller.py` | Per-node phase/timer state, TSP/DBL request state machine, reservations, discharge/recovery — the safety authority |
| `webster.py` | Pure Webster cycle/green-split calculation, no side effects |
| `rule_controller.py` | Deterministic non-LLM TSP/DBL decision source, same output contract as the LLM path |
| `telemetry_exporter.py` | Builds and atomically writes `traffic_state_telemetry.json` |
| `telemetry_dashboard.py` | In-process Tk dashboard: summary/trends/LLM performance tabs, Excel export |
| `bus_event_log.py` | Per-bus TSP/DBL event tracking feeding the Excel export |
| `real_world_units.py` | Anchors sim units (frames, pixels, saturation flow) to real-world HCM quantities for display/export only — never feeds back into the simulation |
| `guard.py` | Strict positional validation of model output; fail-closed to all-off |
| `agent.py` | Separate-process LLM (Ollama/Gemini) or rule turn loop; writes `decision.json` through the guard |
| `measure_saturation.py` | Offline HCM-style saturation-flow calibration script, not part of the runtime import path |

## Documentation in this repo

- `docs/TRAFFIC_SIMULATOR_ARCHITECTURE_AND_REUSE_GUIDE.md` — deep architecture/reuse reference (see the process-topology caveat above).
- `docs/TRAFFIC_SIMULATOR_GUIDE_AND_DOCUMENTATION.md` — full source documentation.
- `docs/SIMULATION_INPUT_PARAMETERS.md` — default config values for demand, approaches, and bus routes.
- `docs/audits/` — historical incident/audit reports (gridlock, callback/placeholder, priority-starvation). Add new audits here, dated `YYYY-MM-DD` in the filename; keep `docs/` to current guides only.
- `docs/ui-ux-design-rulebook.md` — UI/UX implementation rules; apply these when changing the Tkinter control panel or telemetry dashboard.

## Working in this codebase

- Nodes are independent (`self.nodes[node_x]`) — never merge Node A/B state back into a shared clock.
- Change signal/right-of-way behavior only in `signal_controller.py`, together with the `vehicle.py` request contract; never let a vehicle or the LLM path set green directly.
- Change LLM-facing schema in `agent.py` and `guard.py` together, and check whether `rule_controller.py` needs the same change to stay a valid comparator. Keep `decision.json`'s internal route-ID-keyed format canonical.
- Change telemetry fields in `telemetry_exporter.py`, `telemetry_dashboard.py`, the agent minimap, and export mappings together, bumping `schema_version` for breaking changes.
- Preserve atomic writes (`os.replace` after a temp-file write) for every cross-process JSON file.
- Preserve START/RESET full-reset semantics and STOP's log/vehicle preservation — they have distinct, deliberate contracts.
- After a movement/signal change, run the full test suite plus a manual windowed run; check `traffic_state_telemetry.json` for the expected shape.
