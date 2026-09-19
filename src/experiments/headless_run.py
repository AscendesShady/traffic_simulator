"""Headless replay of main.py's fixed-step loop, without Tk, pygame video or
the agent subprocess. Used for seed batches in tests and audits, so a
signal/movement change can be measured against Bus Events in seconds
instead of a windowed run per seed.

    python -m src.experiments.headless_run --seeds 1 2 3 4 5 --minutes 5 --tsp

prints median node_wait_frames by tsp_action across the batch.
"""

import argparse
import contextlib
import io
import json
import statistics
import tempfile
from pathlib import Path

from src.core import main
from src.core.signal_controller import SignalController
from src.core.vehicle import Bus
from src.telemetry.bus_event_log import BusEventTracker
from src.ui import canvas_gemini as canvas
from src.ui import control_panel

TSP_ROUTES = ("R1_EB_A_NB", "R2_EB_B_NB", "R4_WB_A_SB", "R5_WB_B_SB")

# The demand regime of the Test Run 1-3 benchmarks (Control Panel Inputs
# sheet): about double the panel defaults, mean network speed ~0.13 px/frame.
BENCHMARK_DEMAND = {
    "EB": ("Binomial", 24), "WB": ("Binomial", 22),
    "A_NB": ("Poisson", 17), "A_SB": ("Poisson", 18),
    "B_NB": ("Poisson", 14), "B_SB": ("Poisson", 15),
}
BENCHMARK_ROUTES_ACTIVE = {"R3_EB_ONLY": (True, 90)}


def apply_benchmark_regime():
    for key, (model, rate) in BENCHMARK_DEMAND.items():
        control_panel.approach_configs[key].update(
            {"active": True, "model": model, "rate": rate}
        )
    for route_id, (active, headway) in BENCHMARK_ROUTES_ACTIVE.items():
        control_panel.bus_routes_config[route_id].update(
            {"active": active, "headway_sec": headway}
        )

LANE_OPTIONS = {
    "EB": [canvas.H_Y - (i + 0.5) * canvas.LANE for i in range(3)],
    "WB": [canvas.H_Y + (i + 0.5) * canvas.LANE for i in range(3)],
    "A_NB": [canvas.INT_X[0] - (i + 0.5) * canvas.LANE for i in range(3)],
    "A_SB": [canvas.INT_X[0] + (i + 0.5) * canvas.LANE for i in range(3)],
    "B_NB": [canvas.INT_X[1] - (i + 0.5) * canvas.LANE for i in range(3)],
    "B_SB": [canvas.INT_X[1] + (i + 0.5) * canvas.LANE for i in range(3)],
}
SPAWN_AT = {
    "EB": ("EB", -20), "WB": ("WB", canvas.WIDTH + 20),
    "A_NB": ("NB", canvas.HEIGHT + 20), "A_SB": ("SB", -20),
    "B_NB": ("NB", canvas.HEIGHT + 20), "B_SB": ("SB", -20),
}


def run(seed, frames, tsp=True, dbl=False):
    """One seeded run; returns the completed bus-event records."""
    for route_id, cfg in control_panel.bus_routes_config.items():
        on = route_id in TSP_ROUTES
        cfg["tsp_enabled"] = tsp and on
        cfg["dbl_enabled"] = dbl and on
    control_panel.global_config["random_seed"] = seed
    control_panel.global_config["test_running"] = False
    signals = SignalController(
        global_config=control_panel.global_config, yellow_time=60, red_clearance_time=60
    )
    vehicles = []
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
        log_path = Path(tmp) / "bus_events.jsonl"
        tracker = BusEventTracker(log_path)
        # Reset touches the real session logs; point them at the temp dir.
        saved = (main.TELEMETRY_LOG_PATH, main.AGENT_TURN_LOG_PATH, main.BUS_EVENTS_LOG_PATH)
        main.TELEMETRY_LOG_PATH, main.AGENT_TURN_LOG_PATH, main.BUS_EVENTS_LOG_PATH = (
            Path(tmp) / "t.jsonl", Path(tmp) / "a.jsonl", log_path
        )
        try:
            main.perform_full_reset(vehicles, signals)
            cfgs = control_panel.approach_configs
            for frame in range(1, frames + 1):
                if not main.is_discharge_demand_suspended(signals):
                    if main.post_discharge_admission_allowed():
                        for key, (direction, pos) in SPAWN_AT.items():
                            if cfgs[key]["active"]:
                                main.try_spawn_vehicle(
                                    vehicles, key, direction, pos, LANE_OPTIONS[key],
                                    cfgs[key], signal_controller=signals,
                                )
                    if main.post_discharge_meter_frames_remaining <= 0:
                        main.check_and_dispatch_buses(vehicles, LANE_OPTIONS, 1.0 / 60.0)
                signals.update(vehicles=vehicles)
                if not signals.is_discharge_active():
                    main.advance_post_discharge_metering()
                signal_data = signals.get_all_signals(canvas.INT_X)
                for v in vehicles[:]:
                    v.update(
                        signal_data=signal_data, int_x_list=canvas.INT_X, h_y=canvas.H_Y,
                        road_w=canvas.ROAD_W, stop_offset=canvas.STOP, lane_w=canvas.LANE,
                        all_vehicles=vehicles, signal_controller=signals,
                    )
                    if (
                        v.x < -60 or v.x > canvas.WIDTH + 60
                        or v.y < -60 or v.y > canvas.HEIGHT + 60
                    ):
                        if isinstance(v, Bus) and v.passed_nodes:
                            tracker.complete(v, frame, signals)
                        vehicles.remove(v)
                tracker.observe(vehicles, frame, signals)
                main.accumulate_frame_metrics(vehicles)
        finally:
            main.TELEMETRY_LOG_PATH, main.AGENT_TURN_LOG_PATH, main.BUS_EVENTS_LOG_PATH = saved
        return main._read_jsonl_rows(log_path)


def node_waits_by_action(records):
    """{tsp_action: [node_wait_frames, ...]} over every node crossing."""
    waits = {}
    for record in records:
        for node in record.get("nodes", []):
            if node.get("node_clear_frame") is None:
                continue
            waits.setdefault(node.get("tsp_action", "none"), []).append(
                int(node.get("node_wait_frames") or 0)
            )
    return waits


def batch(seeds, frames, **kwargs):
    records = []
    for seed in seeds:
        records.extend(run(seed, frames, **kwargs))
    return records


def main_cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    parser.add_argument("--minutes", type=float, default=5.0)
    parser.add_argument("--tsp", action="store_true")
    parser.add_argument("--dbl", action="store_true")
    parser.add_argument("--benchmark-regime", action="store_true")
    args = parser.parse_args()
    if args.benchmark_regime:
        apply_benchmark_regime()
    records = batch(args.seeds, int(args.minutes * 3600), tsp=args.tsp, dbl=args.dbl)
    waits = node_waits_by_action(records)
    summary = {
        action: {"n": len(values), "median_wait_frames": statistics.median(values)}
        for action, values in sorted(waits.items())
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main_cli()
