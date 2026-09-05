"""Mechanically regenerate the coding-LLM guide from production source files."""

from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
OUTPUT = PROJECT / "TRAFFIC_SIMULATOR_GUIDE_AND_DOCUMENTATION.md"


SECTIONS = [
    (
        "2. `canvas_gemini.py` — Network geometry and rendering",
        ["canvas_gemini.py"],
        "Defines the authoritative 1000x600 road geometry, semantic-signal-to-RGB boundary, stop bars, signal heads, DBL indicators, labels, and complete Pygame network rendering. It never decides vehicle eligibility or signal safety.",
    ),
    (
        "3. `control_panel.py` — Configuration and operator callbacks",
        ["control_panel.py"],
        "Owns the shared global, approach, six-route, and network-discharge configuration dictionaries and builds the Tkinter control panel with normal desktop stacking. Operators can select Auto or one of six discharge corridors, start recovery, request a safe stop, and read the selected/status/reason/recommendation messages. Its per-approach model selector includes Congestion Peak. Callbacks mutate configuration only. The LLM selector is display-only and RUN LLM is intentionally a no-op.",
    ),
    (
        "4. `main.py` — Application entry point and simulation ownership",
        ["main.py"],
        "Owns the vehicle list, stochastic arrivals, bounded Congestion Peak demand/backlogs, lane-aware bus dispatch, monotonic fixed-step accumulator with bounded work per Tk callback, source-relative child/dashboard paths, drawing, telemetry calls, reset, pause, and shutdown lifecycle. It freezes arrival admission and bus dispatch whenever network discharge is requested or active.",
    ),
    (
        "5. `signal_controller.py` — Signals, conflict reservations, DBL, and TSP",
        ["signal_controller.py"],
        "Owns independent Node A/Node B normal phases, movement reservations, route-leg priority requests, deterministic queues, stable attempt identity, durable terminal history, yellow and all-red interlocks, exclusive priority greens, geometry-aware same-green flow, rear-clear release, and telemetry-facing priority state. It also owns the gridlock-discharge state machine: downstream-first EB/WB staging, automatic safe-candidate ranking, manual corridor objectives, waiting diagnostics, exclusive greens, and safe restoration through yellow/all-red.",
    ),
    (
        "6. `vehicle.py` — Vehicle, bus, route-leg, and movement physics",
        ["vehicle.py"],
        "Implements following, fail-closed signal compliance, spillback checks, intersection-entry reservations, continuous turns, physical-node identity for vertical traffic, rear-clear completion, safe bus lane transitions, upstream merge holding, route-leg identity, and passenger authority.",
    ),
    (
        "7. Telemetry subsystem — Export and dashboard",
        ["telemetry_exporter.py", "telemetry_dashboard.py"],
        "The exporter builds schema-versioned per-node state, exports the controller's exact normal signal timing, separates pending/active/clearing priority, exports per-source congestion demand and the complete network-discharge status, and atomically replaces a source-relative JSON file. The dashboard validates that schema, distinguishes LIVE/PAUSED/STALE/ERROR, always reschedules polling, displays road/demand queues and active/pending grants separately, renders each node independently, and shows the recovery selection, status, reason, recommendation, stage, and discharge count. Both notebook tabs are scrollbar-free and responsive: debounced resize handling scales fonts, cards, diagrams, and charts to the available window, while a uniform 3-by-3 grid keeps all summary metric cards equal. Below the summary cards, a three-band nominal phase-cycle diagram shows east-west, Node A north-south, and Node B north-south timing, outlined all-red intervals, a wrapping time marker, and authoritative live-state dots that expose priority or discharge divergence. Its Session Trends tab samples only advancing LIVE frames into bounded process memory and plots occupancy, queue pressure, and congestion without creating a history file; reset, manual clear, or dashboard close discards the samples.",
    ),
]


def source_block(filename):
    source = (PROJECT / filename).read_text(encoding="utf-8").rstrip()
    return f"### Full source: `{filename}`\n\n```python\n{source}\n```\n"


