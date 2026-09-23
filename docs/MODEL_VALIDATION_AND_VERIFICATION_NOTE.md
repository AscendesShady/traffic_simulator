# Model validation and verification note

*Prepared to support editorial and peer review of results produced with this
simulator. Structured around the standard verification/validation (V&V)
taxonomy for simulation studies — conceptual-model validity, verification,
operational validity, data validity (Sargent, 2013) — and, for the traffic
content, around the calibration and validation practice of FHWA's Traffic
Analysis Toolbox Volume III (Dowling, Skabardonis & Alexiadis, 2004).
Every model component below names the published method it implements or
adapts (adaptations are marked), the code that implements it and the test
that pins it, so a reviewer can
check each claim against its source rather than take it on trust. Full
references are in §10.*

*Revision of 2026-09-23. This revision follows SUMO-benchmarked audits
(`docs/audits/2026-09-21-sumo-conformance-audit.md`, and the 2026-09-23
audit whose findings are §4.2) that found defects in the model and the measurement harness
used for every campaign up to and including 2026-09-22. They are listed in
§4.2 with their measured effect; all are corrected. **Results from those
campaigns should not be reported** — the primary dependent variable did not
count the demand held outside the network, the two signals were
uncoordinated, the change intervals were one second each, and the default
movement engine was not physically calibrated.*

## 1. What is being claimed, and what is not

This is a microscopic simulation of **two connected signalized intersections**
(a 200 m urban arterial link) carrying general traffic and six fixed bus
routes with Transit Signal Priority (TSP) and a Dynamic Bus Lane (DBL), used
to produce **paired, controlled comparisons of signal-control decision
quality** — e.g. "does TSP-gated priority reduce passenger-hours of delay
relative to a coordinated Webster fixed-time baseline, holding demand fixed."
It is not offered as a general-purpose traffic-prediction tool, a certified
signal-timing product, or a claim of numerical parity with any existing
simulator.

Stating the boundary of the claim is itself part of the validity argument:
every argument below is scoped to *decision-quality comparison under
controlled demand*, not to "this simulator predicts real intersection X."
The network is synthetic, so operational validity is established against
published standards and internal consistency, not against field counts
(§4, §8).

## 2. Conceptual model validity

Each component is a published, citable method. Where the model departs from
the standard, the departure is stated.

