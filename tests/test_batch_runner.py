"""Batch Benchmark Runner: seed parsing, the live preview, the (model x
seed) queue, and unattended failure isolation -- chained entirely through
the existing single-run path (request_start_test), never reimplemented.
"""
import copy

import control_panel
import main
import batch_runner


# --------------------------------------------------------------------------
# Seed parsing
# --------------------------------------------------------------------------

def test_seed_parsing():
    seeds, error = batch_runner.parse_seed_list("42,45-47,50")
    assert error is None
    assert seeds == [42, 45, 46, 47, 50]

    seeds, error = batch_runner.parse_seed_list("42,43,44")
    assert error is None
    assert seeds == [42, 43, 44]

    # Duplicates across a plain value and a range are removed, and the
    # result is sorted regardless of input order.
    seeds, error = batch_runner.parse_seed_list("5,3,5,3-4")
    assert error is None
    assert seeds == [3, 4, 5]

    seeds, error = batch_runner.parse_seed_list("abc")
    assert seeds == []
    assert error is not None

    seeds, error = batch_runner.parse_seed_list("")
    assert seeds == []
    assert error is not None

    seeds, error = batch_runner.parse_seed_list("10-5")
    assert seeds == []
    assert error is not None

    seeds, error = batch_runner.parse_seed_list("42,,44")
    assert seeds == []
    assert error is not None


def test_describe_seeds_live_viewer_text():
    assert batch_runner.describe_seeds([42, 43, 44, 45, 46]) == (
        "5 seeds: 42, 43, 44, 45, 46"
    )
    assert batch_runner.describe_seeds([]) == "0 seeds"


# --------------------------------------------------------------------------
# Live preview
# --------------------------------------------------------------------------

def test_preview_counts():
    preview = batch_runner.compute_batch_preview(
        model_count=4, seed_count=3, duration_sim_seconds=600,
        duration_label="10 min",
    )
    assert preview["runs"] == 12
    assert preview["checkpoints_per_run"] == 2  # cp5(no) -> just cp10; +final = 2
    assert preview["result_sets"] == 24
    assert "12 runs" in preview["text"]
    assert "24 result sets" in preview["text"]
    assert "4 models" in preview["text"] and "3 seeds" in preview["text"]


# --------------------------------------------------------------------------
# Queue expansion: seeds outer, models inner, so every model's result for
# one seed is complete early and can be analysed while the batch continues.
# --------------------------------------------------------------------------

def test_batch_expands_cross():
    queue = batch_runner.expand_batch(["m1", "m2"], [1, 2, 3])
    assert queue == [
        (1, "m1"), (1, "m2"),
        (2, "m1"), (2, "m2"),
        (3, "m1"), (3, "m2"),
    ]


def test_batch_queue_is_seed_first():
    queue = batch_runner.expand_batch(["A", "B"], [1, 2])
    assert queue == [(1, "A"), (1, "B"), (2, "A"), (2, "B")]

    # The runner consumes the queue in that same order.
    runner = batch_runner.BatchRunner(
        set_seed=lambda s: None, set_model=lambda m: None, start_run=lambda: None,
    )
    runner.start(["A", "B"], [1, 2])
    order = []
    while runner.current is not None:
        order.append((runner.current["seed"], runner.current["model"]))
        runner.report_run_outcome("COMPLETED")
    assert order == [(1, "A"), (1, "B"), (2, "A"), (2, "B")]


def test_api_rate_limit_skips_model_across_remaining_seeds_seed_first():
    """With seeds outer, a rate-limited model's runs are no longer contiguous;
    the skip must still remove that model for every remaining seed while the
    other model keeps running on those seeds."""
    runner = batch_runner.BatchRunner(
        set_seed=lambda s: None, set_model=lambda m: None, start_run=lambda: None,
    )
    runner.start(["gemini-2.5-flash", "rule-based"], [1, 2, 3])
    assert runner.current == {"model": "gemini-2.5-flash", "seed": 1}

    runner.report_run_outcome("FAILED", rate_limited=True, is_api_model=True)

    skipped = [row for row in runner.results if row["status"] == "SKIPPED"]
    assert {row["seed"] for row in skipped} == {2, 3}
    assert all(row["model"] == "gemini-2.5-flash" for row in skipped)
    assert runner.current == {"model": "rule-based", "seed": 1}
    assert runner.queue == [(2, "rule-based"), (3, "rule-based")]

    # The other model still runs on every seed, in seed order.
    order = []
    while runner.current is not None:
        order.append((runner.current["seed"], runner.current["model"]))
        runner.report_run_outcome("COMPLETED")
    assert order == [(1, "rule-based"), (2, "rule-based"), (3, "rule-based")]


