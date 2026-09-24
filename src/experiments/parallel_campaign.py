"""Run a campaign's non-LLM arms as parallel headless processes.

Phase one of a two-phase campaign. The baseline and the deterministic
comparators (rule-based, passenger-pressure-tsp) use no GPU and have no
model latency to keep faithful, so they run here, several at once on the CPU
at below-normal priority. The LLM arms then run one at a time in the
windowed app -- the model on the GPU, the simulation with the CPU to itself
-- and join this campaign by starting the app with TRAFFIC_JOIN_CAMPAIGN set
(main.new_or_joined_campaign); or run them first and pass their campaign id
here with --campaign.

Each worker runs one (arm, seed) through the windowed batch's own per-run
path -- set the seed and model, control_panel.request_start_test,
main.perform_full_reset, main.step_simulation every frame, record_telemetry,
fire_due_checkpoints, finish_timed_test -- in its own runtime directory, and
the coordinator appends its summary rows and workbooks to the campaign and
writes the paired DVs. A rule arm decides on the same sim-time grid through
the agent's real graph in-process, and its decision lands through
main.merge_live_decision. Runs are deterministic: an (arm, seed) gives a
bit-identical end state whether run alone or beside others (measured
2026-09-24: 8 of 8 at 4 and at 8 in parallel).

    python -m src.experiments.parallel_campaign --arms baseline rule-based \\
        passenger-pressure-tsp --seeds 234 764 101 --minutes 60
"""
import argparse
import csv
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import traceback

REPO = pathlib.Path(__file__).resolve().parents[2]
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
BASELINE_NAMES = ("baseline", "none")


