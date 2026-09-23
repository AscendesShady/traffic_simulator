# Model validation and verification note

*Prepared to support editorial and peer review of results produced with this
simulator. Structured around the standard verification/validation (V&V)
taxonomy used for simulation studies (conceptual-model validity, verification,
operational validity, data validity — Sargent, 2013, "Verification and
Validation of Simulation Models"), because that is the framework a simulation
methodologist reviewing this work is most likely to apply.*

## 1. What is being claimed, and what is not

This is a microscopic simulation of **two connected signalized intersections**
(a 200 m urban arterial link) carrying general traffic and six fixed bus
routes with Transit Signal Priority (TSP) and a Dynamic Bus Lane (DBL), used
to produce **paired, controlled comparisons of signal-control decision quality**
— e.g. "does TSP-gated priority reduce passenger-hours of delay relative to a
Webster fixed-time baseline, holding demand fixed." It is not offered as a
general-purpose traffic-prediction tool, a certified signal-timing product, or
a claim of numerical parity with any existing simulator.

Stating the boundary of the claim is itself part of the validity argument: a
narrower, explicit claim is easier to defend than a broad one, and every
validity argument below is scoped to *this* claim — decision-quality
comparison under controlled demand — not to "this simulator predicts real
intersection X's throughput."

## 2. Conceptual model validity (face validity)

The traffic-engineering concepts the model is built from are the standard
ones a transportation reviewer will look for, not bespoke inventions:

- **Signal timing** is derived from Webster's method (`src/core/webster.py`,
  pure function, no side effects), the standard cycle/green-split formula from
  the signal-timing literature, applied per node from a measured critical-lane
  flow rather than an assumed one (`webster.movement_lane_flows` walks every
  turning movement through the actual lane geometry).
- **Level of service and delay** follow HCM-style control-delay accounting:
  `mean_control_delay_sec_per_vehicle` measures arrival-to-departure time lost
  below each vehicle's own free-flow speed over its full route, which is the
  same quantity HCM's LOS grading is built on (explicitly labeled a
  network-wide proxy against per-intersection HCM grades, not presented as
  identical to them).
- **Car-following** offers two named, declared models: a calibrated
  gap/speed heuristic ("legacy"), and a full **Intelligent Driver Model**
  implementation per vehicle class ("idm": car/truck/bus each with their own
  T, a, b), which is a peer-reviewed, widely used longitudinal model in the
  transportation-simulation literature (Treiber, Hennecke & Helbing).
- **Lane-changing** under the IDM engine is full **MOBIL**
  (safety criterion + politeness-weighted incentive criterion), the standard
  paired companion model to IDM in the literature (Kesting, Treiber &
  Helbing), not an ad hoc heuristic.
- **Transit priority (TSP) and bus-lane (DBL) logic** model real deployed
  concepts — conditional green extension/early green with a feasibility gate,
  a reserved curb lane with obstruction/eviction rules — rather than an
  idealized "buses always win" abstraction; a request can be denied, gated by
  downstream space, or revoked mid-grant.

A reviewer can check every one of these against a citation without needing to
trust an unnamed internal formula.

## 3. Verification: does the implementation match the intended model?

Verification asks "was the model built right," independent of whether the
model itself is realistic. Evidence:

- **41 automated test modules** (`tests/test_*.py`) exercise signal priority,
  DBL obstruction/eviction/fallback, lane changing, adversarial safety,
  gridlock discharge/recovery, batch execution, calibration, and the AI/rule
  decision path. All are run through `tests/pytest.ini` in CI-equivalent form.
- **A structural safety invariant is continuously tested, not assumed:**
  `tests/test_adversarial_simulation.py` asserts all-pair and swept
  (before-and-after-tick) vehicle non-overlap on every run. Vehicle
  collision is not "usually" prevented — it is checked as a hard test
  invariant every time the suite runs.
- **The simulation step is proven to be single-sourced.** `main.step_simulation`
  is the one and only per-frame authority; both the interactive Tk render loop
  and the headless batch/training runner (`src/experiments/headless_run.py`)
  call it and nothing else. `tests/test_step_equivalence.py` enforces this
  from the source (not just by convention) and by fingerprinting a whole run,
  which forecloses the common simulation-study failure mode of "the code path
  that was validated isn't the code path that produced the reported numbers."
- **Physical/behavioral calibration is pinned by test, not by eyeballing a
  plot.** `tests/test_physical_calibration.py` checks the IDM engine's
  saturation flow against the HCM benchmark (1,913–1,939 veh/hr/lane
  cars-only at full speed scale against an HCM reference of 1,900; ~1,480
  veh/hr/lane at the default half-speed scale), 0→v0 acceleration time
  (4–12 s), peak braking at or below the modeled comfortable deceleration,
  queue spacing at the HCM jam-spacing anchor (7.5 m, from which
  `vehicle.METERS_PER_PX = 0.25` m/px is itself derived and pinned equal to
  `real_world_units.meters_per_pixel()` by a dedicated test), queue-tail
  shockwave speed, MOBIL safety/politeness behavior, and a headless network
  soak with zero overlaps.
