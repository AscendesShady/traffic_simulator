"""AI Configured: the model writes the timing plan and the lane-2 flashers;
the controller keeps every safety interval and falls back to Webster."""
import json

import pytest

import src.core.guard as guard
import src.core.main as main
import src.ui.control_panel as control_panel
from src.core.signal_controller import SignalController, NORMAL
from src.core.vehicle import Vehicle, DBL_LANE_INDEX
from src.ui import canvas_gemini as canvas
from tests.helpers import NODE_A, NODE_B, make_bus_for_leg

H_Y, LANE = canvas.H_Y, canvas.LANE
A, B = str(NODE_A), str(NODE_B)


def node_plan(ew=20, ns=10, cut=False, **dbl):
    return {
        "ew_green_sec": ew, "ns_green_sec": ns, "end_current_green_now": cut,
        "dbl": {a: dbl.get(a, False) for a in ("EB", "WB", "NB", "SB")},
    }


def plan(**overrides):
    return {A: node_plan(), B: node_plan(), **overrides}


# --- guard -----------------------------------------------------------------

def test_plan_is_validated_whole_and_greens_are_clamped():
    out = guard.validate_signal_plan({"plan": plan(**{A: node_plan(ew=120, ns=2.4, cut=True, EB=True)})})
    assert out[A] == node_plan(ew=90, ns=5, cut=True, EB=True)
    assert out[B] == node_plan()


@pytest.mark.parametrize("bad", [
    None, {}, {"plan": []},
    {"plan": {A: node_plan()}},                                    # one node
    {"plan": plan(**{A: dict(node_plan(), extra=1)})},             # extra key
    {"plan": plan(**{A: dict(node_plan(), ew_green_sec="20")})},   # string green
    {"plan": plan(**{A: dict(node_plan(), ew_green_sec=True)})},   # bool green
    {"plan": plan(**{A: dict(node_plan(), end_current_green_now=1)})},
    {"plan": plan(**{A: dict(node_plan(), dbl={"EB": True})})},    # missing approaches
    {"plan": plan(**{A: dict(node_plan(), dbl=dict(node_plan()["dbl"], EB="yes"))})},
])
def test_anything_else_fails_closed(bad):
    assert guard.validate_signal_plan(bad) is None


def test_safe_signal_plan_holds_webster_on_garbage_and_passes_valid(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "REJECT_LOG_PATH", tmp_path / "rejects.log")
    held = guard.safe_signal_plan("I think EW should be green longer", 3, "m")
    assert held["status"] == "HELD_WEBSTER" and held["plan"] is None
    assert held["schema"] == "signal_plan" and held["reason"] == ""
    assert (tmp_path / "rejects.log").exists()
    ok = guard.safe_signal_plan(
        "```json\n" + json.dumps({"plan": plan(), "reason": "both light"}) + "\n```", 4, "m"
    )
    assert ok["status"] == "OK" and ok["plan"] == plan() and ok["reason"] == "both light"


# --- controller --------------------------------------------------------------

def make_controller():
    return SignalController(
        {"green_time": 240, "webster_splits": {NODE_A: {"EW_green_frames": 400, "NS_green_frames": 300}}},
        yellow_time=3, red_clearance_time=2, min_green_frames=300,
    )


def run(controller, frames, vehicles=()):
    for _ in range(frames):
        controller.update(list(vehicles))


def test_plan_sets_green_lengths_and_no_plan_means_webster():
    c = make_controller()
    assert c.get_green_time(NODE_A, 0) == 400 and c.get_green_time(NODE_B, 0) == 240
    c.apply_plan(plan(**{A: node_plan(ew=7, ns=6)}))
    assert c.plan_active
    assert c.get_green_time(NODE_A, 0) == 420 and c.get_green_time(NODE_A, 3) == 360
    assert c.get_green_time(NODE_B, 0) == 1200
    c.clear_plan()
    assert not c.plan_active and c.get_green_time(NODE_A, 0) == 400


def test_a_shorter_plan_ends_the_running_green_now_through_yellow_and_all_red():
    c = make_controller()
    run(c, 350)                                   # EW green at Node A, 350 frames in
    node = c.nodes[NODE_A]
    assert node.phase == 0
    c.apply_plan(plan(**{A: node_plan(ew=5, ns=5)}))     # 300 frames < 350 elapsed
    c.update([])
    assert node.phase == 1                        # yellow, not straight to NS
    run(c, 3)
    assert node.phase == 2                        # all-red
    run(c, 2)
    assert node.phase == 3                        # NS green only after clearance
    assert c.get_all_signals()[NODE_A]["NB"] == "GREEN"


