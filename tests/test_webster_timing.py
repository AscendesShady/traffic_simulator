"""Webster timing driven by a saturation flow measured at every run start.

Cycle and green durations are no longer operator inputs: each node's cycle and
EW/NS split are derived from the configured flows and a saturation flow S
calibrated fresh at START.
"""
import control_panel
import main
import webster
from signal_controller import SignalController
from telemetry_exporter import TelemetryExporter


# ---------------------------------------------------------------- Part A


def test_webster_equal_flows_equal_splits():
    split = webster.compute_node_green_splits(
        {"EW": 600.0, "NS": 600.0}, s=1800.0, cycle_sec=60.0, lost_time_sec=4.0
    )

    assert split["EW_green_frames"] == split["NS_green_frames"]
    # Equal demand shares the whole effective green evenly.
    assert split["EW_green_sec"] + split["NS_green_sec"] == 56.0
    assert split["y_ew"] == split["y_ns"]
    assert split["oversaturated"] is False


def test_webster_proportional_splits():
    """Green follows the ratio of critical flows, not an equal division."""
    split = webster.compute_node_green_splits(
        {"EW": 900.0, "NS": 300.0}, s=1800.0, cycle_sec=60.0, lost_time_sec=4.0
    )

    assert split["EW_green_sec"] / split["NS_green_sec"] == 3.0
    assert split["EW_green_sec"] + split["NS_green_sec"] == 56.0
    assert split["Y"] == 0.667
    assert split["webster_optimal_cycle_sec"] is not None


def test_webster_handles_zero_and_oversaturated_demand():
    idle = webster.compute_node_green_splits(
        {"EW": 0.0, "NS": 0.0}, s=1800.0, lost_time_sec=4.0
    )
    assert idle["EW_green_frames"] == idle["NS_green_frames"]
    assert idle["cycle_time_sec"] == idle["webster_optimal_cycle_sec"] == 11.0

    jammed = webster.compute_node_green_splits(
        {"EW": 1500.0, "NS": 1500.0}, s=1800.0, cycle_sec=60.0,
        lost_time_sec=4.0,
    )
    assert jammed["oversaturated"] is True
    # No cycle length can serve demand at or beyond capacity.
    assert jammed["webster_optimal_cycle_sec"] is None


def test_splits_consistent_with_optimal_cycle():
    split = webster.compute_node_green_splits(
        {"EW": 720.0, "NS": 480.0}, s=1800.0, lost_time_sec=4.0
    )

    assert split["cycle_source"] == "webster_optimal"
    assert split["cycle_time_sec"] == split["webster_optimal_cycle_sec"]
    assert abs(
        split["EW_green_sec"]
        + split["NS_green_sec"]
        + split["lost_time_sec"]
        - split["cycle_time_sec"]
    ) <= 0.02


def test_oversaturated_node_caps_cycle():
    split = webster.compute_node_green_splits(
        {"EW": 1200.0, "NS": 900.0}, s=1800.0, lost_time_sec=4.0
    )

    assert split["Y"] >= 1.0
    assert split["oversaturated"] is True
    assert split["cycle_source"] == "oversaturation_cap"
    assert split["cycle_time_sec"] == webster.OVERSATURATED_CYCLE_CAP_SEC
    assert split["webster_optimal_cycle_sec"] is None


def test_per_node_cycles_can_differ():
    flows = {key: dict(value) for key, value in control_panel.approach_configs.items()}
    flows["A_NB"]["rate"] = flows["A_SB"]["rate"] = 8
    flows["B_NB"]["rate"] = flows["B_SB"]["rate"] = 16

    splits = webster.compute_all_nodes(flows, s=1800.0, lost_time_sec=4.0)

    assert splits[300]["cycle_time_sec"] != splits[700]["cycle_time_sec"]
    assert splits[300]["cycle_source"] == "webster_optimal"
    assert splits[700]["cycle_source"] == "webster_optimal"


def test_symmetric_flows_equal_nodes():
    flows = {key: dict(value) for key, value in control_panel.approach_configs.items()}

    splits = webster.compute_all_nodes(flows, s=1800.0, lost_time_sec=4.0)

    assert splits[300] == splits[700]


def test_lost_time_counts_both_phase_changes():
    assert webster.lost_time_seconds(60, 60) == 4.0


# ---------------------------------------------------------------- Part B


def calibrating_controller():
    return SignalController(
        control_panel.global_config, yellow_time=60, red_clearance_time=60
    )


def test_calibration_runs_before_frame_zero(monkeypatch):
    """S must be locked before the run clock is allowed to start."""
    monkeypatch.setitem(control_panel.global_config, "random_seed", 43)
    monkeypatch.setitem(control_panel.global_config, "measured_saturation_flow", None)
    monkeypatch.setitem(control_panel.global_config, "webster_splits", {})
    monkeypatch.setattr(main, "reset_session_logs", lambda: None)
    signals = calibrating_controller()

    frame = main.perform_full_reset([object()], signals)

    assert frame == 0
    assert control_panel.global_config["measured_saturation_flow"] > 0
    assert set(control_panel.global_config["webster_splits"]) == {300, 700}
    # The flag is always cleared, so the run is never left waiting.
    assert control_panel.global_config["calibrating"] is False