- **The AI/LLM and rule-based decision paths are fail-closed by construction
  and by test.** `guard.py` accepts only well-formed, correctly-shaped model
  output; anything malformed, foreign to the current run, or stale becomes a
  logged all-off decision (`HELD_ALL_OFF`), never an unvalidated pass-through.
  This matters for reviewers because it means a language-model or heuristic
  decision source cannot corrupt the physical/safety layer even under
  malformed or adversarial output — the safety-relevant code (`signal_controller.py`)
  is the only thing that can ever grant right-of-way, and it never accepts a
  request it cannot independently verify.

## 4. Operational validity: does the model behave like the real system, for
   the purpose it's used for?

Operational validity is judged against the *stated purpose* (comparing
control strategies), not against "does it reproduce every real intersection
exactly," so the relevant evidence is about controlled, fair comparison, not
photographic realism:

- **Common random numbers (CRN), correctly implemented.** Demand is
  exogenous: every spawn source has its own seeded RNG stream
  (`main.seed_demand_streams`), each vehicle's full attribute set (turn,
  lane, type, speed, color) is drawn once at offer time and queued, and
  `demand_draw_hash` fingerprints the entire offered arrival schedule. A
  controller arm that changes congestion cannot change what demand was
  *offered* — this is the textbook variance-reduction technique for paired
  simulation comparisons (Law & Kelton), and `pair_against_baseline` actively
  refuses to pair two runs whose `demand_draw_hash` differs, rather than
  silently producing a biased comparison.
- **Steady-state bias is removed, not ignored.** Every steady-state dependent
  variable (`*_delay_steady`, `pax_per_min_steady`, `converged`) discards the
  first `warmup_discard_frames` (120 s by default) via a one-time snapshot
  baseline, standard warm-up handling for transient simulation output
  analysis; cumulative columns remain available separately for anyone who
  wants the unfiltered series.
- **The primary dependent variable is passenger-weighted, not
  vehicle-weighted**, and is a paired per-seed difference against a baseline
  run, not a raw arm-level number: `total_person_hours_travel_delay_steady`
  (`net_person_hours_saved = baseline − arm`, split by mode) is written by
  `main.pair_against_baseline` only when both runs agree on every column in
  `PAIRING_MUST_MATCH` (`config_hash`, `git_sha`, `demand_draw_hash`,
  checkpoint minute, test duration) — a pair is refused, with the differing
  columns listed, rather than silently computed on non-comparable runs. A
  regime hash (`config_hash`) is frozen once per run *after* calibration and
  deliberately excludes the treatment itself (TSP/DBL/model), so a controller
  cannot be scored against a different regime than its baseline by
  construction.
- **The baseline arm is actively guarded against contamination.**
  `agent.guard_baseline` forces any baseline or stale-arm decision to
  `OBSERVATION_ONLY`, `perform_full_reset` clears every route flag for a
  baseline test, and `build_experiment_summary_row` raises
  `BaselineContaminationError` — refusing to export the row and marking the
  batch `FAILED` — rather than silently exporting a baseline row that
  actually received treatment. This directly forecloses the most damaging
  possible reviewer objection to a "vs. baseline" claim: that the baseline
  wasn't really a baseline.

### 4.1 Export-time self-checks, and what a "failed" run means

A run's summary row is refused -- the workbook is still written, the batch
marks the run `FAILED`, and the row never reaches `experiment_summary_<YYYYMMDD>.csv`
-- whenever `main._run_export_assertions` finds the row inconsistent with
its own inputs. The checks are:

| Check | Refuses when |
|---|---|
| Steady window | `steady_window_sec != checkpoint − warmup_discard_sec` (only for a positive window) |
| Passenger identity | `passengers_served_total != bus + car` |
| Bus identity | `buses_served` (network throughput) `!= buses_tsp_treated + buses_untreated` (Bus Events log) |
| Person-hours identity | stopped and travel delay totals differ from `bus + car` by more than 1e-4 h |
| Decision cadence | the median realised decision interval deviates > 20 % from the nominal grid (≥ 3 gaps) |
| Baseline contamination | a `model == "None"` run logged any decision, TSP-treated bus or TSP grant (`BaselineContaminationError`) |

A refusal is fail-closed: it cannot produce a wrong row, only a missing one.
The 2026-09-21 campaign (20 runs, 7 refused) is the worked example of both
ways that happens, and both were defects in the *harness*, not the model:

- **Four refusals were the harness contradicting itself by rounding.**
  `total_person_hours_delay` was `round(bus + car, 4)` while the parts were
  rounded separately, so the total sat 1e-4 off the parts' sum for about one
  run in seven, and float noise pushed the difference past the tolerance.
  The travel-delay total already used the sum of the rounded parts; the
  stopped-delay total now does too, and
  `tests/test_experiment_summary.py::test_person_hours_totals_equal_sum_of_rounded_parts`
  runs the assertion over 2,000 random inputs.