def test_cut_waits_for_minimum_green():
    c = make_controller()
    run(c, 100)                                   # 100 frames into EW green
    node = c.nodes[NODE_A]
    c.apply_plan(plan(**{A: node_plan(ew=60, ns=10, cut=True)}))
    run(c, 150)
    assert node.phase == 0 and node.cut_pending  # still green: min green is 300
    run(c, 50)
    assert node.phase == 1 and not node.cut_pending and node.timer == 0


def test_plan_replaces_tsp_and_reissuing_keeps_the_phase_running():
    c = make_controller()
    control_panel.bus_routes_config["R1_EB_A_NB"]["tsp_enabled"] = True
    bus = make_bus_for_leg("R1_EB_A_NB", NODE_A, "T")
    bus.x = NODE_A - 200
    try:
        c.update([bus])
        assert c.nodes[NODE_A].active_request is not None
        c.apply_plan(plan())
        assert c.nodes[NODE_A].active_request is None
        assert c.nodes[NODE_A].priority_state == NORMAL
        assert c.experiment_metrics["tsp_requests_denied"] + c.experiment_metrics["tsp_requests_granted"] >= 0
        run(c, 200, [bus])
        assert c.nodes[NODE_A].active_request is None   # no new requests while a plan is live
        timer = c.nodes[NODE_A].timer
        c.apply_plan(plan())                             # same plan again
        c.update([bus])
        assert c.nodes[NODE_A].timer == timer + 1        # no restart
    finally:
        control_panel.bus_routes_config["R1_EB_A_NB"]["tsp_enabled"] = False


def test_commanded_lane_is_dbl_for_vehicles_and_shares_with_left_turns():
    c = make_controller()
    c.apply_plan(plan(**{A: node_plan(EB=True)}))
    assert c.is_dbl_active_for_approach(NODE_A, "EB")
    assert not c.is_dbl_active_for_approach(NODE_A, "WB")
    assert c.get_all_dbl_states()[NODE_A]["EB"] == "ACTIVE"
    req = c.get_active_dbl_request(NODE_A, "EB")
    assert req["commanded"] and req["entry_lane"] == DBL_LANE_INDEX and req["bus_id"] is None
    assert c.get_active_dbl_request(NODE_A, "WB") is None
    assert not c.dbl_excludes_left_turns(NODE_A, "EB")     # left-turners may use it
    bus = make_bus_for_leg("R3_EB_ONLY", NODE_A, "R3")     # route flag off...
    assert c.is_dbl_requested_for_bus_leg(bus, NODE_A)      # ...but the lane is commanded
    assert c.get_plan()[A]["dbl"] == ["EB"]
    c.update([])
    assert c.experiment_metrics["dbl_total_active_frames"] == 1


def advance(vehicles, controller, frames):
    signals = controller.get_all_signals()
    for _ in range(frames):
        controller.update(vehicles)
        signals = controller.get_all_signals()
        for v in vehicles:
            v.update(signals, canvas.INT_X, H_Y, canvas.ROAD_W, canvas.STOP, LANE,
                     all_vehicles=vehicles, signal_controller=controller)


def test_commanded_lane_evicts_straight_cars_but_keeps_left_turners():
    c = make_controller()
    lane2 = H_Y - 2.5 * LANE
    straight = Vehicle(NODE_A - 350, lane2, "EB", target_turn="STRAIGHT", lane_index=2)
    left = Vehicle(NODE_A - 300, lane2, "EB", target_turn="LEFT", lane_index=2)
    late = Vehicle(NODE_A - 90, lane2, "EB", target_turn="STRAIGHT", lane_index=2)  # too close to slide
    for v in (straight, left, late):
        v.speed = 0.5
    c.apply_plan(plan(**{A: node_plan(EB=True)}))
    vehicles = [straight, left, late]
    advance(vehicles, c, 2)
    assert straight.lane_vacate_target in (0, 1)
    assert left.lane_vacate_target is None and left.lane_index == 2
    assert late.lane_vacate_target is None and late.speed > 0    # drives on, no hold


