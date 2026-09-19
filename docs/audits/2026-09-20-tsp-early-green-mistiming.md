# TSP early-green mistiming, non-converged DV, baseline arming leak — 2026-09-20

Forensic follow-up on 195 benchmark runs (Test Runs 1–3). Three defects were
reported; each got its own commit and tests. Findings that differ from the
original report are marked **Verified differently**.

## Defect 1 — `early_green` buses wait ~10 s longer than untreated buses

### Reported evidence (Test Run 3, 14 workbooks, 581 node crossings)

| `nodeN_tsp_action` | n | median `node_wait_frames` | seconds | Mann-Whitney vs `none` |
|---|---|---|---|---|
| `none` | 521 | 550 | 9.2 s | — |
| `extending` | 20 | 443 | 7.4 s | p = 0.87 |
| `early_green` | 40 | 1148 | **19.1 s** | **p = 0.00011** |

Supporting: `corr(tsp_adjust_frames, node_wait_frames) = +0.351`; treated
buses get a zero wait 3.3 % of the time vs 12.3 % untreated.

### Source verification

`signal_controller.py` before this change:

- `is_bus_tsp_eligible` compared **distance only** against
  `priority_eligibility_px` (default 500 px, slider up to 800 px, link between
  nodes 400 px).
- `_request_can_start_tsp` checked only "bus is upstream, in the requested
  lane, one action per request". **No arrival-time check existed.**
- `_begin_early_green` truncated the running conflicting green on the first
  frame the request was head-of-queue, with no check that the bus could use
  the green it brought forward.

So the reported root cause is real as a code path. What the data actually
shows, though, is different — see below.

### Verified differently: the 1148-vs-550 gap is selection, not treatment harm

`node_wait_frames` counts stopped frames anywhere on the approach leg. The
`early_green` tag is applied to a bus that was head-of-queue **while the
conflicting phase was green** — i.e. to the buses that met a red. The `none`
group pools the baseline arm, the nemotron arm (which never fired TSP) and
every bus in a treated arm that arrived on green or whose route flag was off
at the time. Comparing the two groups compares red-arrivals against a
green-heavy mixture.

Per-arm distribution of **all** node crossings, from the same Test Run 3
Bus Events sheets (n / median / mean frames):

| arm | all crossings | early_green | extending | none |
|---|---|---|---|---|
| baseline | 82 / **935** / 990 | – | – | 82 / 935 / 990 |
| rule-based | 85 / **405** / 616 | 9 / 1030 / 1144 | 5 / 442 / 278 | 71 / 364 / 573 |
| llama3-latest | 84 / **657** / 774 | 14 / 1446 / 1417 | 7 / 972 / 965 | 63 / 457 / 609 |
| gemini-3.5-flash-lite | 82 / **634** / 878 | 14 / 1062 / 1175 | 5 / 1602 / 1148 | 63 / 473 / 791 |
| grok-4 | 84 / **535** / 874 | 2 / 920 / 920 | 1 / 47 / 47 | 81 / 545 / 883 |
| llama3.1-8b | 82 / 660 / 925 | 1 / 1620 / 1620 | 1 / 273 / 273 | 80 / 660 / 924 |
| nemotron-3-nano-4b | 82 / **935** / 990 | – | 1 / 245 / 245 | 81 / 950 / 1000 |

Every arm that actually fired TSP has a **lower** all-bus median wait than
the baseline (405–660 vs 935); nemotron, which fired nothing, reproduces the
baseline to the frame (same seed). Inside each arm the `early_green` subgroup
is the slow one because it is the red-arriving subgroup; the `none` subgroup
inside a treated arm (364–473) is far below the baseline's `none` (935)
because it is the green-arriving subgroup. The sign at the arm level is
right.

A second mechanism the report missed: the truncation fires the instant a bus
becomes head-of-queue, which under the benchmark demand is at the moment it
enters the 400 px zone at **free-flow speed**, before it reaches the queue.
Instrumenting the gate in a headless replay of the benchmark regime (seed 1,
5 min): 11 evaluations, bus speed 0.50 px/frame in 10 of them. A speed-based
ETA is therefore only as good as the empty road ahead. This is why the
reported "3850-frame travel time at 0.13 px/frame" never appears at trigger
time: 0.13 is the network mean, not the speed of a bus entering the zone.

### Fix (commit 1)

Kept exactly to the requested shape — a gate on whether an early green may
*start*; the priority state machine, `_begin_extension`, `_tsp_cap_frames`
and `TSP_MAX_ADJUST_FRACTION` are untouched.

