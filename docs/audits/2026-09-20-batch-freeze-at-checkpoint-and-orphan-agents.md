# 2026-09-20 — Batch froze at the 5-min checkpoint; six agents were driving one sim

## Symptom

A 4-arm x 1-seed batch (baseline, hermes3:8b, dolphin3:8b, orca-mini:7b; seed 50;
10-min runs) stopped advancing during the orca-mini run. Telemetry froze at
frame 17995 / 299.917 s (`data/traffic_state_telemetry.json`, 03:29). The window
stayed open but nothing moved and the batch never started its next run. Hours
later `logs/agent_turn_log.jsonl` had grown to 1.0 GB and `agent_rejects.log`
was full of `PermissionError ... -> decision.json`.

## Evidence

| observation | source |
|---|---|
| `orca-mini-7b_5min_50seed_20092026_032907.xlsx` written 03:29:07 | `results/` |
| no `experiment_summary.csv` row after 03:24:02 (dolphin3's 10-min row) | `results/experiment_summary.csv` |
| sim frozen at 299.917 s = the 5-min checkpoint mark | telemetry |
| main process idle, 2.6 GB RSS; batch `poll_batch_runner` never advanced | process list |
| 5 extra `python -m src.agents.agent` processes, created 19 Sep 19:51-20:59 | `Get-CimInstance Win32_Process` |
| turn-log tail: same `turn` number repeated 3-4x per second, `tick_index=None` | `agent_turn_log.jsonl` |
| 47 of 112 decision rows for the orca-mini run are `SKIPPED_SLOW` | workbook Decisions sheet |

## Root cause 1 (the freeze): an exporter assertion escaped into the Tk callback

`fire_due_checkpoints(300)` -> `export_test_workbook` (succeeded) ->
`append_experiment_summary_row(300)` -> `build_experiment_summary_row` ->
`_run_export_assertions` raised

```
ExportAssertionError: realised decision cadence 10.086s deviates 102% from nominal 5.0s
```

`append_experiment_summary_row` only caught `BaselineContaminationError`. The
exception propagated out of the fixed-step `after` callback, so Tk never
rescheduled it: physics, telemetry, and the batch poller (all driven from that
one callback) stopped. The window itself stays responsive, which is why it
looked "stuck" rather than crashed.

The assertion was itself wrong. The scheduler was fine — ticks 1..58 are
contiguous, every 5 s grid point is logged as issued or `SKIPPED_SLOW`. One
`OBSERVATION_ONLY` straggler from the *previous* run (tick 118 @ 590 s,
guard_baseline's stale-arm path) landed in this run's log after
`reset_session_logs`, contributing a single 300 s gap that doubled the *mean*.

**Fixed (this commit):**
- `append_experiment_summary_row` now catches `ExportAssertionError` and any
  other exception, records `test_failed_reason`, prints loudly, and returns
  `False`. It can no longer take the sim loop down.
- Cadence assertion uses the **median** gap and ignores `OBSERVATION_ONLY`
  rows and rows scheduled beyond the current run's sim time.
- Checkpoint call sites pass `workbook_filename` and the exact
  `master_frame_count / 60` so `qa_duration_deviation_sec` is real.

Verified offline against the run's own logs: the row now writes with
`actual_decision_interval_sec_mean = median = 5.0`, 48 skipped ticks counted.

## Root cause 2 (the 1 GB log, the PermissionErrors, contaminated arms): orphan agents

Five `agent.py` subprocesses from earlier sessions (started 19 Sep evening)
were never terminated. `main.py` kills the child on `WM_DELETE_WINDOW`, but a
sim that is force-stopped from the IDE, crashes, or freezes (as above) leaves
its child alive. Each orphan:

- polls the shared `data/ai_control.json`, so it re-arms whenever any later
  session arms;
- runs the **old** loop (code loaded at its start), i.e. `sleep(tick)` after
  each turn, not the fixed schedule;
- writes `decision.json` (racing the live agent -> `PermissionError` on
  `os.replace`, ~60 in `agent_rejects.log`) and appends to the same turn log,
  ~100 KB per row (full telemetry snapshot). 5 procs x 1 row / 5 s x 3.5 h
  after the freeze = 1 GB.

Consequence for the data: during the LLM arms of this batch, **six** agents
were issuing decisions for one sim. The `decisions_issued = 64` for orca-mini
includes 53 orphan rows (`tick_index = None`). hermes3 and dolphin3 in this
batch are contaminated the same way. Baseline is not (orphans sleep when
`armed` is false).

**Fix applied (same day):** item 1 below is in `main.py` (`stdin=subprocess.PIPE`) and `agent.py` (`exit_when_parent_dies`), verified to exit 10 ms after the parent closes the pipe. Items 2-3 remain optional.

1. *Agent dies with its parent.* Spawn the agent with `stdin=subprocess.PIPE`
   and, in `agent.py`, start a daemon thread that blocks on
   `sys.stdin.read()` and calls `os._exit(0)` when it returns (the pipe
   closes when the parent dies, however it dies). ~10 lines, no dependency,
   works on Windows. This is the root fix.
2. *Refuse to double-drive.* On startup `main.py` should look for other
   `src.agents.agent` processes and refuse to arm (or kill them) — or the
   agent should hold an exclusive lock file and exit if it is already held.
   Cheap belt-and-braces on top of (1).
3. *Do not embed the full telemetry snapshot in `HELD_ALL_OFF`/stale rows.*
   They carry a snapshot that is stale by definition. Halves log growth under
   any future runaway.

## Recovery for this machine

```powershell
# kill the five orphan agents (PIDs from Get-CimInstance; the live one is the
# newest, created with the current main.py)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*src.agents.agent*' } |
  Select-Object ProcessId, CreationDate
Stop-Process -Id <old PIDs>

Remove-Item logs\agent_turn_log.jsonl   # 1 GB, unusable
```

Then re-run the batch. Treat the hermes3/dolphin3/orca-mini rows from
2026-09-20 02:57-03:29 as invalid (six-agent contamination); the two baseline
rows are usable.

## What the schedule change did show

Even contaminated, the orca-mini run demonstrates the fixed decision schedule
working as designed: 112 opportunities on a 5 s grid over 300 s (minus the
first), 47 skipped because the previous call was still running,
`utilisation_rate` 0.57. Before this change those skipped ticks were invisible
and the model simply decided less often.