| Component | Standard implemented | Implementation | Pinned by |
|---|---|---|---|
| Car-following (default engine) | Intelligent Driver Model per vehicle class (Treiber, Hennecke & Helbing, 2000; urban parameters after Treiber & Kesting, 2013) | `vehicle._idm_step`, `IDM_PARAMS_MPS` | `test_physical_calibration.py` |
| Braking bound | Class emergency deceleration, SUMO vehicle-type defaults (car 9, truck/bus 7 m/s²) as applied in SUMO's speed finalisation (Lopez et al., 2018) | `vehicle.EMERGENCY_DECEL_MPS2` | `test_standards_conformance.py` |
| Insertion speed | Safe entry speed against the vehicle ahead (SUMO insertion follow speed) | `vehicle.safe_insertion_speed` | `test_standards_conformance.py` |
| Lane changing | MOBIL (Kesting, Treiber & Helbing, 2007); 3 s manoeuvre (Toledo & Zohar, 2007) | `choose_discretionary_lane_mobil`, `LANE_CHANGE_DURATION_S` | `test_physical_calibration.py`, `test_lane_change.py` |
| Green splits and cycle | Webster (1958), critical-lane flow ratios | `webster.compute_node_green_splits` | `test_webster_timing.py` |
| Lost time | HCM 7th ed. (TRB, 2022) Ch. 19: t_L = l1 + (Y + AR − e) per phase; displayed green G = g + l1 − e | `webster.lost_time_seconds` | `test_standards_conformance.py` |
| Saturation flow and start-up lost time | HCM 7th ed. Ch. 31 queue-discharge field procedure (headways from the 5th queued vehicle, uninterrupted discharge) | `main.calibrate_saturation_flow` | `test_webster_timing.py`, `test_standards_conformance.py` |
| Yellow and all-red | ITE (2020) kinematic formulas, Y = t + v/(2a), R = (W + L)/v, bounded by MUTCD (FHWA, 2009) §4D.26 | `webster.ite_change_intervals`, `main.signal_change_intervals` | `test_standards_conformance.py` |
| Dilemma zone | ITE (2020) Type I dilemma zone: go if inside v·t + v²/2a at yellow onset | `vehicle.ite_stopping_distance_px` | `test_standards_conformance.py` |
| Coordination | Common cycle and progression offset (NCHRP Report 812, Urbanik et al., 2015); offset = link travel time at mean car speed; transition by bounded (±20 %) green adjustment each cycle | `SignalController.set_coordination`, `_coordinate` | `test_standards_conformance.py`, `test_webster_timing.py` |
| Near-side turns (left-hand traffic) | Run with their approach's green; no turn on red without a green filter arrow (TSRGD 2016, Schedule 14; Highway Code rule 175). Never cross the opposing stream; conflict only where the square-corner pivot sweeps the adjacent lane (vehicles longer than the 5.5 m lane) | `vehicle.Vehicle.update`, `SignalController._corner_sweep_conflict` | `test_standards_conformance.py`, `test_vehicle_safety.py` |
| Transit Signal Priority | Conditional green extension / early green with arrival gate and recovery (Smith, Hemily & Ivanovic, 2005) | `SignalController._priority_update` | `test_signal_priority.py` |
| Near-side stop check-in | Priority request only after the near-side dwell (Smith, Hemily & Ivanovic, 2005) | `is_bus_tsp_eligible` | `test_standards_conformance.py` |
| Bus dwell | TCQSM 3rd ed. (TCRP Report 165, 2013) Ch. 6: door time + passenger service, parallel doors | `main.dwell_frames_for` | `test_standards_conformance.py` |
| Delay and level of service | HCM signalized-intersection LOS thresholds, applied to delay over the whole route (both nodes for through traffic), so the grade is conservative against HCM's per-intersection control delay | `main.build_experiment_summary_row` | `test_experiment_summary.py` |
| Surrogate safety | Time-to-collision conflicts below 1.5 s (FHWA SSAM, Gettman et al., 2008), counted online rather than from trajectory files | `vehicle.SAFETY_COUNTERS` | `test_standards_conformance.py` |
| Demand-loading check | GEH statistic (FHWA TAT Vol. III), target GEH < 5 on ≥ 85 % of sources. **Adapted:** GEH normally compares model with field counts; with no field counts it is applied here to volume entered vs volume offered, i.e. it tests whether the network admits its demand, not whether the demand is real | `main._network_validation_columns` | — (reported per run) |
| Replications | N = (t · s / e)² (FHWA TAT Vol. III) | `main.required_replications` | `test_standards_conformance.py` |

**Declared departures from the standards.** Traffic drives on the left and
only near-side (left) turns are modelled; they turn at a square corner
without slowing to a turning speed, and far-side (right) turns, pedestrians
and filter arrows are not modelled; bus stops are in-lane (no
bays); bus occupancy is fixed at 45 (boardings equal alightings in
expectation); dwell parameters are TCQSM-range defaults, not calibrated to a
corridor; model parameters (IDM, MOBIL, dwell, change-interval inputs) are
literature values, not fitted to a site; Webster's formula is undefined when
the flow-ratio sum Y ≥ 1, and the cycle is then held at its 120 s cap; the
legacy movement engine (§4.2) remains selectable for comparison but is not a
valid engine for results.

**Physical dimensions** at the model scale (0.25 m per pixel, anchored to
7.5 m queue spacing): cars 4.5 × 2.5 m, trucks 7 × 3 m; desired speeds
32–45 km/h for cars at the campaign speed setting; a 200 m link; 181 m
arterial approaches and **56 m side-street approaches** (to the stop line); lanes **5.5 m** wide,
so the conflict box is 33 m across. The box width is what the all-red must
clear, which gives 3.5 s against ≈ 2.5 s for a standard-width six-lane
crossing: about 2 s more lost time per cycle, identical for every arm.

## 3. Verification: was the model built right?

- **A continuously run suite** (`tests/test_*.py`, through `tests/pytest.ini`)
  exercises signal priority, DBL obstruction/eviction/fallback, lane
  changing, adversarial safety, gridlock discharge, batch execution,
  calibration, and every decision arm through the real guard and merge.
  `tests/test_standards_conformance.py` pins each standard-derived behaviour
  in §2 to its source.