def build_guide():
    parts = [
        "# Traffic Simulator Guide and Source Documentation",
        "",
        "**Purpose:** Explain the simulator architecture, runtime behavior, safety contracts, file responsibilities, and complete embedded Python source so a coding LLM can reconstruct or inspect the application from one document.",
        "",
        "Companion documents:",
        "",
        "- [Audit and Step-by-Step Fix Report](audits/TRAFFIC_SIMULATOR_AUDIT_AND_STEP_BY_STEP_FIX_REPORT.md)",
        "- [Gridlock Incident Report](audits/TRAFFIC_SIMULATOR_GRIDLOCK_INCIDENT_REPORT.md)",
        "",
        "## 1. General overview",
        "",
        "This guide is a source-synchronized description of the traffic simulator. `main.py` is the executable entry point; the remaining files are imported modules except `telemetry_dashboard.py`, which `main.py` launches as a child process.",
        "",
        "### Canonical signal-controller architecture",
        "",
        "The architecture decision is closed: the current simulator uses independent per-node state (the former Option B). There is one `SignalController` network-coordinator object, but `controller.nodes[300]` and `controller.nodes[700]` are distinct `NodeState` objects with independent phase, timer, request queue, active priority request, clearance, reservation, and terminal-history state. Both nodes start aligned by default, but node-local occupancy or priority may make their clocks and signals diverge. `get_all_signals()` computes a separate signal map for each node.",
        "",
        "The compatibility properties `controller.phase` and `controller.timer` do not represent shared runtime storage. Their getters expose Node A, while their setters broadcast a setup value into both distinct node objects for legacy callers and tests. Current production updates iterate and update the nodes independently. Any `tsp_dbl_hardcoding_spec.md` copy that presents shared versus independent clocks as an unresolved choice is a superseded pre-F-13 planning artifact and is not authoritative for this checkout.",
        "",
        "The network has Node A at x=300, Node B at x=700, horizontal center y=300, three 22-pixel lanes per direction, 132-pixel roads, and a 10-pixel stop offset. `canvas_gemini.py` is the authoritative geometry/rendering boundary.",
        "",
        "Communication flow:",
        "",
        "```text",
        "control_panel configuration and callbacks",
        "                |",
        "                v",
        "main fixed-step loop -> SignalController per-node state",
        "                |             |",
        "                v             v",
        "          Vehicle / Bus movement and conflict reservations",
        "                |",
        "                +-> canvas rendering",
        "                +-> atomic telemetry JSON -> telemetry dashboard",
        "```",
        "",
        "Safety contracts:",
        "",
        "- Unknown, missing, RGB, or malformed signal values fail closed as RED in vehicle physics.",
        "- Normal left turns reserve the conflict box and remain exclusive until their rear clears.",
        "- DBL and TSP do not directly write colors. They submit a bus/route-leg/node/lane request to one arbiter per node.",
        "- Priority transitions through conflict yellow, all-red clearance, exclusive approach green, rear-clear holding, and recovery all-red.",
        "- Normal and priority green begin only after all-red has cleared the intersection box.",
        "- During an active green, compatible same-axis traffic may enter continuously; it does not wait for the entire node rectangle to become empty.",
        "- A long left turn protects only its genuinely shared adjacent corner lane, releasing following through or left-turn traffic as soon as that corner is clear and normal following can maintain separation.",
        "- A request at Node A never changes Node B state, and vice versa.",
        "- North/south vehicles remain pinned to the physical node whose vertical road they occupy; clearing one node cannot complete the remote node.",
        "- DBL lane authority comes from the active route leg: through legs currently use lane 1 and left legs lane 2.",
        "- Duplicate eligible frames preserve one priority request ID; timed-out requests become durable terminal denials and require a new eligibility edge before retry.",
        "- Pending DBL/TSP is distinct from a granted or rear-clearing priority interval in telemetry, dashboard metrics, and DBL lamps.",
        "- Dashboard trend history is process-local and bounded to 600 samples. Duplicate, paused, and stale snapshots do not add samples, and a backwards frame/time jump clears the previous run.",
        "- The latest telemetry JSON remains a snapshot rather than JSONL; no time-series history is persisted to disk.",
        "- Telemetry exports green, yellow, all-red, and nominal-cycle frame counts so the dashboard phase diagram stays synchronized with the controller's configured timing.",
        "- The phase-cycle background is the nominal plan. Its marker dots are the authoritative live states; blue indicates mixed approaches or node divergence during priority operation.",
        "- The telemetry dashboard starts within the available screen, remains freely resizable, and automatically compacts fonts, cards, diagrams, and charts without adding dashboard scrollbars.",
        "- The nine summary metric cards use a uniform three-column by three-row grid, so their widths and heights remain equal at every supported window size.",
        "- Runtime and telemetry paths are resolved from the source directory, not the caller's working directory.",
        "- The control panel uses normal window stacking rather than forced topmost behavior, so the canvas can be raised or overlapped normally.",
        "- Catch-up work is capped per Tk callback so a delayed simulation update does not make native window dragging unresponsive.",
        "- Congestion Peak alternates 30 simulated seconds of oversaturated demand with 30 seconds of recovery for each selected approach.",
        "- Peak demand is the greater of four times the rate-slider value or 90 vehicles/minute; recovery uses the configured slider value.",
        "- Blocked congestion arrivals remain as an integer backlog, capped at 5,000 per source, and become vehicle objects only when the spawn boundary is safe.",
        "- Reset and model changes clear congestion backlogs so stale demand cannot flood a restarted scenario.",
        "- Network discharge suspends arrivals, pending-demand admission, automatic/manual bus dispatch, and DBL/TSP arbitration while recovery owns the signal schedule.",
        "- Auto discharge ranks only currently safe movements, prioritizes downstream dependencies and larger queues, and reevaluates after every protected stage.",
        "- Manual discharge selects the operator's recovery objective, but it waits and recommends another action when the requested conflict box or receiving lane is unavailable.",
        "- EB recovery opens Node B before coordinating Node A; WB recovery opens Node A before coordinating Node B.",
        "- Every discharge green is exclusive per node and begins only after yellow, minimum all-red, conflict-box clearance, and receiving-space validation.",
        "- Safe Stop returns to normal timing only after yellow, minimum all-red, and both conflict boxes are empty.",
        "- Collision avoidance, following distance, spillback prevention, and intersection reservations remain authoritative during discharge.",
        "- The LLM controls are placeholders: model selection changes only a label and RUN LLM intentionally does nothing.",
        "",
        "Run and verify:",
        "",
        "```powershell",
        ".\\myenv\\Scripts\\python.exe main.py",
        ".\\myenv\\Scripts\\python.exe -m py_compile canvas_gemini.py control_panel.py main.py signal_controller.py telemetry_dashboard.py telemetry_exporter.py vehicle.py",
        ".\\myenv\\Scripts\\python.exe -m pytest -q",
        "```",
        "",
    ]
    for title, filenames, purpose in SECTIONS:
        parts.extend([f"## {title}", "", "Purpose:", "", purpose, ""])
        for filename in filenames:
            parts.extend([source_block(filename), ""])
    return "\n".join(parts).rstrip() + "\n"


if __name__ == "__main__":
    OUTPUT.write_text(build_guide(), encoding="utf-8")
    print(f"Wrote {OUTPUT}")