def default_workers():
    """Physical cores less two: one for the OS and desktop, one for a
    windowed run or the agent. ponytail: assumes 2-way SMT, as agent.py does."""
    return max(1, (os.cpu_count() or 2) // 2 - 2)


def resolve_arm(name):
    """The model string a non-LLM arm runs as ("None" for the baseline);
    ValueError for anything that needs a model."""
    from src.agents import rule_controller
    from src.ui import control_panel

    text = str(name).strip()
    if text.lower() in BASELINE_NAMES or text in ("None", control_panel.BATCH_BASELINE_LABEL):
        return "None"
    if rule_controller.is_rule_model(text):
        return text
    raise ValueError(
        f"{text!r} needs a model: run LLM arms in the windowed app "
        "(TRAFFIC_JOIN_CAMPAIGN joins them to this campaign)"
    )


# ---------------------------------------------------------------- worker

def _isolate_runtime(root):
    """Point every runtime file at ``root`` -- the list tests/conftest.py
    keeps -- so parallel workers never share logs, decisions or telemetry."""
    from src.agents import agent
    from src.core import guard, main
    from src.telemetry import bus_event_log, telemetry_exporter
    from src.ui import control_panel

    data, logs, results = root / "data", root / "logs", root / "results"
    for folder in (data, logs, results):
        folder.mkdir(parents=True, exist_ok=True)
    telemetry = data / "traffic_state_telemetry.json"
    for module, name, path in (
        (main, "DATA_DIR", data), (main, "LOGS_DIR", logs), (main, "RESULTS_DIR", results),
        (main, "TELEMETRY_PATH", telemetry), (main, "DECISION_PATH", data / "decision.json"),
        (main, "AGENT_TURN_LOG_PATH", logs / "agent_turn_log.jsonl"),
        (main, "TELEMETRY_LOG_PATH", logs / "telemetry_log.jsonl"),
        (main, "BUS_EVENTS_LOG_PATH", logs / "bus_events.jsonl"),
        (main, "TRIPINFO_LOG_PATH", logs / "tripinfo.jsonl"),
        (main, "FCD_LOG_PATH", logs / "fcd.csv"),
        (main, "EXCEL_EXPORT_DIR", results),
        (main, "EXPERIMENT_SUMMARY_PATH", results / "experiment_summary.csv"),
        (main, "INSTANCE_LOCK_PATH", data / "simulator.lock"),
        (guard, "REJECT_LOG_PATH", logs / "agent_rejects.log"),
        (agent, "TELEMETRY_PATH", telemetry),
        (agent, "AI_CONTROL_PATH", data / "ai_control.json"),
        (agent, "DECISION_PATH", data / "decision.json"),
        (agent, "TURN_LOG_PATH", logs / "agent_turn_log.jsonl"),
        (control_panel, "AI_CONTROL_PATH", data / "ai_control.json"),
        (bus_event_log, "DEFAULT_BUS_EVENTS_PATH", logs / "bus_events.jsonl"),
        (telemetry_exporter, "DEFAULT_TELEMETRY_PATH", telemetry),
    ):
        setattr(module, name, path)
    return results


def _rule_decider(model, vehicles, signals, telemetry):
    """The rule arm's decision source: on the sim-time grid, the agent's real
    graph decides from a fresh telemetry snapshot and publishes decision.json;
    main.merge_live_decision then applies it exactly as in the windowed app."""
    from src.agents import agent
    from src.core import main
    from src.ui import control_panel

    graph = agent.build_graph()
    grid = max(1, int(round(control_panel._effective_tick_seconds() * 60)))
    memory = {"turn": 0, "recent": []}

    def decide(frame):
        if frame % grid == 0:
            payload = telemetry.build_payload(
                signals, vehicles, frame,
                demand_state=main.get_demand_telemetry(),
                throughput_state=main.network_throughput,
            )
            agent.atomic_write_json(agent.TELEMETRY_PATH, payload)
            memory["turn"] += 1
            result = graph.invoke({
                "telemetry": {}, "discharge_active": False, "minimap": "",
                "locked_routes": set(), "raw_output": "", "call_metrics": {},
                "decision": {}, "status": "OK", "recent_decisions": memory["recent"],
                "turn": memory["turn"], "model": model,
                "decision_lag_sec": agent.turn_decision_lag(model, agent.DEFAULT_DECISION_LAG_SEC),
                "control_path": str(agent.AI_CONTROL_PATH),
                "run_uuid": payload.get("run_uuid"), "telemetry_frame": frame,
                "tick_index": memory["turn"], "scheduled_sim_time": frame / 60.0,
                "decision_interval_sec": grid / 60.0,
                "control_mode": control_panel.CONTROL_MODE_ASSISTED,
            })
            memory["recent"] = result.get("recent_decisions", memory["recent"])
        main.merge_live_decision(frame)

    return decide


def run_worker(arm, seed, minutes, campaign_id, stamp, workdir, warmup_sec=None):
    """One timed run of ``arm`` at ``seed`` in ``workdir``; writes result.json."""
    workdir = pathlib.Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    outcome = {"arm": arm, "seed": seed, "status": "FAILED"}
    try:
        if hasattr(os, "nice") and os.name != "nt":
            os.nice(5)
        results = _isolate_runtime(workdir)
        from src.core import main
        from src.core.signal_controller import SignalController
        from src.telemetry.telemetry_exporter import TelemetryExporter
        from src.ui import control_panel

        model = resolve_arm(arm)
        config = control_panel.global_config
        if warmup_sec is not None:
            config["warmup_discard_frames"] = int(round(float(warmup_sec) * 60))
        runtime = config.setdefault("batch_runtime", dict(control_panel.DEFAULT_BATCH_RUNTIME))
        runtime.update({"active": True, "campaign_id": campaign_id, "campaign_stamp": stamp})
        config["test_duration_sim_seconds"] = int(round(float(minutes) * 60))
        # The windowed batch's own per-run setup (main.build_batch_runner).
        config["random_seed"] = int(seed)
        control_panel.set_active_ai_model(model, persist=False)
        config["ai_runtime"]["armed"] = model != "None"
        control_panel.write_ai_control()
        control_panel.request_start_test()

        vehicles = []
        signals = SignalController(global_config=config, yellow_time=60, red_clearance_time=60)
        telemetry = TelemetryExporter(filename=main.TELEMETRY_PATH, export_interval_frames=10)
        frame = main.perform_full_reset(vehicles, signals, telemetry)
        config.update({
            "is_paused": False, "reset_triggered": False, "is_running": True,
            "run_has_started": True, "test_last_export": "", "start_requested": False,
        })
        control_panel.write_ai_control()
        decide = (
            _rule_decider(model, vehicles, signals, telemetry)
            if model != "None" else main.merge_live_decision
        )
        started = time.perf_counter()
        from src.experiments.progress import ProgressReporter
        reporter = ProgressReporter(
            f"{campaign_id[:8]} {arm} seed {seed}", config["test_duration_sim_seconds"] * 60,
            meta={"campaign_id": campaign_id, "arm": arm, "seed": seed},
        )
        # The Tk loop's order after each step: checkpoints, completion,
        # telemetry (main.main's simulation_step).
        while True:
            frame += 1
            main.step_simulation(vehicles, signals, frame, decide)
            config["sim_time_seconds"] = round(frame / 60.0, 1)
            reporter.update(frame)
            main.fire_due_checkpoints(frame)
            if main.timed_test_is_complete(frame):
                main.finish_timed_test(frame)
                break
            main.record_telemetry(telemetry, signals, vehicles, frame)
        reporter.finish("done" if not config.get("test_failed_reason") else "failed")
        outcome.update({
            "status": "COMPLETED" if not config.get("test_failed_reason") else "FAILED",
            "reason": str(config.get("test_failed_reason") or ""),
            "wall_s": round(time.perf_counter() - started, 1),
            "frames": frame,
            "summary_csv": [str(p) for p in sorted(results.glob("experiment_summary_*.csv"))],
            "workbooks": [str(p) for p in sorted(results.glob("*.xlsx"))],
        })
    except Exception as exc:
        outcome["reason"] = f"{type(exc).__name__}: {exc}"
        outcome["traceback"] = traceback.format_exc()
    (workdir / "result.json").write_text(json.dumps(outcome, indent=1), encoding="utf-8")
    return outcome


# ----------------------------------------------------------- coordinator

def _launch(arm, seed, minutes, campaign_id, stamp, workdir, warmup_sec):
    command = [
        sys.executable, "-m", "src.experiments.parallel_campaign", "--worker",
        "--arm", str(arm), "--seed", str(seed), "--minutes", str(minutes),
        "--campaign", campaign_id, "--stamp", stamp, "--workdir", str(workdir),
    ]
    if warmup_sec is not None:
        command += ["--warmup-sec", str(warmup_sec)]
    env = dict(os.environ, PYTHONPATH=str(REPO))
    return subprocess.Popen(
        command, cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0,
    )


def run_campaign(arms, seeds, minutes, workers=None, campaign=None, warmup_sec=None,
                 workroot=None, echo=print):
    """Run every (arm, seed) at most ``workers`` at a time, fold the rows and
    workbooks into the campaign, pair them, and return the outcomes."""
    from src.core import main
    from src.ui import control_panel

    models = [resolve_arm(arm) for arm in arms]           # refuse LLM arms up front
    workers = max(1, int(workers or default_workers()))
    campaign_id = campaign or new_campaign_id()
    stamp = main.campaign_stamp_of(campaign_id) or time.strftime("%Y%m%d")
    workroot = pathlib.Path(workroot or (main.RESULTS_DIR / "parallel" / campaign_id))
    jobs = [(arm, int(seed)) for arm in arms for seed in seeds]
    echo(f"campaign {campaign_id} ({stamp}): {len(jobs)} run(s), {workers} at a time, "
         f"{minutes} sim-min each, arms {models}")
    from src.experiments.progress import ProgressReporter
    campaign_progress = ProgressReporter(
        f"campaign {campaign_id[:8]}", len(jobs), kind="campaign", every_frames=1,
        meta={"campaign_id": campaign_id, "arms": models, "seeds": list(seeds), "workers": workers},
    )
    queue, running, outcomes = list(jobs), [], []
    while queue or running:
        while queue and len(running) < workers:
            arm, seed = queue.pop(0)
            workdir = workroot / f"{arm.replace(':', '_')}_seed{seed}"
            running.append((_launch(arm, seed, minutes, campaign_id, stamp, workdir, warmup_sec), workdir))
        for process, workdir in list(running):
            if process.poll() is None:
                continue
            running.remove((process, workdir))
            try:
                outcome = json.loads((workdir / "result.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                outcome = {"status": "FAILED", "reason": "worker wrote no result", "workdir": str(workdir)}
            outcomes.append(outcome)
            campaign_progress.update(len(outcomes))
            echo(f"  {outcome.get('arm')}/seed {outcome.get('seed')}: {outcome['status']}"
                 f"{' ' + outcome.get('reason', '') if outcome['status'] != 'COMPLETED' else ''}"
                 f"{' in ' + str(outcome.get('wall_s')) + ' s' if outcome.get('wall_s') else ''}")
        time.sleep(0.5)

    # Fold into the campaign: the rows through main's own appender (header
    # check and rotation), the workbooks beside the windowed batch's.
    runtime = control_panel.global_config.setdefault("batch_runtime", dict(control_panel.DEFAULT_BATCH_RUNTIME))
    runtime.update({"campaign_id": campaign_id, "campaign_stamp": stamp})
    main.EXCEL_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    for outcome in sorted(outcomes, key=lambda o: (str(o.get("arm")), int(o.get("seed") or 0))):
        if outcome.get("status") != "COMPLETED":
            continue
        for path in outcome.get("summary_csv", []):
            with open(path, encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    main.append_experiment_summary_row(None, row=row)
        for path in outcome.get("workbooks", []):
            shutil.copy2(path, main.EXCEL_EXPORT_DIR / pathlib.Path(path).name)
    main.print_campaign_summary(campaign_id)
    campaign_progress.finish(
        "done" if all(o.get("status") == "COMPLETED" for o in outcomes) else "failed",
        failed=sum(1 for o in outcomes if o.get("status") != "COMPLETED"),
    )
    runtime.update({"campaign_id": None, "campaign_stamp": None})
    echo(
        f"\nTo add the LLM arms to this campaign, start the app with "
        f"{main.JOIN_CAMPAIGN_ENV}={campaign_id} and queue them with the same seeds, "
        f"duration and settings (PowerShell: $env:{main.JOIN_CAMPAIGN_ENV}='{campaign_id}'; "
        r".\.venv\Scripts\python.exe run.py)."
    )
    return campaign_id, outcomes


def new_campaign_id():
    import uuid
    return str(uuid.uuid4())


def main_cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--arms", nargs="+", default=["baseline"])
    parser.add_argument("--seeds", nargs="+", type=int, default=[234])
    parser.add_argument("--minutes", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--campaign", default=None, help="join this campaign id")
    parser.add_argument("--warmup-sec", type=float, default=None)
    # worker mode (internal)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--arm")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--stamp")
    parser.add_argument("--workdir")
    args = parser.parse_args(argv)
    if args.worker:
        outcome = run_worker(args.arm, args.seed, args.minutes, args.campaign, args.stamp,
                             args.workdir, args.warmup_sec)
        return 0 if outcome["status"] == "COMPLETED" else 1
    try:
        run_campaign(args.arms, args.seeds, args.minutes, args.workers, args.campaign, args.warmup_sec)
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main_cli())