- **Golden-output regression.** `tests/test_golden_regression.py` pins the
  complete end state (every vehicle, both controllers, every counter, the
  demand fingerprint) of two canonical scenarios. Any change to what the
  simulation produces fails the suite until it is deliberately re-pinned and
  committed with its reason — the discipline SUMO's own regression suite
  rests on. The rest of the suite asserts properties; a change that shifted
  every result while keeping each property true could not otherwise be
  caught.
- **Safety is enforced and measured, not assumed.**
  `tests/test_adversarial_simulation.py` asserts all-pair and swept
  (before→after step) non-overlap on every run. Braking is bounded (§2);
  when preventing an overlap needs more than the bound, the move is
  truncated and the event is counted (`ssm_*` columns). The earlier claim
  that collision was "structurally impossible" rested on unbounded braking
  — up to ~90 g — and is withdrawn (§4.2).
- **One frame, one code path.** `main.step_simulation` is the only per-frame
  authority for both the interactive loop and the headless runner;
  `tests/test_step_equivalence.py` enforces it from the source and by
  fingerprinting a whole run.
- **Physical calibration by test.** `tests/test_physical_calibration.py` pins
  the IDM engine to HCM saturation flow (1,913–1,939 veh/h/lane cars-only at
  speed scale 1.0 against HCM's 1,900 base), 0→v₀ in 4–12 s, peak braking
  ≤ b, 7.5 m queue spacing (the anchor `vehicle.METERS_PER_PX` = 0.25 m/px
  is derived from and pinned to), queue-tail shockwave speed, MOBIL safety
  and politeness, and a headless soak with no overlaps.
- **The index that accelerates neighbour queries is proven inert.**
  `tests/test_frame_index.py` asserts whole runs are identical with it on
  and off.
- **Fail-closed decision path.** `guard.py` accepts only well-formed model
  output; anything malformed, foreign to the run, or stale becomes a logged
  all-off decision. A decision source can only *request* priority; the
  signal controller alone grants right of way.

## 4. Operational validity: does it behave credibly for its purpose?

### 4.1 Experimental design

- **Common random numbers** (Law, 2015). Demand is exogenous: each source
  has its own seeded stream, every arrival's attributes are drawn at offer
  time, and `demand_draw_hash` fingerprints the offered schedule up to the
  row's own mark. Each vehicle's behavioural draws come from its own stream
  keyed on its exogenous identity, and each bus trip's dwells from a stream
  keyed on (route, trip). A controller that changes congestion cannot
  change what is offered, how any given vehicle behaves stochastically, or
  how long any bus dwells. A pair whose demand fingerprint differs is
  refused.
- **The primary DV counts all the delay.** `total_person_hours_delay_incl_entry_steady`
  is passenger-weighted time below each vehicle's own free-flow speed
  **plus** the time offered demand waited at the network boundary to enter,
  after a 120 s warm-up. FHWA TAT Vol. III asks that demand unable to enter
  be accounted for; without it an arm that holds traffic outside the model
  is credited for the delay it exported (§4.2 item 1).
- **Pairing contract.** Pairs form only within a campaign and only when
  both rows agree on `config_hash` (regime frozen after calibration,
  excluding the treatment), `git_sha`, `demand_draw_hash`, checkpoint and
  duration; otherwise the pair is refused with the differing columns.
- **Replications.** The end-of-batch summary states, per arm, the runs
  needed for the mean paired DV to lie within ±10 % of itself at 95 %
  confidence, and flags an under-replicated arm.
- **Real-time pace and control delay.** The model runs in real time
  against a simulation paced to one simulated second per wall second
  (fixed 60 Hz step driven by a wall-clock accumulator). The achieved pace
  is recorded per row and flagged beyond ±2 %. Independently of pace, the
  control delay each decision actually suffered is measured on the
  simulation clock — from the frame of the snapshot the decider saw to the
  frame its decision took effect — and reported per decision and as a
  median and 95th percentile per run, so the latency a result depends on
  is a measurement, not an assumption.
- **Baseline integrity.** A baseline row that shows any decision or TSP
  treatment is refused (`BaselineContaminationError`).

### 4.2 Defects found in the 2026-09-22 campaign and corrected

A worked example of the validation process being adversarial rather than
confirmatory. Each defect was found by measurement, not inspection, and
each is now covered by a test.

1. **Latent demand was outside the DV.** Only 58–62 % of offered vehicles
   were served, yet the model reported v/c 0.78–0.91. The wait of arrivals
   held at a blocked source was counted in no DV, and arms admitted
   different amounts of traffic (served fraction 0.58–0.62 across arms).
   In a measured pair the on-road DV improved by 0.41 pax-h while the
   uncounted entry wait grew by ~4.8 pax-h — the sign of the paired result
   could flip. *Corrected:* entry wait is in the primary DV; latent demand,
   GEH and served fraction are reported and flagged.
2. **The two signals were uncoordinated.** Node A ran an 81.6 s cycle and
   Node B 62.0 s, both from phase 0, so their relative offset drifted ≈20 s
   per cycle and progression on the link swept from good to bad within a
   run. *Corrected:* common cycle, progression offset, per-cycle transition.
3. **Change intervals were 1 s yellow and 1 s all-red**, against ITE/MUTCD
   practice; Webster's lost time was consequently 4 s. *Corrected:* ITE
   intervals (3.0 s + 3.5 s at the campaign speed), HCM lost time with
   measured start-up loss (≈12.6 s per cycle).
4. **The default engine was not physically calibrated.** On an
   uninterrupted discharge the legacy engine's saturation flow is ≈2,800
   veh/h/lane at the campaign speed (HCM base 1,900); its measured 1,887
   veh/h/lane was an artefact of queue stalls averaging it down: its
   absolute 27 km/h "standing" threshold made slow vehicles read as
   spillback and stalled the discharge behind them for up to 20 s. About 470
   of its braking events per vehicle-hour exceeded 2 g. *Corrected:* IDM is
   the default; the calibrator excludes interrupted headways.
5. **Saturation flow depended on the seed.** Calibration was seeded from the
   run seed: 1,887 veh/h/lane at seed 234 against 2,338 at seed 764, moving
   the cycle from 81.6 s to its 40 s floor and putting two regimes in one
   campaign. *Corrected:* fixed calibration seed, cached per regime.
6. **13 of 29 arms refused pairing on identical demand.** The demand
   fingerprint was a rolling digest read at export and differed by one
   arrival when the final export landed a frame late. *Corrected:* the
   fingerprint is taken by frame up to the row's mark.
7. **A working model arm scored as a total failure.** A provider timeout
   left its worker holding the call lock and every following turn was
   recorded as a rejected, all-off decision (grok-4.6: 98.6 % "rejects",
   zero effective decisions). *Corrected:* such turns are skipped and
   counted, and the decision interval defaults above the longest provider
   timeout.
8. **Unphysical braking was absorbed silently**, including at insertion
   (36 % of IDM hard-braking came from vehicles entering at full speed
   behind a queue) and at conflict refusals inside the booking distance.
   *Corrected:* bounded braking, safe insertion, reservation look-ahead,
   dilemma-zone rule, near-side turns on green only (item 11); residual events
   counted (§8).
9. **Update order mattered.** Vehicles read neighbours that had or had not
   moved in the same frame depending on list order. *Corrected:*
   plan-then-commit perception and per-vehicle random streams; the residual
   is characterised in §8.
10. **Distinct-vehicle counters undercounted** (147–150 against 168) because
    they were keyed on memory addresses, which CPython reuses. *Corrected:*
    per-run vehicle serials.
11. **Near-side turns ignored the signal and blocked their whole approach.**
    Found by the validation runs, not by inspection: at 0.6× demand, where
    Webster rated both nodes under capacity (Y = 0.70), only 40–49 % of
    offered vehicles were served. A per-source diagnostic showed three
    compounding causes. (a) Near-side turns booked the box on any colour
    whenever no *reserved* vehicle conflicted: 30 of 61 entered on red,
    across the cross street's green, and the all-red then waited for them
    (median 13.0 s at Node A against 3.5 s nominal). (b) Every turning
    vehicle conflicted with every through lane of its own approach until it
    had pivoted, although only a vehicle longer than the lane is wide
    reaches the adjacent lane; the starvation rule then held that approach's
    through traffic for 36 s in 3 minutes at Node A. (c) Each source
    admitted strictly in arrival order, so the head arrival's blocked lane
    held back arrivals bound for free lanes 29–99 % of the time. A
    permitted-left gap check added earlier in this audit (HCM's 4.5 s
    critical headway) was also wrong for left-hand traffic — a near-side
    turn never crosses the opposing stream — and was removed. *Corrected:*
    near-side turns obey the lamp (TSRGD 2016), the corner-sweep conflict
    follows the pivot geometry, and sources admit per lane. The same
    diagnostic found far-node turners stuck in lane 0 at the stop line for
    over 100 s (they began their two lane changes only 62.5 m out) and two
    stale-state leaks that let a vehicle book the box on red; turners now
    position right after the near node, take a counted missed turn after
    10 s, and neither leak survives. Afterwards, at the same demand and
    seed: 0 of 124 near-side turns entered on red, and admission at the
    sources rose from 48–79 % to 85–98 %.

The export-time self-checks (§4.3) and pairing contract are why several of
these surfaced as refused rows rather than wrong numbers.

### 4.3 Export-time self-checks

A run's summary row is refused — the workbook is still written, the run is
marked `FAILED`, and the row never reaches the summary CSV — whenever
`main._run_export_assertions` finds it inconsistent with its own inputs:

| Check | Refuses when |
|---|---|
| Steady window | `steady_window_sec != checkpoint − warmup_discard_sec` (positive window) |
| Passenger identity | `passengers_served_total != bus + car` |
| Bus identity | `buses_served != buses_tsp_treated + buses_untreated` (Bus Events log) |
| Person-hours identity | stopped and travel delay totals differ from `bus + car` by > 1e-4 h |
| Decision cadence | median realised decision interval deviates > 20 % from the nominal grid |
| Baseline contamination | a baseline run logged any decision, TSP-treated bus or grant |

A refusal is fail-closed: a missing row, never a wrong one. The 2026-09-21
campaign (7 of 20 rows refused) traced them to harness defects — a rounding
inconsistency and the test suite deleting a live run's logs — both
corrected and tested (`test_experiment_summary.py`, `tests/conftest.py`).

## 5. Reproducibility

- Every run and decision carries `run_uuid`, `git_sha`, `git_dirty` and
  `config_hash`; the full control-panel input table (at start and at
  export), the signal timing actually used (`signal_timing_json`: Y, AR,
  measured l1, lost time, common cycle, offsets) and the calibration
  provenance are in every workbook and summary row.
- Per-vehicle trip records (depart, arrival, duration, time loss, waiting
  time, depart delay, dwell) are exported for every vehicle, with unfinished
  vehicles written at export, so a reviewer can recompute any statistic
  from individual trips; floating-car trajectories are available on request
  (`fcd_period_s`).
- All cross-process state is written atomically; seeds are explicit and
  arms are paired on identical seeds.

## 6. Cross-check against SUMO

The model's behavioural contracts were checked against a from-source
knowledge base of Eclipse SUMO (Lopez et al., 2018) — `reverse_engineering/`,
`docs/audits/2026-09-21-sumo-conformance-audit.md` — as an independent
checklist of what a credible microscopic simulator guarantees, restricted to
features this model has. No numerical parity is claimed.

| SUMO contract | Status here |
|---|---|
| Fixed step; signal state fixed before movement planning | Implemented |
| Planning precedes execution | **Partial:** perception reads the frame-start state (plan-then-commit); conflict-box reservations are still granted in update order (§8) |
| Emergency-deceleration bound; collision handling as a recorded recovery layer | Implemented: bounded braking; truncations counted per vehicle-hour (§8) |
| Safe insertion speed | Implemented |
| Fixed-time phase and offset semantics | Implemented (common cycle, offsets); no actuated or adaptive control |
| Minor links yield to major traffic | Not applicable: every movement is signal-controlled; near-side turns do not cross the opposing stream (left-hand traffic); no far-side turns, no pedestrians |
| Transit stops with dwell and boarding | **Partial:** in-lane stops with TCQSM dwell; occupancy fixed, no passenger model |
| Per-vehicle trip output incl. unfinished; trajectory output | Implemented (Trip Info sheet; optional FCD) |
| Golden-output regression | Implemented (two scenarios) |
| Deterministic, entity-owned random streams | Implemented (demand, behaviour, dwell) |
| Sub-lane / continuous lateral model, junction internal lanes, routing | Not implemented (outside this study's scope) |

## 7. Explicitly acknowledged limitations

- Two intersections, fixed geometry; no pedestrians, rail or right turns.
- **No field data.** The network is synthetic and no parameter is fitted to
  a site. Validation is against published standards, reference values and
  internal consistency (FHWA TAT Vol. III's calibration steps that need
  field counts or travel times cannot be performed). Results are therefore
  evidence about the *relative* performance of control strategies under
  standard traffic behaviour, not predictions for a real corridor.
- Lanes are 5.5 m wide at the kinematic scale (§2), lengthening the all-red
  and each lane change; the effect is common to every arm.
- North–south approaches are 56 m, so under heavy demand queues reach the
  boundary. Latent demand is measured and charged in the DV, but a
  boundary that truncates queues is itself a limitation; FHWA TAT Vol. III
  advises extending the network until latent demand is negligible. Report
  `latent_demand_share_at_end` with any result.
- Saturation flow is calibrated on one straight lane in isolation; the
  in-network value is measured and reported beside it, not fed back.
- Box reservations are first-come in update order; the residual order
  sensitivity is quantified in §8.
- The reservation and square-corner turning abstraction still produces
  some braking-bound events and recovery truncations; they are counted,
  not eliminated.
- The legacy engine must not be used for results.
- LLM arms are non-deterministic across repeats of a seed; only demand,
  behaviour and dwell draws are common.
- The evidence in §8 was produced headless from the working tree before
  the fixes were committed; regenerate it from the commit the dissertation
  cites and quote that `git_sha`.

## 7a. Questions an examiner is likely to ask

**Why not use SUMO or VISSIM?** The study needs an LLM in the control loop,
paired common-random-number arms and per-decision audit records, and a TSP/DBL
state machine whose every gate is inspectable. Building on published
behavioural models (§2) and checking the result against SUMO's documented
contracts (§6) keeps the behaviour standard while keeping the experiment
under control. The claim is limited to what this requires (§1).

**Is it calibrated?** Against standards, yes: saturation flow on the driving
engine reproduces HCM's base rate at the reference speed (§3), change
intervals follow ITE, lost time follows HCM. Against a site, no, and the
note says so (§7). The DV is a paired *difference*, so parameter error that
affects both arms alike largely cancels. Parameters that interact with the
treatment — bus dwell, bus desired speed, the side-street storage — do not
cancel; a sensitivity run on each is the honest answer where the result
depends on it.

**Could the TSP benefit be an artefact of the simulator?** Four guards: the
baseline and treatment share demand, behaviour and dwell draws, and a pair
is refused if its demand fingerprint differs; the primary DV includes delay
held outside the network, so an arm cannot win by admitting less traffic;
the baseline is refused if it shows any treatment; and the run-to-run noise
floor is measured (§8), so an effect smaller than that floor is not
reported as an effect.

**Is the model deterministic?** For a given seed, code and configuration,
yes: golden-output regression pins two scenarios' complete end state (§3).
Changing only the update order still moves results, by an amount compared
with ordinary behavioural noise in §8.

**Why is so much demand held at the boundary?** The side streets store
about seven cars per lane (56 m). Under heavy demand the queue overflows
and the wait is charged to the DV (§4.1). Results should be reported at a
demand level where latent demand is small (§8) or with the latent share
stated beside them.

## 8. Measured evidence (validation run, 2026-09-23)

*Pending.* The validation campaign — two seeds × two demand levels
(campaign and 0.6×) × baseline and TSP+DBL, 20 min each, plus four
behavioural realisations and a reversed update order per seed and demand
level — is being re-run on the model as corrected in §4.2 item 11. Its
signal timing, calibrated and in-network saturation flow, demand loading
(served fraction, latent share, GEH), coordination adherence, surrogate-safety
rates, bus dwell, demand-fingerprint equality and noise floor will be
reported here. Figures from the run before that correction are superseded
and are not quoted.

## 9. Summary argument

The validity case rests on four legs, each checkable against cited sources
and tests: (1) every model component is a published traffic-engineering or
simulation standard, named in §2 with the code and test that implement it;
(2) the implementation is verified by a continuously run suite that
includes golden-output regression, hard safety invariants with measured
safety surrogates, and single-code-path enforcement; (3) the experimental
method — common random numbers across demand, behaviour and dwell, a
primary DV that counts latent demand, a strict pairing contract, warm-up
discard, replication adequacy and real-time pace — follows accepted
simulation output-analysis practice; and (4) the model was cross-checked
against a reference simulator's documented contracts, and the defects that
process found (§4.2) are stated with their measured effect rather than
smoothed over. Combined with an explicitly bounded claim, this is the basis
on which the *comparisons* this simulator produces can be trusted — and the
reason results from before this revision should not be.

## 10. References

- Dowling, R., Skabardonis, A., & Alexiadis, V. (2004). *Traffic Analysis
  Toolbox Volume III: Guidelines for Applying Traffic Microsimulation
  Modeling Software* (FHWA-HRT-04-040). Federal Highway Administration.
  (Updated 2019 as FHWA-HOP-18-036.)
- Federal Highway Administration. (2009). *Manual on Uniform Traffic
  Control Devices for Streets and Highways*, 2009 Edition, Section 4D.26
  (Yellow Change and Red Clearance Intervals). (Renumbered in the 11th
  Edition, 2023.)
- Gettman, D., Pu, L., Sayed, T., & Shelby, S. (2008). *Surrogate Safety
  Assessment Model and Validation: Final Report* (FHWA-HRT-08-051). Federal
  Highway Administration.
- Institute of Transportation Engineers. (2020). *Guidelines for
  Determining Traffic Signal Change and Clearance Intervals* (ITE
  Recommended Practice). Washington, DC: ITE.
- Kesting, A., Treiber, M., & Helbing, D. (2007). General lane-changing
  model MOBIL for car-following models. *Transportation Research Record*,
  1999, 86–94.
- Kittelson & Associates, Parsons Brinckerhoff, KFH Group, Texas A&M
  Transportation Institute, & Arup. (2013). *Transit Capacity and Quality of
  Service Manual*, Third Edition (TCRP Report 165). Transportation Research
  Board.
- Law, A. M. (2015). *Simulation Modeling and Analysis* (5th ed.).
  McGraw-Hill.
- Lopez, P. A., Behrisch, M., Bieker-Walz, L., Erdmann, J., Flötteröd,
  Y.-P., Hilbrich, R., Lücken, L., Rummel, J., Wagner, P., & Wießner, E.
  (2018). Microscopic traffic simulation using SUMO. In *21st IEEE
  International Conference on Intelligent Transportation Systems (ITSC)*,
  2575–2582. (Vehicle-type defaults: SUMO documentation, "Vehicle Type
  Parameter Defaults".)
- Sargent, R. G. (2013). Verification and validation of simulation models.
  *Journal of Simulation*, 7(1), 12–24.
- Smith, H. R., Hemily, B., & Ivanovic, M. (2005). *Transit Signal Priority
  (TSP): A Planning and Implementation Handbook*. ITS America / U.S.
  Department of Transportation.
- Department for Transport. (2016). *The Traffic Signs Regulations and
  General Directions 2016* (SI 2016/362), Schedule 14 (light signals).
  London: The Stationery Office.
- Department for Transport & Driver and Vehicle Standards Agency. (2022).
  *The Highway Code*, rule 175 (traffic light signals). London: The
  Stationery Office.
- Toledo, T., & Zohar, D. (2007). Modeling duration of lane changes.
  *Transportation Research Record*, 1999, 71–78.
- Transportation Research Board. (2022). *Highway Capacity Manual, 7th
  Edition: A Guide for Multimodal Mobility Analysis*. Washington, DC: The
  National Academies Press. Chapters 19 (Signalized Intersections) and 31
  (Signalized Intersections: Supplemental).
- Treiber, M., Hennecke, A., & Helbing, D. (2000). Congested traffic states
  in empirical observations and microscopic simulations. *Physical Review
  E*, 62(2), 1805–1824.
- Treiber, M., & Kesting, A. (2013). *Traffic Flow Dynamics: Data, Models
  and Simulation*. Springer.
- Urbanik, T., Tanaka, A., Lozner, B., Lindstrom, E., Lee, K., Quayle, S.,
  Beaird, S., Tsoi, S., Ryus, P., Gettman, D., Sunkari, S., Balke, K., &
  Bullock, D. (2015). *Signal Timing Manual*, Second Edition (NCHRP Report
  812). Transportation Research Board.
- Webster, F. V. (1958). *Traffic Signal Settings* (Road Research Technical
  Paper No. 39). London: HMSO.