- **Three refusals were the test suite deleting a live run's logs.** The
  suite was run while the batch was in flight; tests that call
  `perform_full_reset` with the modules' default paths unlink
  `logs/*.jsonl` of whatever simulator is running, and those that write
  `data/ai_control.json` flip its agent to observation-only. The three runs
  kept their vehicles (the tests are another process) but lost every log
  row before that minute, so the Bus Events sheet disagreed with
  `buses_served` and the bus identity refused the row -- which is the check
  doing its job. `tests/conftest.py::isolate_runtime_files` now points every
  `data/`, `logs/` and `results/` path of every module at `tmp_path` for
  every test, so the suite can no longer touch a live simulator.

The lesson a reviewer should take from this: a refused row is evidence the
identities are actually enforced, and the workbook that survives it (its
Telemetry, Bus Events and AI Decision Audit sheets) is enough to diagnose
which check fired and why.

## 5. Reproducibility

- Every run and every decision carries `run_uuid`, `git_sha`, and
  `config_hash`; a decision not made for the live run/regime is rejected
  (`FOREIGN_DECISION`) rather than silently accepted.
- All cross-process state (`traffic_state_telemetry.json`, `decision.json`,
  and friends) is written atomically (`os.replace` after a temp-file write),
  so no consumer — including the exporter that produces the reported numbers
  — can ever read a half-written file.
- Seeds are explicit and reused deliberately: batch comparisons run paired
  arms on identical seeds, and RL/rule comparators are trained on seeds
  disjoint from the evaluation batch (recorded in the trained policy's
  metadata), which forecloses training/evaluation leakage as an objection.

## 6. An independent structural benchmark: cross-checking against SUMO

Beyond internal tests, this model's behavioral invariants were checked
against the *documented behavioral contracts* of Eclipse SUMO — the
transportation field's de facto reference open-source microscopic simulator —
using a from-source reverse-engineering knowledge base of SUMO's own
architecture and invariants (`reverse_engineering/`, `docs/audits/2026-09-21-sumo-conformance-audit.md`).
This is not a claim of numerical parity with SUMO; it is an independent
checklist of what a credible microscopic traffic simulator is expected to
guarantee (deterministic stepping, signal state fixed before movement
planning, exactly-once insertion/removal accounting, deterministic demand
RNG, named/declared car-following and lane-change models), and this
simulator was checked line-by-line against it. Result: every applicable
invariant matched, with two structural differences identified and now
explicitly documented rather than left implicit:

1. Vehicles plan-and-commit sequentially within a single pass (in spawn
   order), rather than SUMO's two-phase plan-all-then-commit-all — a small,
   unmeasured order effect, not a correctness defect.
2. Right-of-way is **100% preventive** (signal-controller reservations), with
   no collision-detection/teleport recovery layer of the kind SUMO has as a
   backstop — collision is treated as structurally impossible and
   continuously tested against (§3), rather than as a possible outcome that
   is detected and repaired after the fact.

Presenting a known limitation that was actively looked for and precisely
characterized is stronger evidence of rigor than presenting none — it shows
the validation process was adversarial rather than confirmatory.

## 7. Explicitly acknowledged limitations (scope, not defects)

A peer reviewer will trust a bounded, honestly-stated scope far more than an
unbounded claim. This model does **not** attempt, and does not need to claim,
correctness for:

- Pedestrians, rail, or electric-vehicle systems.
- Network topologies beyond two connected signalized intersections (the
  network geometry is parameterized off `canvas_gemini.INT_X`, but no claim
  is made about arbitrary networks).
- Numerical trajectory parity with SUMO, VISSIM, or any other named
  simulator — the car-following/lane-change models are named, cited, and
  independently calibrated against HCM benchmarks, not fit to reproduce
  another tool's output.
- Real-time hardware-in-the-loop signal control; this is explicitly stated
  elsewhere in the project's own documentation as "a simulation and research
  tool, not a certified traffic-signal controller."

## 8. Summary argument

The validity case for this simulator rests on four independent legs, each of
which a reviewer can check directly against cited source and tests rather
than take on faith: (1) the traffic-engineering concepts are the field's
standard, cited models, not bespoke formulas; (2) the implementation is
verified against its own intended behavior by a broad, continuously-run test
suite that includes hard safety invariants (never a "seems to work"); (3) the
experimental methodology (common random numbers, paired comparison, warm-up
discard, baseline-contamination guards, regime-hash discipline) matches
accepted simulation output-analysis practice rather than ad hoc
before/after numbers; and (4) the model's behavioral contracts were
independently cross-checked against a documented reference implementation's
own invariants, with every difference found stated precisely rather than
smoothed over. Combined with an explicitly bounded claim — decision-quality
comparison on a two-intersection corridor under controlled demand, not
general-purpose traffic prediction — this is sufficient evidentiary basis for
a reviewer to trust the *comparisons* this simulator produces, which is the
only thing the results actually claim.