1. **Shared ETA estimator** `vehicle.eta_frames_to_stop_bar(distance, speed)`
   — current speed, floored at `ETA_MIN_SPEED_PX_PER_FRAME = 0.05` (a stopped
   bus reads as a long, finite wait), capped at 6000 frames. The telemetry
   exporter's `eta_to_stop_bar_sec_live` now calls it, so the arbiter and the
   agent/rule decision path read the same estimator (the rule and the LLM keep
   using the free-flow ETA for "will my flag land in time", which is a
   different question and part of the LLM-facing schema).
2. **Arrival window** in `_early_green_is_feasible`, re-evaluated every frame
   while the request is armed:
   `earliest_green = frames_left_in_truncated_green + yellow + all_red`,
   `latest_useful = earliest_green + bus_phase_green`,
   allow only if `earliest_green <= crossing <= latest_useful` where
   `crossing = max(eta, earliest_green)`. **Deliberate deviation from the
   spec's `now + eta` lower bound:** a bus already stopped at the bar crosses
   when the green opens, and it is precisely the bus that gains from an early
   green. Denying it (the literal spec) means red truncation never serves a
   bus waiting at the red. Measured on the same 5 seeds the literal window
   is mixed — total bus wait −1 % but all-crossings median 521 vs 470
   (benchmark demand); −13 % and 74 vs 93 (panel default) — while denying
   every queued bus.
3. **Net-benefit guard**: `cut <= yellow + all_red` → withheld
   (`TSP_CUT_BELOW_CLEARANCE`).
4. A withheld request stays `ARMED` (DBL lane reservation intact) and is
   re-checked as the bus closes in; if the bus crosses untreated it finishes
   **`DENIED`** carrying the last gate reason (`tsp_gate_reason` is also on
   the request snapshot).
5. **`priority_eligibility_px` clamped to the 400 px link** at
   construction/reset, with a logged warning; panel default and slider now
   400. A bus cannot request priority at a node it has not been released
   toward.

Tests: T1–T4 in `tests/test_signal_priority.py`; T5/T6 in
`tests/test_tsp_batch_regression.py` (env-gated, `TSP_BATCH_TESTS=1`).
`src/experiments/headless_run.py` replays `main.py`'s fixed-step loop
without Tk, so a 5-seed batch takes a minute or two.

### 5-seed batch after the fix (T5 / T6)

Headless, 5 × 5 sim-minutes, static TSP flags on R1/R2/R4/R5, median
`node_wait_frames` (n / median / mean):

**Benchmark demand regime** (EB 24, WB 22 Binomial; NB/SB 17/18/14/15 Poisson — the Test Run 3 inputs):

| arm | all crossings | early_green | extending | none | Σ bus wait frames |
|---|---|---|---|---|---|
| TSP off (baseline) | 201 / **774** / 943 | – | – | 201 / 774 / 943 | 191 347 |
| TSP on, pre-patch | 210 / **470** / 738 | 41 / 1171 / 1290 | 28 / 474 / 848 | 141 / 315 / 556 | 155 037 |
| TSP on, patched | 210 / **470** / 738 | 41 / 1171 / 1290 | 28 / 474 / 848 | 141 / 315 / 556 | 155 037 |

**Panel default demand** (EB/WB 12, NB/SB 8):

| arm | all crossings | early_green | extending | none | Σ bus wait frames |
|---|---|---|---|---|---|
| TSP off | 204 / 184 / 311 | – | – | 204 / 184 / 311 | 63 406 |
| TSP on, pre-patch | 205 / 93 / 278 | 50 / 506 / 526 | 36 / 0 / 379 | 119 / 35 / 143 | 56 951 |
| TSP on, patched | 205 / 93 / 278 | 50 / 506 / 526 | 36 / 0 / 379 | 119 / 35 / 143 | 56 951 |

Reading:

- **T5 as written (`early_green` median ≤ `none` median) is not met and cannot
  be met by any timing change** — 1171 vs 315 here, 1148 vs 550 in the field.
  It is the red-arriving subgroup against the green-arriving subgroup. The
  regression test pins the arm-level criterion instead: TSP on ≤ TSP off on
  paired seeds (470 vs 774; 93 vs 184). Both hold, and the field data agrees
  (405–660 vs 935).
- **T6 holds**: `extending` is unchanged to the frame (474 / 0).
- The patched runs are frame-identical to pre-patch: the gate never denied a
  truncation in either regime, because buses are at free flow when they
  trigger (see above). The gate is a guard against the pathological case the
  report described — a slow, distant bus with a request in flight — and T1
  proves it closes that path; it is not what separates treated from untreated
  waits in the benchmark data.