# --------------------------------------------------------------------------
# The batch chains request_start_test() -- it does not reimplement a run.
# --------------------------------------------------------------------------

def test_batch_uses_single_run_path(monkeypatch):
    calls = []
    monkeypatch.setattr(
        control_panel, "request_start_test",
        lambda: calls.append("request_start_test") or 300,
    )
    monkeypatch.setattr(control_panel, "write_ai_control", lambda *a, **k: None)
    control_panel.global_config["test_duration_sim_seconds"] = 300

    runner = main.build_batch_runner()
    runner.start(["rule-based"], [7])

    assert calls == ["request_start_test"]
    assert runner.current == {"model": "rule-based", "seed": 7}
    assert control_panel.global_config["random_seed"] == 7
    assert control_panel.global_config["ai_runtime"]["model"] == "rule-based"


def test_batch_maps_baseline_label_to_none_model(monkeypatch):
    monkeypatch.setattr(control_panel, "request_start_test", lambda: 300)
    monkeypatch.setattr(control_panel, "write_ai_control", lambda *a, **k: None)

    runner = main.build_batch_runner()
    runner.start([control_panel.BATCH_BASELINE_LABEL], [1])

    assert control_panel.global_config["ai_runtime"]["model"] == "None"
    assert control_panel.global_config["ai_runtime"]["armed"] is False


# --------------------------------------------------------------------------
# Failure isolation
# --------------------------------------------------------------------------

def test_failed_run_continues_batch():
    runner = batch_runner.BatchRunner(
        set_seed=lambda s: None, set_model=lambda m: None, start_run=lambda: None,
    )
    runner.start(["m1"], [1, 2])
    assert runner.current == {"model": "m1", "seed": 1}

    runner.report_run_outcome("FAILED", reason="crash")

    assert runner.results[-1] == {
        "model": "m1", "seed": 1, "status": "FAILED", "reason": "crash",
    }
    assert runner.current == {"model": "m1", "seed": 2}
    assert runner.is_active()


def test_api_rate_limit_skips_model_remainder():
    runner = batch_runner.BatchRunner(
        set_seed=lambda s: None, set_model=lambda m: None, start_run=lambda: None,
    )
    runner.start(["gemini-2.5-flash", "rule-based"], [1, 2, 3])
    assert runner.current == {"model": "gemini-2.5-flash", "seed": 1}

    runner.report_run_outcome("FAILED", rate_limited=True, is_api_model=True)

    skipped = [row for row in runner.results if row["status"] == "SKIPPED"]
    assert {row["seed"] for row in skipped} == {2, 3}
    assert all(row["model"] == "gemini-2.5-flash" for row in skipped)
    # The next model's queue is untouched by the previous model's limit.
    assert runner.current == {"model": "rule-based", "seed": 1}


def test_repeated_failures_of_an_api_model_are_treated_as_rate_limited():
    """When explicit detection is unavailable, two consecutive failures of
    the same API model are treated as a rate limit and skip the rest."""
    runner = batch_runner.BatchRunner(
        set_seed=lambda s: None, set_model=lambda m: None, start_run=lambda: None,
    )
    runner.start(["gpt-5"], [1, 2, 3])
    runner.report_run_outcome("FAILED", is_api_model=True)  # 1st failure: continues
    assert runner.current == {"model": "gpt-5", "seed": 2}

    runner.report_run_outcome("FAILED", is_api_model=True)  # 2nd consecutive: skip rest

    assert runner.current is None
    assert not runner.is_active()
    statuses = {row["seed"]: row["status"] for row in runner.results}
    assert statuses == {1: "FAILED", 2: "FAILED", 3: "SKIPPED"}


# --------------------------------------------------------------------------
# STOP BATCH
# --------------------------------------------------------------------------

def test_stop_batch_finishes_current_run():
    runner = batch_runner.BatchRunner(
        set_seed=lambda s: None, set_model=lambda m: None, start_run=lambda: None,
    )
    runner.start(["m1", "m2"], [1])
    assert runner.current == {"model": "m1", "seed": 1}

    runner.request_stop()
    assert runner.current == {"model": "m1", "seed": 1}  # not aborted mid-run

    runner.report_run_outcome("COMPLETED")

    assert runner.current is None
    assert not runner.is_active()
    assert runner.results == [
        {"model": "m1", "seed": 1, "status": "COMPLETED", "reason": ""}
    ]  # m2 never started


# --------------------------------------------------------------------------
# Regime untouched
# --------------------------------------------------------------------------