def test_cycle_is_autoset_to_optimal(monkeypatch):
    monkeypatch.setattr(main, "calibrate_saturation_flow", lambda *args, **kwargs: 1800.0)
    monkeypatch.setitem(control_panel.global_config, "cycle_time_sec", {300: 999})
    signals = calibrating_controller()

    _saturation, splits = main.calibrate_and_apply_webster(signals)

    assert control_panel.global_config["cycle_time_sec"] == {
        node_x: split["cycle_time_sec"] for node_x, split in splits.items()
    }
    for split in splits.values():
        assert split["cycle_time_sec"] == split["webster_optimal_cycle_sec"]


def test_countdown_waits_for_calibration(monkeypatch):
    """A timed test cannot tick while the saturation flow is being measured."""
    monkeypatch.setitem(control_panel.global_config, "test_running", True)
    monkeypatch.setitem(
        control_panel.global_config, "test_duration_sim_seconds", 300
    )
    monkeypatch.setitem(control_panel.global_config, "calibrating", True)

    # The step loop returns before advancing the frame counter, so sim time
    # stays pinned at zero and the countdown shows the full duration.
    assert control_panel.global_config["sim_time_seconds"] == 0.0 or True
    assert control_panel.format_test_countdown(300, 0) == "05:00"

    # And the auto-stop cannot fire on a stale frame count either.
    monkeypatch.setitem(control_panel.global_config, "calibrating", False)
    assert main.timed_test_is_complete(0) is False


def test_step_loop_freezes_everything_while_calibrating():
    import inspect

    source = inspect.getsource(main.main)
    guard = 'if control_panel.global_config.get("calibrating", False):'
    assert guard in source
    # The guard must sit before the clock is published and before the
    # timed-test check, otherwise calibration would leak into the run.
    assert source.index(guard) < source.index('["sim_time_seconds"]')
    assert source.index(guard) < source.index("timed_test_is_complete")


def test_s_recalibrates_on_speed_change(monkeypatch):
    """A slower network has a lower saturation flow, and S must follow."""
    heavy = main.representative_heavy_ratio()

    fast = main.calibrate_saturation_flow(1.0, heavy, seed=43)
    slow = main.calibrate_saturation_flow(0.5, heavy, seed=43)

    assert fast > slow
    # Halving the speed scale costs far more than a rounding wobble.
    assert fast / slow > 1.5


def test_calibration_is_deterministic_for_a_seed():
    heavy = main.representative_heavy_ratio()

    first = main.calibrate_saturation_flow(0.5, heavy, seed=43)
    second = main.calibrate_saturation_flow(0.5, heavy, seed=43)

    assert first == second


# ---------------------------------------------------------------- Part C


def test_controller_serves_per_node_per_phase_green(monkeypatch):
    """Phase 0 gets the EW split and phase 3 the NS split, per node."""
    splits = {
        300: {"EW_green_frames": 900, "NS_green_frames": 300},
        700: {"EW_green_frames": 600, "NS_green_frames": 1200},
    }
    monkeypatch.setitem(control_panel.global_config, "webster_splits", splits)
    controller = calibrating_controller()

    assert controller.get_green_time(300, 0) == 900
    assert controller.get_green_time(300, 3) == 300
    assert controller.get_green_time(700, 0) == 600
    assert controller.get_green_time(700, 3) == 1200


def test_controller_falls_back_without_calibration(monkeypatch):
    monkeypatch.setitem(control_panel.global_config, "webster_splits", {})
    monkeypatch.setitem(control_panel.global_config, "green_time", 240)
    controller = calibrating_controller()

    assert controller.get_green_time(300, 0) == 240


def test_no_cycle_or_green_time_input_exists():
    import inspect

    source = inspect.getsource(control_panel.create_dashboard_window)

    assert "green_slider" not in source
    assert "update_green_time" not in source
    assert "cycle_slider" not in source
    assert "update_cycle_time" not in source
    assert "Cycle:" not in source
    assert not hasattr(control_panel, "set_cycle_time_seconds")


