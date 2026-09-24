"""One authoritative frame: the Tk loop and headless_run both run
main.step_simulation, so a headless experiment is production's engine
without the window (audit 2026-09-21, item 7)."""
from tests.helpers import NETWORK_CROSSING_FRAMES
import copy
import inspect
import json

from src.core import main
from src.core.vehicle import Bus
from src.experiments import headless_run


def _fingerprint(vehicles, signals):
    """Complete end-of-run state: every vehicle, both controller nodes,
    every cumulative counter and every completed bus event."""
    return {
        "vehicles": [
            (type(v).__name__, round(v.x, 3), round(v.y, 3), v.direction, v.lane_index,
             round(v.speed, 3), v.target_turn, sorted(v.passed_nodes))
            for v in vehicles
        ],
        "nodes": {
            str(x): (n.phase, n.timer, n.priority_state, n.priority_timer,
                     n.active_request.request_id if n.active_request else None,
                     len(n.terminal_history), len(n.reservations))  # keys are object ids
            for x, n in signals.nodes.items()
        },
        "throughput": dict(main.network_throughput),
        "run_metrics": json.loads(json.dumps(main.run_metrics, default=str)),
        "demand_hash": main._demand_draw_hash_hex(),
        "warmup_snapshot": dict(main.network_throughput_at_warmup),
    }


def test_tk_loop_runs_nothing_but_step_simulation():
    """The production callback must contain no simulation logic of its own:
    every spawn, decision merge, controller update, vehicle update, removal
    and metric lives in step_simulation, or headless would drift again."""
    source = inspect.getsource(main.main)
    assert "step_simulation(vehicles, signals, master_frame_count)" in source
    for forbidden in ("try_spawn_vehicle(", "check_and_dispatch_buses(", "merge_ai_decision(",
                      "signals.update(", "v.update(", "vehicles.remove(", "accumulate_frame_metrics(",
                      "bus_event_tracker.", "network_throughput["):
        assert forbidden not in source, forbidden
    step = inspect.getsource(main.step_simulation).split('"""')[2]  # body, not docstring
    # Production order, and the decision merges after spawning, before signals.
    order = [step.index(s) for s in ("try_spawn_vehicle(", "check_and_dispatch_buses(", "decide(frame_number)",
                                     "signals.update(", "v.update(", "_record_node_crossings(",
                                     "_record_completed_vehicle_delay(", "bus_event_tracker.complete(",
                                     "bus_event_tracker.observe(", "accumulate_frame_metrics(vehicles, signals)",
                                     "snapshot_warmup_baseline(")]
    assert order == sorted(order)


def test_headless_run_is_deterministic_and_keeps_production_bookkeeping(monkeypatch):
    monkeypatch.setitem(main.control_panel.global_config, "warmup_discard_frames", 600)
    # A frequent service, so buses run inside this short window (the default
    # headways put the first departure at 180 s).
    for route in main.control_panel.bus_routes_config.values():
        monkeypatch.setitem(route, "headway_sec", 30)
    states = []
    decisions = []

    def decide(payload, frame):
        decisions.append((frame, payload["frame_number"], len(payload["vehicle_positions"])))
        return {r: {"tsp": True, "dbl": False} for r in main.control_panel.bus_routes_config}

    for _ in range(2):
        headless_run.run(
            seed=11, frames=NETWORK_CROSSING_FRAMES // 2, tsp=True, decide=decide, decide_every_frames=300,
            on_finish=lambda vehicles, signals: states.append(copy.deepcopy(_fingerprint(vehicles, signals))),
        )
    first, second = states
    assert first == second  # same seed, same actions: identical complete state
    assert first["vehicles"] and first["throughput"]["cars_served"] > 0
    # The bookkeeping headless used to skip: general completions, served
    # passengers, completed delay, signal-aware metrics, warm-up snapshot.
    assert first["throughput"]["passengers_served_car"] >= first["throughput"]["cars_served"]  # trucks carry 1
    delay_samples = first["run_metrics"]["car_delay_samples_sec"] + first["run_metrics"]["bus_delay_samples_sec"]
    assert len(delay_samples) == first["throughput"]["vehicles_served_total"]
    assert first["throughput"]["stopped_vehicle_frames"] > 0
    assert first["warmup_snapshot"]["vehicles_served_total"] <= first["throughput"]["vehicles_served_total"]
    assert first["warmup_snapshot"]
    assert any(v[0] == "Bus" for v in first["vehicles"])  # dispatched through the shared step
    # Decisions were taken on the frame's own telemetry, after that frame's spawns.
    assert [d[0] for d in decisions[:5]] == [300, 600, 900, 1200, 1500]
    assert all(d[0] == d[1] for d in decisions)
