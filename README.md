# Traffic Simulator

A desktop traffic and transit simulation of two connected signalized intersections. The project combines a Pygame network canvas, a Tkinter operator panel, and a live telemetry dashboard to model traffic demand, bus priority, spillback, and controlled gridlock recovery.

## Highlights

- Two coordinated intersections: Node A and Node B.
- Six configurable approaches: eastbound, westbound, and north/south traffic at each node.
- Poisson, binomial, negative-binomial, and congestion-peak demand models.
- Signal sequencing with green, yellow, and all-red clearance intervals.
- Transit Signal Priority (TSP) and Dynamic Bus Lane (DBL) requests through a node-specific safety arbiter.
- Automatic or operator-selected network discharge for gridlock recovery.
- Resizable, two-axis scrollable telemetry with session-only trends and phase-cycle visualization.
- Regression tests for routes, callbacks, collision prevention, priority, telemetry, and discharge behavior.

The LLM selector and **RUN LLM** control are intentional UI placeholders; they do not call an external model or alter signal policy.

## Signal-controller architecture

The application creates one `SignalController`, which owns two distinct per-node state objects. Node A and Node B independently maintain their phase, timer, clearance, reservations, and TSP/DBL requests; they may start aligned but can diverge under node-local traffic or priority. The legacy `controller.phase` and `controller.timer` assignment properties broadcast setup values to both nodes, but normal production updates operate per node. Historical specifications that describe the shared-clock versus independent-clock choice as unresolved are superseded by the current implementation.

## Requirements

- Python 3 with Tkinter support
- Dependencies listed in `requirements.txt`

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

Launching `main.py` opens the simulation canvas and control panel, then starts the telemetry dashboard as a child process.

## Using the simulator

The control panel can:

- enable or disable traffic approaches;
- select a demand distribution and arrival rate;
- configure and dispatch buses on six routes;
- enable DBL or TSP behavior;
- pause, resume, and reset the simulation; and
- start automatic or manually selected gridlock discharge.

The telemetry dashboard reads the latest atomic JSON snapshot and maintains bounded time-series history only in memory. It can be resized from any edge; use the mouse wheel for vertical scrolling and Shift+wheel for horizontal scrolling on either tab. Closing the dashboard clears its history. `traffic_state_telemetry.json` and the `runtime/` directory are generated locally and intentionally excluded from Git.

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
.\.venv\Scripts\python.exe -m py_compile canvas_gemini.py control_panel.py main.py signal_controller.py telemetry_dashboard.py telemetry_exporter.py vehicle.py
```

## Project structure

| File | Purpose |
| --- | --- |
| `main.py` | Application entry point, fixed-step loop, spawning, and cross-module orchestration |
| `canvas_gemini.py` | Authoritative road geometry and Pygame rendering |
| `control_panel.py` | Tkinter controls, shared configuration, and operator callbacks |
| `signal_controller.py` | Normal phases, TSP/DBL arbitration, safety clearance, and discharge sequencing |
| `vehicle.py` | Vehicle and bus routing, movement, following, and conflict behavior |
| `telemetry_exporter.py` | Atomic telemetry snapshot generation |
| `telemetry_dashboard.py` | Live metrics, in-memory trends, and phase visualization |
| `tests/` | Automated route, safety, callback, priority, discharge, and telemetry checks |

## Documentation

- [Simulator guide and complete source documentation](TRAFFIC_SIMULATOR_GUIDE_AND_DOCUMENTATION.md)
- [Audit and step-by-step fix report](TRAFFIC_SIMULATOR_AUDIT_AND_STEP_BY_STEP_FIX_REPORT.md)
- [Gridlock incident report](TRAFFIC_SIMULATOR_GRIDLOCK_INCIDENT_REPORT.md)

## Scope

This project is a simulation and experimentation tool. It is not a certified traffic-signal controller and must not be used to operate real infrastructure.