- Improving the mistiming case for real would need the queue ahead of the bus
  in the arrival estimate (arrival ≈ green start + vehicles ahead × saturation
  headway), not just its speed. Not done here: no evidence it would move
  passenger throughput, and it changes which buses get served rather than how
  much any bus gains (a truncation gains at most `cut` ≤ 20 % of the
  conflicting green whichever green the bus takes, because the cycle shift is
  not recovered).

## Defect 2 — the primary DV is a non-converged cumulative average

Confirmed: `pax_per_min` was `passengers_served_total / T`, including ~90 s of
network fill. Fix (commit 2):

- `global_config["warmup_discard_frames"] = 7200` (120 s), recorded in every
  workbook's Control Panel Inputs sheet and as `warmup_discard_sec` in the
  summary CSV.
- `main.snapshot_warmup_baseline` freezes `network_throughput` once at the
  warm-up frame; `_steady_state_metrics` emits `pax_per_min_steady`,
  `steady_window_sec`, `converged` (|ppm(T) − ppm(0.8T)| / ppm(T) < 0.05, from
  the telemetry log) and `*_steady` variants of the five delay columns. The
  cumulative columns are unchanged.
- A summary CSV with an older header is rotated to
  `experiment_summary_<stamp>.csv` untouched; the new file starts with the
  new header (DictWriter would otherwise write ragged rows).
- Default benchmark duration is now 60 min (`test_duration_sim_seconds =
  3600`, combobox preselects "1 hr"); still selectable.
- `python -m src.experiments.rederive_steady_dv "<folder>"` re-derives
  `pax_per_min_steady` and `converged` from existing workbooks' Telemetry
  sheets. Test Run 3 (120 s discard, 5-minute runs → 180 s window):

| workbook | cumulative | steady | converged |
|---|---|---|---|
| baseline s1 / s2 | 533.0 / 534.4 | 649.5 / 673.4 | T / F |
| rule-based s1 / s2 | 553.7 / 527.5 | 654.7 / 630.9 | T / T |
| llama3-latest s1 / s2 | 549.2 / 541.7 | 674.3 / 683.8 | T / F |
| gemini-3.5-flash-lite s1 / s2 | 546.1 / 524.0 | 651.5 / 650.8 | F / F |
| grok-4 s1 / s2 | 552.1 / 534.1 | 682.7 / 671.6 | F / F |
| llama3.1-8b s1 / s2 | 532.6 / 533.9 | 649.1 / 674.1 | T / F |
| nemotron-3-nano-4b s1 / s2 | 533.0 / 533.6 | 651.6 / 674.0 | T / F |

Steady values run ~20 % above the cumulative ones, and half the runs are not
converged at 5 minutes — which is the case for the 60-minute default.

## Defect 3 — baseline arms for one turn

Confirmed, with two leak paths:

1. **Straggler turn.** A model call in flight when the batch runner switches
   the panel to the baseline finishes after `perform_full_reset` has cleared
   the logs, and lands one `OK` turn in the baseline's fresh
   `agent_turn_log.jsonl` (Test Run 3 seed 2).
2. **Sticky route flags.** `_set_ai_flags` writes the model's TSP/DBL flags
   into the live `bus_routes_config`; nothing cleared them at the next run's
   START, so a baseline run following a model run inherited the model's last
   flags and treated buses (Test Runs 1–2, `buses_tsp_treated > 0`).

Fix (commit 3):

- `agent.guard_baseline` runs before anything reaches `decision.json` or the
  turn log: a baseline model (`None`), or a turn whose arm/model changed
  underneath it (control file re-read via `state["control_path"]`), becomes
  `OBSERVATION_ONLY` with all-off flags. Asserted: no `OK` can carry the
  baseline model. `ai_turn` short-circuits for the baseline instead of raising.
- `perform_full_reset` forces all route flags off when a timed test starts
  with `test_model == "None"`.
- `build_experiment_summary_row` excludes `OBSERVATION_ONLY` turns from
  `total_decisions` and raises `BaselineContaminationError` if a baseline row
  shows any decision or any TSP-treated bus; `append_experiment_summary_row`
  refuses the row, prints a banner and sets `test_failed_reason`, which the
  batch poller reports as a `FAILED` run.

Tests: `tests/test_baseline_arm.py` (12 observation-only turns, straggler
downgrade, flag clearing, headless baseline minutes with zero treatment,
contamination refusal).
