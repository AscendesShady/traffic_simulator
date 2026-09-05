# Traffic Simulator

A desktop traffic and transit simulation of two connected signalized intersections. The project combines a Pygame network canvas, a Tkinter operator panel, and a live telemetry dashboard to model traffic demand, bus priority, spillback, and controlled gridlock recovery.

## Highlights

- Two coordinated intersections: Node A and Node B.
- Six configurable approaches: eastbound, westbound, and north/south traffic at each node.
- Poisson, binomial, negative-binomial, and congestion-peak demand models.
- Signal sequencing with green, yellow, and all-red clearance intervals.
- Transit Signal Priority (TSP) and Dynamic Bus Lane (DBL) requests through a node-specific safety arbiter.
- Optional whole-network Ollama/LangGraph control of all twelve TSP/DBL flags through a strict all-off safety guard.
- Automatic or operator-selected network discharge for gridlock recovery.
- Smoothly resizable telemetry that scales its content, with uniform summary cards, session-only trends, and phase-cycle visualization.
- Regression tests for routes, callbacks, collision prevention, priority, telemetry, and discharge behavior.

The AI controller runs in a separate process so model latency cannot block the 60 Hz simulation. It communicates through atomic JSON snapshots, and malformed model output is replaced with a complete all-off decision before it reaches the live route configuration.

## Signal-controller architecture

The application creates one `SignalController`, which owns two distinct per-node state objects. Node A and Node B independently maintain their phase, timer, clearance, reservations, and TSP/DBL requests; they may start aligned but can diverge under node-local traffic or priority. The legacy `controller.phase` and `controller.timer` assignment properties broadcast setup values to both nodes, but normal production updates operate per node. Historical specifications that describe the shared-clock versus independent-clock choice as unresolved are superseded by the current implementation.

## Requirements

- Python 3 with Tkinter support
- Dependencies listed in `requirements.txt`
- Ollama installed and running locally, with at least one downloaded model, to use AI control

The current application is developed and tested on Windows. Tkinter is included with the standard Windows Python installer and should not be installed from PyPI.

## Quick start

Clone the repository and open PowerShell in its directory:

```powershell
git clone https://github.com/AscendesShady/traffic_simulator.git
cd traffic_simulator
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

Launching `main.py` opens the simulation canvas and control panel, then starts the telemetry dashboard and LLM agent as separate child processes. The simulator remains usable in manual mode when Ollama is unavailable.

## Using the simulator

The control panel can:

- enable or disable traffic approaches;
- select a demand distribution and arrival rate;
- configure and dispatch buses on six routes;
- enable DBL or TSP behavior;
- select an installed Ollama model, choose a 2–15 second decision interval, and arm or disarm whole-network AI control;
- pause, resume, and reset the simulation; and
- start automatic or manually selected gridlock discharge.

The telemetry dashboard reads the latest atomic JSON snapshot and maintains bounded time-series history only in memory. It can be resized from any edge; fonts, cards, diagrams, and charts compact automatically to fit the available window instead of exposing dashboard scrollbars. Closing the dashboard clears its history. `traffic_state_telemetry.json`, `ai_control.json`, `decision.json`, log files, and the `runtime/` directory are generated locally and intentionally excluded from Git.

When AI control is armed, its next validated decision owns every route's TSP and DBL flags. Disarm it before making lasting manual flag changes. Missing or malformed model output cannot authorize priority; the agent and sim-side guard fall back to all flags off.

## Tests

Run the complete regression suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

If Windows denies pytest access to its default temporary directory, use a project-local directory:

```powershell
New-Item -ItemType Directory -Force .\runtime | Out-Null
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.\runtime\pytest-temp
```

Compile-check the application modules:

```powershell
.\.venv\Scripts\python.exe -m py_compile agent.py guard.py canvas_gemini.py control_panel.py main.py signal_controller.py telemetry_dashboard.py telemetry_exporter.py vehicle.py
```

## Project structure

| File | Purpose |
| --- | --- |
| `main.py` | Application entry point, fixed-step loop, spawning, and cross-module orchestration |
| `agent.py` | Separate-process LangGraph turn loop and Ollama call |
| `guard.py` | Strict model-output extraction, validation, rejection logging, and all-off fallback |
| `canvas_gemini.py` | Authoritative road geometry and Pygame rendering |
| `control_panel.py` | Tkinter controls, shared configuration, and operator callbacks |
| `signal_controller.py` | Normal phases, TSP/DBL arbitration, safety clearance, and discharge sequencing |
| `vehicle.py` | Vehicle and bus routing, movement, following, and conflict behavior |
| `telemetry_exporter.py` | Atomic telemetry snapshot generation |
| `telemetry_dashboard.py` | Live metrics, in-memory trends, and phase visualization |
| `audits/` | Historical, incident, callback, and step-by-step audit reports |
| `excel_exports/` | Session Excel exports generated when the simulator closes |
| `tests/` | Automated route, safety, callback, priority, discharge, and telemetry checks |

## Documentation

- [Simulator guide and complete source documentation](TRAFFIC_SIMULATOR_GUIDE_AND_DOCUMENTATION.md)
- [Audit report index](audits/README.md)
- [Audit and step-by-step fix report](audits/TRAFFIC_SIMULATOR_AUDIT_AND_STEP_BY_STEP_FIX_REPORT.md)
- [Gridlock incident report](audits/TRAFFIC_SIMULATOR_GRIDLOCK_INCIDENT_REPORT.md)
- [Pre-integration callback and placeholder audit](audits/TRAFFIC_SIMULATOR_CALLBACK_AND_PLACEHOLDER_AUDIT.md)

## Scope

This project is a simulation and experimentation tool. It is not a certified traffic-signal controller and must not be used to operate real infrastructure.