def test_batch_does_not_change_regime(monkeypatch):
    monkeypatch.setattr(control_panel, "write_ai_control", lambda *a, **k: None)
    monkeypatch.setattr(control_panel, "request_start_test", lambda: 300)
    control_panel.global_config["test_duration_sim_seconds"] = 300

    before_approaches = copy.deepcopy(control_panel.approach_configs)
    before_routes = copy.deepcopy(control_panel.bus_routes_config)
    before_speed_scale = control_panel.global_config["vehicle_speed_scale"]
    before_eligibility = control_panel.global_config["priority_eligibility_px"]

    runner = main.build_batch_runner()
    runner.start(["rule-based", control_panel.BATCH_BASELINE_LABEL], [1, 2])
    for _ in range(4):
        runner.report_run_outcome("COMPLETED")

    assert control_panel.approach_configs == before_approaches
    assert control_panel.bus_routes_config == before_routes
    assert control_panel.global_config["vehicle_speed_scale"] == before_speed_scale
    assert control_panel.global_config["priority_eligibility_px"] == before_eligibility


# --------------------------------------------------------------------------
# Model picker
# --------------------------------------------------------------------------

def test_poll_batch_runner_end_to_end(monkeypatch):
    """main.poll_batch_runner drives a full two-run batch across simulated
    simulation-loop ticks, proving the real wiring (not just the isolated
    BatchRunner class) chains request_start_test/request_start_stop and
    reaches DONE with both runs recorded."""
    monkeypatch.setattr(control_panel, "write_ai_control", lambda *a, **k: None)
    monkeypatch.setattr(control_panel, "get_api_models", lambda: ["None"])
    monkeypatch.setattr(main, "_read_jsonl_rows", lambda path: [])
    control_panel.global_config["test_duration_sim_seconds"] = 300
    control_panel.global_config["sim_speed"] = 1.0

    def fake_start_test():
        control_panel.global_config["start_requested"] = True
        return control_panel.global_config["test_duration_sim_seconds"]

    def fake_stop():
        control_panel.global_config["test_running"] = False
        control_panel.global_config["is_running"] = False

    monkeypatch.setattr(control_panel, "request_start_test", fake_start_test)
    monkeypatch.setattr(control_panel, "request_start_stop", fake_stop)

    def simulate_reset_tick():
        """Stand-in for the one line in perform_full_reset that matters
        here: consuming start_requested and flipping test_running on."""
        if control_panel.global_config.get("start_requested"):
            control_panel.global_config["start_requested"] = False
            control_panel.global_config["is_running"] = True
            control_panel.global_config["test_running"] = True

    assert control_panel.request_start_batch(["rule-based"], [1, 2]) == 2

    # --- run 1 (seed 1) ---
    main.poll_batch_runner()  # consumes batch_start_requested; queues run 1
    assert control_panel.global_config["random_seed"] == 1
    simulate_reset_tick()
    main.poll_batch_runner()  # AWAITING_START -> RUN_ACTIVE
    main.poll_batch_runner()  # RUN_ACTIVE, still running -> no-op
    fake_stop()               # stand-in for finish_timed_test
    main.poll_batch_runner()  # RUN_ACTIVE -> COMPLETED; queues run 2

    runtime = control_panel.global_config["batch_runtime"]
    assert runtime["results"] == [
        {"model": "rule-based", "seed": 1, "status": "COMPLETED", "reason": ""}
    ]
    assert runtime["active"] is True

    # --- run 2 (seed 2) ---
    assert control_panel.global_config["random_seed"] == 2
    main.poll_batch_runner()  # IDLE -> AWAITING_START for run 2
    simulate_reset_tick()
    main.poll_batch_runner()  # AWAITING_START -> RUN_ACTIVE
    fake_stop()
    main.poll_batch_runner()  # RUN_ACTIVE -> COMPLETED; queue empty -> DONE

    runtime = control_panel.global_config["batch_runtime"]
    assert [row["status"] for row in runtime["results"]] == ["COMPLETED", "COMPLETED"]
    assert runtime["active"] is False


def test_model_picker_lists_local_api_rule_and_none(monkeypatch):
    monkeypatch.setattr(
        control_panel, "get_ollama_models",
        lambda: ["None", "llama3.1:8b", "gemma4:12b"],
    )
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    choices = control_panel.get_batch_model_choices()

    assert choices[0] == control_panel.BATCH_BASELINE_LABEL
    assert control_panel.RULE_BASED_MODEL in choices
    assert "llama3.1:8b" in choices
    assert "gemma4:12b" in choices
    assert "gemini-2.5-flash" in choices
    assert "gpt-5" not in choices  # OPENAI_API_KEY not set
    assert "None" not in choices  # bare "None" never appears; only the label does