def test_reset_clears_the_plan(monkeypatch):
    c = make_controller()
    c.apply_plan(plan(**{A: node_plan(EB=True)}))
    monkeypatch.setattr(main, "signals", c, raising=False)
    main.perform_full_reset([], c)
    assert not c.plan_active and not c.nodes[NODE_A].dbl_commanded


# --- merge, agent, panel, telemetry -------------------------------------------

def write_decision(path, turn, plan_, status="OK", model="test-model", schema="signal_plan", **extra):
    payload = {
        "run_uuid": "RUN-1", "schema_version": 2, "schema": schema, "turn": turn,
        "model": model, "timestamp": main.time.time(), "status": status,
        "plan": plan_, "reason": "", **extra,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def configured_runtime(monkeypatch):
    runtime = {"armed": True, "model": "test-model", "control_mode": "configured",
               "tick_seconds": 10, "last_status": "WAITING_FOR_DECISION", "last_turn": 0}
    monkeypatch.setitem(control_panel.global_config, "ai_runtime", runtime)
    monkeypatch.setitem(control_panel.global_config, "_run_uuid", "RUN-1")
    for route in control_panel.bus_routes_config.values():
        monkeypatch.setitem(route, "tsp_enabled", False)
        monkeypatch.setitem(route, "dbl_enabled", False)
    return runtime


def test_merge_applies_a_plan_once_and_holds_webster_on_refusals(tmp_path, configured_runtime):
    c = make_controller()
    path = tmp_path / "decision.json"
    run(c, 100)
    write_decision(path, 1, plan(**{A: node_plan(ew=60, ns=10, cut=True, EB=True)}))
    assert main.merge_ai_decision(path, signals=c)
    assert c.plan_active and c.nodes[NODE_A].cut_pending and c.nodes[NODE_A].dbl_commanded == {"EB"}
    assert configured_runtime["last_status"] == "OK" and configured_runtime["last_turn"] == 1
    run(c, 200)                                   # the cut fires at min green
    assert c.nodes[NODE_A].phase == 1
    assert main.merge_ai_decision(path, signals=c)  # same file, 30 frames later
    assert not c.nodes[NODE_A].cut_pending           # not re-fired
    # Route flags never carry TSP under a plan.
    assert not any(r["tsp_enabled"] or r["dbl_enabled"] for r in control_panel.bus_routes_config.values())

    write_decision(path, 2, None, status="HELD_WEBSTER")
    assert not main.merge_ai_decision(path, signals=c)
    assert not c.plan_active and configured_runtime["last_status"] == "HELD_WEBSTER"
    assert configured_runtime["last_turn"] == 2

    write_decision(path, 3, plan())
    assert main.merge_ai_decision(path, signals=c) and c.plan_active
    write_decision(path, 4, plan(**{A: {"ew_green_sec": 20}}))          # invalid
    assert not main.merge_ai_decision(path, signals=c)
    assert not c.plan_active and configured_runtime["last_status"] == "INVALID_DECISION"

    write_decision(path, 5, plan())
    assert main.merge_ai_decision(path, signals=c) and c.plan_active
    write_decision(path, 6, None, schema=None, status="OK",
                   flags={r: {"tsp": True, "dbl": False} for r in guard.ROUTE_ORDER})  # assisted straggler
    assert not main.merge_ai_decision(path, signals=c)
    assert not c.plan_active and configured_runtime["last_status"] == "FOREIGN_DECISION"
    assert not control_panel.bus_routes_config["R1_EB_A_NB"]["tsp_enabled"]

    write_decision(path, 7, plan())
    assert main.merge_ai_decision(path, signals=c) and c.plan_active
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["timestamp"] -= 1000
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert not main.merge_ai_decision(path, signals=c)
    assert not c.plan_active and configured_runtime["last_status"] == "STALE_DECISION"


def test_disarming_withdraws_the_plan(configured_runtime):
    c = make_controller()
    c.apply_plan(plan())
    main._live_signals = c
    configured_runtime["armed"] = False
    main.merge_live_decision(30)
    assert not c.plan_active


def test_agent_configured_turn_reads_the_signal_map_and_guards_the_plan(tmp_path, monkeypatch):
    from src.agents import agent
    monkeypatch.setattr(guard, "REJECT_LOG_PATH", tmp_path / "rejects.log")
    telemetry = {
        "simulation_time_seconds": 120.0,
        "network_throughput": {"passengers_per_minute": 40, "passengers_per_minute_recent": 38},
        "measured_saturation_flow_veh_per_hr": 1290,
        "signal_state": {
            "timing": {"yellow_frames": 60, "all_red_frames": 60},
            "plan": {A: {"ew_green_sec": 30, "ns_green_sec": 15, "dbl": ["EB"]}},
            "nodes": {
                A: {"phase": "EW_GREEN", "residual_green_frames": 300,
                    "signals": {"EB": "GREEN", "WB": "GREEN", "NB": "RED", "SB": "RED"},
                    "queues_passengers_est": {"EB": 12, "WB": 4, "NB": 90, "SB": 0},
                    "queue_length_m": {"EB": 20.0, "WB": 5.0, "NB": 80.0, "SB": 0.0},
                    "downstream_space_m": {"EB": 100.0, "WB": 100.0, "NB": 0.0, "SB": 100.0},
                    "downstream_blocked": {"NB": True},
                    "left_turners_lane2": {"EB": 2},
                    "dbl_commanded": ["EB"]},
            },
        },
        "active_buses": [{"route_leg": {"node_x": NODE_A, "approach": "NB"}, "distance_to_stop_bar_px": 200,
                          "passengers": 45, "eta_to_stop_bar_sec_freeflow": 9.0}],
    }
    state = {"telemetry": telemetry, "control_mode": "configured", "decision_lag_sec": 6.0,
             "minimap": "", "turn": 3, "model": "gemma4:12b"}
    text = agent.read_minimap(state)["minimap"]
    assert "PLAN_IN_FORCE=" in text and "NB[waiting_pax=90" in text
    assert "left_turners_lane2=2" in text and "(BLOCKED)" in text and "lane2_reserved=True" in text
    assert "NB:45pax/eta9.0s" in text
    assert agent.check_locked(dict(state, minimap=text))["locked_routes"] == set()
    prompt = " ".join(agent.signal_plan_prompt(telemetry).split())
    assert "lost time of 1s yellow plus 1s all-red" in prompt
    assert "about 1290 vehicles per hour" in prompt and "Left-turning cars keep using lane 2" in prompt
    assert "5 to 90" in prompt

    held = agent.anti_cheat(dict(state, raw_output="EW longer please"))
    assert held["status"] == "HELD_WEBSTER" and held["decision"]["plan"] is None
    ok = agent.anti_cheat(dict(state, raw_output=json.dumps({"plan": plan(), "reason": "r"})))
    assert ok["status"] == "OK" and ok["decision"]["schema"] == "signal_plan"
    assert ok["decision"]["plan"] == plan()
    assisted = agent.anti_cheat(dict(state, control_mode="assisted", raw_output="", locked_routes=set()))
    assert assisted["status"] == "HELD_ALL_OFF"                          # other mode untouched
    # The turn log carries the plan: decisions_effective and the audit sheet
    # read it from there, so a configured run is never a silent no-op arm.
    monkeypatch.setattr(agent, "TURN_LOG_PATH", tmp_path / "turns.jsonl")
    monkeypatch.setattr(agent, "DECISION_PATH", tmp_path / "decision.json")
    agent.write_decision(dict(state, decision=ok["decision"], run_uuid="R", telemetry_frame=1))
    logged = [json.loads(l) for l in (tmp_path / "turns.jsonl").read_text(encoding="utf-8").splitlines()]
    assert logged[-1]["schema"] == "signal_plan" and logged[-1]["plan"] == plan()
    assert main._decisions_effective_count(logged) == 1


def test_panel_owns_the_mode_and_batch_labels_carry_it(monkeypatch):
    monkeypatch.setattr(control_panel, "write_ai_control", lambda *a, **k: None)
    monkeypatch.setattr(control_panel, "get_ollama_models", lambda: ["None", "gemma4:12b"])
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    runtime = dict(control_panel.global_config["ai_runtime"])
    monkeypatch.setitem(control_panel.global_config, "ai_runtime", runtime)
    assert control_panel.set_active_ai_model("gemma4:12b [decided]") == "gemma4:12b"
    assert runtime["control_mode"] == "configured"
    control_panel.set_active_ai_model("rule-based", control_mode="configured")
    assert runtime["control_mode"] == "assisted"                    # LLM-only
    control_panel.set_active_ai_model("gemma4:12b", control_mode="configured")
    assert runtime["control_mode"] == "configured"
    choices = control_panel.get_batch_model_choices()
    assert "gemma4:12b [decided]" in choices and "rule-based [decided]" not in choices
    assert control_panel.strategy_of("gemma4:12b [decided]") == control_panel.STRATEGY_LLM_DECIDED
    assert control_panel.strategy_of("gemma4:12b") == control_panel.STRATEGY_LLM_ASSISTED
    assert list(control_panel.CONTROL_STRATEGIES) == [
        "Baseline", "Rule-Based", "AI/LLM Assisted", "AI/LLM Decided"
    ]
    monkeypatch.setitem(control_panel.global_config, "test_duration_sim_seconds", 300)
    monkeypatch.setitem(control_panel.global_config, "test_running", False)
    monkeypatch.setitem(control_panel.global_config, "start_requested", False)
    monkeypatch.setitem(control_panel.global_config, "test_control_mode", "assisted")
    control_panel.request_start_test()
    assert control_panel.global_config["test_control_mode"] == "configured"


def test_summary_row_and_telemetry_carry_the_mode_and_plan(monkeypatch):
    from src.telemetry.telemetry_exporter import TelemetryExporter
    monkeypatch.setitem(control_panel.global_config, "test_control_mode", "configured")
    assert "control_mode" in main.EXPERIMENT_SUMMARY_HEADERS
    assert main.build_experiment_summary_row(60)["control_mode"] == "configured"
    c = make_controller()
    c.apply_plan(plan(**{A: node_plan(EB=True)}))
    payload = TelemetryExporter().build_payload(c, [], 10)
    assert payload["signal_state"]["plan"][A]["dbl"] == ["EB"]
    assert payload["signal_state"]["nodes"][A]["dbl_commanded"] == ["EB"]
    assert payload["signal_state"]["nodes"][A]["left_turners_lane2"]["EB"] == 0


# --- headless soak with a scripted planner ------------------------------------

def scripted_planner(payload, frame):
    """A proportional-split planner in the shape the guard accepts: greens
    by waiting passengers, a cut when the running phase serves nobody, lane
    2 reserved where a bus is queued behind through-cars."""
    nodes = payload["signal_state"]["nodes"]
    plan_ = {}
    for node_key, node in nodes.items():
        pax = node["queues_passengers_est"]
        ew, ns = pax["EB"] + pax["WB"], pax["NB"] + pax["SB"]
        total = max(1, ew + ns)
        green = "EW" if "EW" in str(node["phase"]) else "NS"
        running = ew if green == "EW" else ns
        plan_[node_key] = {
            "ew_green_sec": 10 + 50 * ew / total,
            "ns_green_sec": 10 + 50 * ns / total,
            "end_current_green_now": running == 0 and total > 20,
            "dbl": {a: bool(node["queues"].get(a, 0) >= 3 and a in ("EB", "WB")) for a in ("EB", "WB", "NB", "SB")},
        }
    validated = guard.validate_signal_plan({"plan": plan_})
    assert validated is not None, plan_
    main._live_signals.apply_plan(validated)
    return guard.all_off_flags()


def test_scripted_plan_drives_the_whole_network_without_overlaps(monkeypatch):
    import itertools
    from src.experiments import headless_run
    from tests.test_dbl_lane_clearing import rectangles_overlap
    monkeypatch.setitem(control_panel.global_config["ai_runtime"], "control_mode", "configured")
    overlaps = []
    real_step = main.step_simulation

    def checked(vehicles, signals, frame, decide=None):
        real_step(vehicles, signals, frame, decide=decide)
        if frame % 10 == 0:
            overlaps.extend(
                (frame, a.x, b.x) for a, b in itertools.combinations(vehicles, 2)
                if rectangles_overlap(a, b)
            )
    monkeypatch.setattr(main, "step_simulation", checked)
    cuts = {"n": 0}
    seen_plan = {"n": 0}

    def finish(vehicles, signals):
        seen_plan["n"] = int(signals.plan_active)
        cuts["n"] = signals.experiment_metrics["dbl_total_active_frames"]

    headless_run.run(7, 3000, tsp=False, dbl=False, decide=scripted_planner,
                     decide_every_frames=600, on_finish=finish)
    assert seen_plan["n"] == 1
    assert main.network_throughput["vehicles_served_total"] > 0
    assert overlaps == []