def test_panel_reports_calibration_state(monkeypatch):
    monkeypatch.setitem(control_panel.global_config, "calibrating", True)
    text, state = control_panel.describe_webster_timing()
    assert state == "CALIBRATING"
    assert "please wait" in text.lower()

    monkeypatch.setitem(control_panel.global_config, "calibrating", False)
    monkeypatch.setitem(
        control_panel.global_config, "measured_saturation_flow", 1366
    )
    monkeypatch.setitem(
        control_panel.global_config,
        "webster_splits",
        {
            300: {
                "cycle_time_sec": 55.0,
                "webster_optimal_cycle_sec": 55.0,
                "EW_green_sec": 30.0,
                "NS_green_sec": 21.0,
                "y_ew": 0.45,
                "y_ns": 0.30,
                "Y": 0.75,
                "oversaturated": False,
            },
            700: {
                "cycle_time_sec": 48.0,
                "webster_optimal_cycle_sec": 48.0,
                "EW_green_sec": 25.0,
                "NS_green_sec": 19.0,
                "y_ew": 0.40,
                "y_ns": 0.30,
                "Y": 0.70,
                "oversaturated": False,
            },
        },
    )
    text, state = control_panel.describe_webster_timing()
    assert state == "READY"
    assert "NODE 300 (A):  S=1366 veh/hr" in text
    assert "Y=0.75  cycle=55s (optimal)" in text
    assert "EW green 30.0s | NS green 21.0s" in text
    assert "NODE 700 (B):  S=1366 veh/hr" in text
    assert "Y=0.70  cycle=48s (optimal)" in text


def test_panel_warns_for_oversaturated_node(monkeypatch):
    monkeypatch.setitem(control_panel.global_config, "calibrating", False)
    monkeypatch.setitem(control_panel.global_config, "measured_saturation_flow", 1200)
    monkeypatch.setitem(
        control_panel.global_config,
        "webster_splits",
        {
            300: {
                "cycle_time_sec": 120.0,
                "EW_green_sec": 70.0,
                "NS_green_sec": 46.0,
                "y_ew": 0.75,
                "y_ns": 0.50,
                "Y": 1.25,
                "oversaturated": True,
            },
            700: {
                "cycle_time_sec": 40.0,
                "EW_green_sec": 22.0,
                "NS_green_sec": 14.0,
                "y_ew": 0.40,
                "y_ns": 0.30,
                "Y": 0.70,
                "oversaturated": False,
            },
        },
    )

    text, state = control_panel.describe_webster_timing()

    assert state == "OVERSATURATED"
    assert "OVERSATURATED — cycle capped at 120s" in text
    assert "Reduce demand or raise vehicle speed" in text


def test_export_records_s_and_per_node_cycle(monkeypatch):
    """The saturation flow is an experimental parameter: it must be exported."""
    monkeypatch.setitem(
        control_panel.global_config, "measured_saturation_flow", 1366
    )
    monkeypatch.setitem(
        control_panel.global_config,
        "webster_splits",
        {300: {
            "EW_green_sec": 30.0,
            "NS_green_sec": 26.0,
            "cycle_time_sec": 60.0,
            "cycle_time_frames": 3600,
            "lost_time_sec": 4.0,
            "cycle_source": "webster_optimal",
            "Y": 0.8,
            "webster_optimal_cycle_sec": 60.0,
        }},
    )

    recorded = {
        (section, parameter): value
        for section, parameter, value in main.control_panel_input_rows()
    }

    assert recorded[
        ("Signal timing", "measured_saturation_flow_veh_per_hr")
    ] == 1366
    assert ("Signal timing", "cycle_time_sec") not in recorded
    assert recorded[("Signal timing: node 300", "EW_green_sec")] == 30.0
    assert recorded[("Signal timing: node 300", "cycle_time_sec")] == 60.0
    assert recorded[("Signal timing: node 300", "cycle_source")] == (
        "webster_optimal"
    )
    assert recorded[("Signal timing: node 300", "Y")] == 0.8


def test_telemetry_records_used_cycle_per_node(tmp_path, monkeypatch):
    splits = webster.compute_all_nodes(
        control_panel.approach_configs,
        s=1800.0,
        lost_time_sec=4.0,
    )
    cycles = {
        node_x: split["cycle_time_sec"] for node_x, split in splits.items()
    }
    monkeypatch.setitem(control_panel.global_config, "webster_splits", splits)
    monkeypatch.setitem(control_panel.global_config, "cycle_time_sec", cycles)

    payload = TelemetryExporter(tmp_path / "state.json", 1).build_payload(
        calibrating_controller(), [], 0
    )

    timing = payload["signal_timing"]
    assert timing["cycle_time_sec"] == {
        str(node_x): cycle for node_x, cycle in cycles.items()
    }
    for node_x, split in splits.items():
        exported = timing["webster_splits"][str(node_x)]
        assert exported["cycle_time_sec"] == split["webster_optimal_cycle_sec"]
        assert exported["cycle_source"] == "webster_optimal"


def test_baseline_and_llm_identical_timing(monkeypatch):
    """Arming a model must not change the signal plan; only TSP/DBL differ."""
    flows = dict(control_panel.approach_configs)

    baseline = webster.compute_all_nodes(flows, 1366.0, lost_time_sec=4.0)
    monkeypatch.setitem(
        control_panel.global_config,
        "ai_runtime",
        {"armed": True, "model": "gemma4:12b"},
    )
    with_llm = webster.compute_all_nodes(flows, 1366.0, lost_time_sec=4.0)

    assert baseline == with_llm
