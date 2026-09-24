# Traffic simulator: methodology and architecture

> Single authoritative description of the simulator, replacing the former
> *Architecture and Reuse Guide* and *Guide and Documentation*. Part I is
> written so that a manuscript's methodology section can be extracted from it
> directly: every model, parameter, unit and measure is stated in physical
> units with the simulation-unit value beside it, and every value was read
> from the source on 2026-09-21. Part II is the implementation reference for
> anyone changing the code. Where this document and the source disagree, the
> source wins; the constants named here are the place to look.

---

## Part I — Methodology

### 1. Purpose and scope of the model

The simulator is a time-stepped microscopic simulation of a two-intersection
urban arterial segment carrying mixed traffic (cars, trucks) and fixed-route
buses. Its purpose is the **controlled, paired comparison of transit-priority
decision policies** — Transit Signal Priority (TSP) and a Dynamic Bus Lane
(DBL) granted by a rule, a pressure heuristic or a large language model — against a fixed-time Webster baseline, holding
network, demand and vehicle behaviour identical across arms. The objective
function is passenger-weighted: person-hours of travel delay, not vehicle
counts.

It is a research instrument, not a certified signal controller, and it makes
no claim of numerical parity with any commercial or open-source simulator.
Its behavioural invariants were cross-checked against Eclipse SUMO's
documented contracts (`docs/audits/2026-09-21-sumo-conformance-audit.md`);
the validity argument for reviewers is in
`docs/MODEL_VALIDATION_AND_VERIFICATION_NOTE.md`.

### 2. Units, time step and physical scale

| Quantity | Value | Source |
|---|---|---|
| Length scale | 1 px = 0.25 m | `vehicle.METERS_PER_PX`, anchored to the HCM jam spacing of 7.5 m per queued car (18 px car + 12 px standing gap); a test pins it equal to `real_world_units.meters_per_pixel()` |
| Time step | Δt = 1/60 s (60 Hz fixed step) | `main.step_simulation`, `vehicle.FPS` |
| Speed unit | 1 px/frame = 15 m/s = 54 km/h | derived |
| Acceleration unit | 1 px/frame² = 900 m/s² | derived |

Physics always advances in fixed 1/60 s steps; the "simulation speed" control
only changes how many fixed steps are consumed per wall-clock tick (with a
bounded catch-up), never the time step. The headless batch runner and the
interactive window call the same `step_simulation` function and nothing else
(`tests/test_step_equivalence.py` enforces this from the source and by
fingerprinting a whole run).

### 3. Network

A 1,200 m × 700 m surface (4800 × 2800 px) carrying one east–west arterial
and two north–south cross streets. `canvas_gemini` derives every position
from three lengths (`LINK_M` = 500, `APPROACH_M` = 350, `PX_PER_M` = 4).

| Element | Value |
|---|---|
| Signalised nodes | A at x = 350 m (1400 px), B at x = 850 m (3400 px); link A–B = 500 m |
| Approach lengths | 350 m on every approach, edge to node centre (331 m to the stop line): arterial upstream of A and downstream of B, and each cross street either side of the arterial |
| Lanes | 3 per direction on every approach, 5.5 m (22 px) wide; carriageway 33 m (132 px) |
| Conflict area | 33 m × 33 m square per node |
| Stop line | 2.5 m (10 px) upstream of the conflict area |
| Driving side | **Left-hand traffic** (eastbound on the north carriageway). A "left" turn is therefore the near-side turn: it does not cross opposing through traffic, and its only conflict is the corner sweep of the adjacent through lane of its own approach. Far-side (crossing) turns are not modelled. |
| Lane roles | Lane 0 nearest the centre line, lane 1 middle, lane 2 kerb-side. Straight traffic uses lanes 0–1; every left turn is made from lane 2, which is also the bus lane a DBL reserves |

Geometry constants live in `src/ui/canvas_gemini.py` (`INT_X`, `H_Y`,
`LANE`, `ROAD_W`, `STOP`); `docs/NETWORK_GEOMETRY.md` is generated from them.
Nothing downstream hard-codes a node position — route waypoints, Webster
splits, discharge plans and the test helpers all derive from `INT_X`, and
the geometry is part of the run's `config_hash` so results from different
network lengths never pair.

### 4. Vehicles

| Class | Length × width | Passengers | Free-flow speed at default speed scale 0.5 |
|---|---|---|---|
| Car | 4.5 m × 2.5 m (18 × 10 px) | 4 | U(7.5, 10.5) m/s = 27–38 km/h |
| Truck (heavy) | 7.0 m × 3.0 m (28 × 12 px) | 1 | U(6.0, 8.25) m/s = 22–30 km/h |
| Bus | 10.5 m × 3.5 m (42 × 14 px) | 45 | 7.5 m/s = 27 km/h |

Every vehicle draws its own desired speed (`max_speed`) at generation from
the uniform ranges above (base draws of U(1.0, 1.4) and U(0.8, 1.1) px/frame
for cars and trucks, 1.0 for buses, multiplied by `vehicle_speed_scale`, a
run-level knob defaulting to 0.5). Driver heterogeneity is therefore
explicit, and every delay measure is taken against the vehicle's *own*
free-flow speed, not a lane limit.

### 5. Longitudinal movement

Two named movement engines exist and are selected per run by
`global_config["movement_model"]` (default `"idm"`, §5.2); the selection is
part of `config_hash`, so runs from different engines never pair.

**5.1 Legacy following rule (comparison only).** Retained so earlier
results can be reproduced, not for benchmark results: measured on an
uninterrupted discharge its saturation flow is ≈2,800 veh/h/lane at speed
scale 0.6 (HCM base 1,900), about 470 of its braking events per
vehicle-hour exceed 2 g, and its absolute standing threshold (0.5 px/frame,
27 km/h) makes any slower vehicle — every truck at scale ≤ 0.6 — read as a
spillback queue, stalling the discharge behind it for up to 20 s. Every
campaign up to 2026-09-22 used it. With bumper-to-bumper gap *g* to
the leader in the same lane and desired speed *v*max:

- *g* < 3 m (12 px, `SAFE_GAP_PX`): target speed 0;
- 3 m ≤ *g* < 9.25 m (37 px, `FOLLOW_FREE_GAP_PX`): target = min(*v*max, (*g*/30 px)·*v*max);
- *g* ≥ 9.25 m: target = *v*max.

Speed ramps toward the target at ±0.05 px/frame² per step in both directions
(45 m/s² at scale — visually plausible, physically uncalibrated; retained
because every pinned regression result was produced with it). A red or
yellow signal, a spillback hold or a refused entry reservation stops the
vehicle at the stop line from within 15–25 px. Lateral moves slide at
0.5 px/frame (7.5 m/s), about 0.7 s per lane. Discretionary lane changes
between lanes 0 and 1 are a MOBIL-lite rule: incentive is the following-speed
gain as a fraction of the driver's own desired speed (inward requires +20 %,
outward tolerates −10 %, i.e. keep-outer), safety is nobody alongside in the
target lane and ≥ 7.5 m (30 px) clear behind, considered with a 1/120
per-frame hazard (mean 2 s) from the run-seeded RNG, and never within 37.5 m
(150 px) of a stop line.

**5.2 Intelligent Driver Model (`"idm"`).** Per vehicle class
(Treiber, Hennecke & Helbing 2000; urban values after Treiber & Kesting 2013):

a = a₀ [ 1 − (v/v₀)⁴ − (s*/s)² ],  s* = s₀ + max(0, vT + vΔv / (2√(a₀b)))

| Class | T (s) | a₀ (m/s²) | b (m/s²) |
|---|---|---|---|
| Car | 1.0 | 2.0 | 2.0 |
| Truck | 1.7 | 0.8 | 1.5 |
| Bus | 1.5 | 1.2 | 1.5 |

with s₀ = 3 m (so the jam-spacing anchor is identical in both engines) and
v₀ = the vehicle's own drawn desired speed. A red/yellow stop line, a hold
point and a full receiving lane are treated as standing obstacles the model
brakes toward at its comfortable rate. Lane changes are full MOBIL (Kesting,
Treiber & Helbing 2007): politeness p = 0.3, switching threshold 0.1 m/s²,
follower safety limit 4 m/s², keep-outer bias 0.3 m/s²; a change takes
3 s (`LANE_CHANGE_DURATION_S`, Toledo & Zohar 2007). A vehicle counts as
standing below 1.5 m/s.

`tests/test_physical_calibration.py` pins the IDM engine to reference
values: saturation flow from the production calibrator of 1,913–1,939
veh/h/lane (cars only, speed scale 1.0; HCM base 1,900) and ≈1,480 at the
default 0.5 scale; 0→v₀ in 4–12 s; peak braking ≤ b; queue spacing 7.5 m;
queue-tail shockwave −6 to −20 km/h; lane change 3 s; MOBIL safety and
politeness; and a headless network soak with zero overlaps.

*Braking bound and recovery.* The IDM law has no deceleration limit; the
engine floors each step at the class emergency deceleration (car 9.0,
truck and bus 7.0 m/s², SUMO's vehicle-type defaults). When that is not
enough, the move is truncated at the leader's rear bumper or strictly short
of the stop line and the event is counted (`vehicle.SAFETY_COUNTERS`,
reported per vehicle-hour with TTC conflicts below FHWA SSAM's 1.5 s) —
collision is prevented, and every instance of the prevention being
super-physical is on the record. Vehicles enter the network at the speed
from which they could stop behind the vehicle ahead at the comfortable rate
(SUMO's insertion follow speed). A vehicle inside its braking distance of a
stop line asks the controller whether it would be granted entry
(`entry_would_be_granted`, side-effect free) and brakes comfortably for a
box that will not open, rather than learning it at the 6.25 m booking
distance. Cooperative holds (yield to a merging bus, hold behind a DBL bus)
are a comfortable stop in place.

**5.3 Within-frame update order.** Vehicles are updated one at a time, but
every neighbour query (leader gap and speed, followers, lane bands,
receiving space) reads the state each vehicle had at the start of the frame
(`vehicle._seen`), so perception is plan-then-commit as in SUMO. Behavioural
random draws come from each vehicle's own stream keyed on its exogenous
identity (source and arrival index; route and trip), not from a shared
stream consumed in update order. Box reservations remain first-come in list
order. The residual order sensitivity is measured in the validation note.

### 6. Turning movements, lane assignment and route progression

Each approach's arrivals split into a straight share (`turn_split`) and one
or two left options from `control_panel.APPROACH_TURN_OPTIONS`: EB/WB may
turn left at the near or the far node; A_SB and B_NB may make one left or a
double left (left at the first node, then left again at the second); A_NB
and B_SB meet one node and have one option. Straight arrivals are placed in
lane 0 or 1 with equal probability; a left at the first node is placed in
lane 2 at the source; a far-node left rides lanes 0–1 through the near node
and starts working into lane 2 as soon as it has passed it (a driver
positions for the next junction; two 3 s lane changes do not fit in the
last 62.5 m of a queued link). It must be in lane 2 to turn and holds at the
stop line until it is, booking nothing. "In lane 2" is physical: a vehicle
still sliding out of lane 2 (an eviction for a bus's DBL) counts as out of
it, is held, and slides back unless the bus's DBL keeps it out; a lane
commanded in the AI Configured mode is shared with left-turners and never
evicts them. One still out of its lane after
10 s at the line takes the missed turn and goes straight, counted as
`missed_turns_total`. After any left the vehicle lands in the exit
road's lane 2 and keeps it. Buses follow six fixed routes (§8). A vehicle is
removed, and its passengers credited to throughput, only when it leaves the
surface **and** has recorded at least one node crossing; a crossing is
recorded only when the vehicle's rear has cleared the conflict area.

### 7. Demand generation

Demand is exogenous and drawn per source. Each of the six sources
(EB, WB, A_NB, A_SB, B_NB, B_SB) owns a `random.Random` stream seeded from
the run seed; at every 1/60 s tick it decides whether to *offer* an arrival,
draws the arrival's complete attribute set (turn option, lane, class,
desired speed, colour) at that moment, and appends it to a source queue
(capacity 5,000; overflow is counted). An arrival is *admitted* — becomes a
vehicle — only when the entry is clear (no vehicle within the lateral block
zone at the spawn point); a blocked entry delays it and never redraws it.
Consequently a control policy that changes congestion cannot change what
any source offered, and `demand_draw_hash` — a hash over every
(source, offer frame, attributes) tuple offered up to the row's own mark —
fingerprints the offered schedule for the pairing check (it is keyed by
frame, so an export landing a frame late cannot change it). Vehicle
attributes are never drawn at admission time nor from the global RNG;
behavioural draws (the lane-change hazard) use each vehicle's own stream
keyed on (seed, source, arrival index), so they too are common across arms.
The time an offered arrival waits at a blocked source before becoming a
vehicle is latent demand: it is recorded per trip (`entry_delay_frames`,
SUMO's departDelay) and integrated per frame into the primary DV (§11).

Arrival processes per source (rate λ in veh/min, λ_f = λ/3600 per frame):

| Model | Rule |
|---|---|
| Poisson | one arrival with probability 1 − e^(−λ_f) each frame (Bernoulli thinning at frame resolution) |
| Binomial | metered: an arrival with probability 2λ_f, but only once at least 1800/λ frames (half the mean headway) have elapsed since the last |
| Negative binomial | a burst of 1–3 vehicles starts with probability λ_f/2; bursts release one vehicle every 18 frames |
| Congestion peak | alternates 30 s at max(4λ, 90 veh/min) with 30 s at λ |

Current defaults (`control_panel.approach_configs`):

| Source | Model | Rate (veh/min) | Straight | Second-left share | Heavy |
|---|---|---|---|---|---|
| EB | Binomial | 34 | 75 % | 11 % | 10 % |
| WB | Binomial | 32 | 80 % | 10 % | 10 % |
| A_NB | Poisson | 24 | 75 % | — | 15 % |
| A_SB | Poisson | 27 | 75 % | 10 % | 15 % |
| B_NB | Poisson | 25 | 75 % | 10 % | 15 % |
| B_SB | Poisson | 22 | 75 % | — | 15 % |

### 8. Bus routes and dispatch

Six fixed routes defined once in `control_panel.bus_routes_config`, each
with the movement and lane the bus uses at every node it meets:

| Route | Path | Node movements (lane) | Headway | Default |
|---|---|---|---|---|
| R1_EB_A_NB | EB, left at A to NB | A: LEFT (2) | 180 s | active |
| R2_EB_B_NB | EB through A, left at B to NB | A: STRAIGHT (1), B: LEFT (2) | 240 s | active |
| R3_EB_ONLY | EB through both | A, B: STRAIGHT (1) | 300 s | active |
| R4_WB_A_SB | WB through B, left at A to SB | B: STRAIGHT (1), A: LEFT (2) | 240 s | active |
| R5_WB_B_SB | WB, left at B to SB | B: LEFT (2) | 240 s | active |
| R6_WB_ONLY | WB through both | B, A: STRAIGHT (1) | 180 s | inactive |

Each route has stops (`stops`, default one far-side stop at the route's
first node — the placement transit-priority guidance pairs with TSP). A bus
brakes for its stop, dwells, and continues; dwell follows the Transit
Capacity and Quality of Service Manual (3rd ed., Ch. 6): door time 4 s plus
the longer of boardings × 3 s and alightings × 2 s (two-door bus), with
Poisson boardings and alightings of mean 4 each (mean dwell ≈ 19 s,
8–34 s observed). Dwells are drawn per (seed, route, trip index), so every
arm sees identical dwells, and boardings equal alightings in expectation,
so occupancy stays 45. A near-side stop holds the bus 7.5 m short of the
stop line and withholds priority at that node until it has been served.

Departures are trips: one pending trip is appended per elapsed headway (and
per manual dispatch) and trips leave in order as the entry clears, so
departures due while the entry is blocked accumulate rather than vanish.
Each dispatch records scheduled and actual departure time, source delay and
the pending backlog; trips held by a blocked entry are counted once in
`bus_trips_missed`.

### 9. Signal control

**9.1 Baseline: coordinated fixed-time Webster.** Each node runs a
two-phase cycle EW green → EW yellow → all-red → NS green → NS yellow →
all-red. Yellow and all-red follow ITE's kinematic formulas (Guidelines for
Determining Traffic Signal Change and Clearance Intervals, 2020):
Y = t + v/(2a), R = (W + L)/v, with t = 1.0 s, a = 3.05 m/s², L = 6.1 m, v
the 85th-percentile desired speed and W the stop line to far box edge
(35.5 m), bounded by MUTCD (2009) §4D.26 guidance — 3.0 s and 3.5 s at
speed scale 0.6. Lost time is HCM's t_L = l1 + (Y + AR − e) per phase
(HCM 7th ed., Ch. 19; e = 2 s), with the start-up lost time l1 *measured*
on the driving engine by the calibrator (≈1.5–2.3 s under IDM at scale
0.5–0.6; HCM default 2.0 s if the first discharge headways are
interrupted): ≈12.6 s per cycle at scale 0.6, against 4 s under the former
1 s + 1 s. Both nodes run one common cycle — the longer of their own Webster
cycles, each node re-split on its own flow ratios — with Node B offset from
Node A by the link travel time at the mean car desired speed in the
progression direction (EB by default; NCHRP Report 812, Signal Timing
Manual 2nd ed.). Every time a node starts its EW green the controller
compares the start with the master schedule and spreads the error over that
cycle's greens, at most 20 % of each green per cycle; this returns a node to
coordination after a TSP action, an all-red that waited for a blocked box,
or a discharge episode. `"signal_coordination": "independent"` restores the
former per-node cycles, whose relative offset drifts (81.6 s against 62.0 s
on seed 234, ≈20 s per cycle). Nodes still hold separate phase, timer,
clearance, reservation and priority state.

Green splits come from Webster's method with a per-run *measured*
saturation flow S. Per node, each phase is represented by its heaviest
lane: y_EW = q_EW,crit / S, y_NS = q_NS,crit / S, Y = y_EW + y_NS. Critical
lane flows come from a node-by-node movement matrix
(`webster.movement_lane_flows`) that walks every (source, turn option) pair
through the lanes §6 assigns, so a side-street left that lands in the
arterial's lane 2 loads that lane at the *next* node. Then

C_opt = (1.5 L + 5) / (1 − Y),

bounded to 40–150 s (`webster.MIN_CYCLE_SEC`, `max_cycle_sec`, default 150 s,
within the Signal Timing Manual's range for large intersections, NCHRP Report
812) and, when
Y ≥ 1 and the optimum is undefined, set to the maximum; the source is
reported (`webster_optimal`, `min_cycle_floor`, `max_cycle_cap`,
`oversaturation_cap`). The maximum binds below saturation too: the optimum
diverges as Y → 1 (371 s at Y = 0.94, 4,111 s at Y = 0.99 on this network),
and until 2026-09-24 it was applied only at Y ≥ 1. At 150 s a node stays
below capacity up to Y = (C − L)/C ≈ 0.916. The value was chosen by a
sensitivity run on this network (baseline arm, seeds 234/764, 15 min): against
120 s, 150 s cut total person-hours by 10 % at 0.7× campaign demand (which then
runs its own 132 s optimum), 7 % at 0.8× and 3 % at 1.0×; 180 s added under 1 %
while bus delay rose with the longer reds. Effective green C − L is split in proportion to
y, and converted to displayed green G = g + l1 − e so the controller's real
cycle equals the cycle Webster chose. Minimum green is 5 s (300 frames).
Saturation flow is measured once per regime (speed scale, vehicle mix,
movement engine; with a fixed calibration seed, never the run seed) by the
HCM queue-discharge method (`main.calibrate_saturation_flow`):
a standing queue of 30 vehicles is built on one EB lane behind red with no
downstream interference, released with no new arrivals, the first 4
departures are discarded as start-up lost time (and measure l1), headways
above 2.5 × the median are dropped as an interrupted discharge (HCM 7th ed.,
Ch. 31), and S is the reciprocal of the mean of the rest (default fallback
1,366 veh/h/lane if the run yields none). Under IDM with 10 % heavy vehicles
S ≈ 1,340 veh/h/lane at speed scale 0.5 and ≈1,460 at 0.6; cars-only figures
are in §5.2. Because this is one lane in isolation, the network's own
saturation flow is also measured during every run from stop-bar headways
(§11) and reported beside it. Calibration happens *after* the
movement model is selected and *before* the regime hash is frozen, so S and
the splits describe the engine that actually drives.

**9.2 Right-of-way and conflict safety.** The signal controller
(`SignalController`) is the sole authority over entry to a conflict area.
Green is necessary but not sufficient. From within 6.25 m (25 px) of the
stop line on green, a vehicle must (i) not be held by a route-exit merge,
(ii) have receiving space in the specific lane its movement enters (free
road from the conflict area to the tail of the nearest standing vehicle,
at least its own length plus 3 m — the spillback hold; this is the same
`receiving_space_px` telemetry and the TSP gate read), and (iii) obtain a
movement reservation. A reservation is refused while any live reservation
or occupant conflicts with it: movements on perpendicular axes always
conflict; on the same axis only a near-side (left) turn and its own
approach's traffic can conflict, and only where the turn's square-corner
pivot physically sweeps the other vehicle's lane: half the turning
vehicle's length either side of its lane centre, which passes the 5.5 m
lane edge only for a vehicle longer than a lane is wide (a truck by
0.75 m, a bus by 2.5 m, never a car), and then only into the adjacent lane.
The conflict is released dynamically once the corner is clear. A through
entry in a swept lane is refused once a waiting turner has been denied for
1 s (60 frames), so a queue of through traffic cannot starve the turn.
Near-side turns obey the signal like through traffic -- no turn on red
without a green filter arrow (TSRGD 2016; Highway Code rule 175) -- and,
being near-side turns in left-hand traffic, never cross or yield to the
opposing stream. At
yellow onset a vehicle inside its ITE stopping distance (v·t + v²/2a, the
same t and a the yellow is computed from) commits to go and is treated as
facing green until the next green — the Type I dilemma-zone rule the change
interval is designed for; beyond it the yellow is an obstacle like red. A
node is added to a vehicle's `passed_nodes` only when its rear has cleared
the conflict area, and all-red must find the area empty before the next
green starts. Overlap is prevented by construction and continuously tested
(`tests/test_adversarial_simulation.py`, all-pair and swept non-overlap);
where prevention needs more than the braking bound (§5.2) it is counted,
not hidden.

**9.3 Transit Signal Priority.** TSP is a bounded perturbation of the
running Webster cycle, never a phase override. A bus becomes eligible when
its route flag is on for the leg it is on and it is within the eligibility
distance of the stop line (default 100 m = 400 px; the panel offers
62.5–350 m and the controller clamps it to the 500 m A–B link). One request per (bus, node, leg) is
queued per node; the head request is *armed* and may receive **one**
action:

- **Green extension** — if the bus's phase is green when it would end, the
  green is held, up to a cap of 20 % of that phase's green
  (`TSP_MAX_ADJUST_FRACTION`), *only if* the bus's predicted arrival
  (`vehicle.bus_eta_frames`: under IDM the unimpeded time to accelerate from
  its current speed to its desired speed at the bus a₀ and cruise — how
  deployed TSP predicts arrival from a check-in point; the same estimator
  telemetry and the comparators read) falls within the cap; otherwise the
  green ends on time and the request stays armed.
- **Early green (red truncation)** — if the conflicting phase is green, it
  is shortened by up to the same cap, never below the 5 s minimum green,
  *only if* the cut exceeds yellow + all-red and the bus's predicted
  crossing, max(ETA, start of the advanced green), falls inside the green
  the cut brings forward. The gate is re-evaluated every frame as the bus
  closes in.

Both actions are additionally refused every frame while the bus's receiving
lane cannot take it (`TSP_DENY_DOWNSTREAM_BLOCKED`). A request whose bus
crosses untreated finishes `DENIED` with the gate reason
(`TSP_ETA_OUTSIDE_GREEN_WINDOW`, `TSP_CUT_BELOW_CLEARANCE`,
`DOWNSTREAM_BLOCKED`); a request unserved for 120 s (7,200 frames) times
out. Request states: `REQUESTED → ARMED → {TSP_EXTENDING | TSP_EARLY_TRUNCATE} → {COMPLETED | DENIED | CANCELLED}`.
Every extension and truncation passes through yellow and all-red; the
intersection-clear wait is never skipped.

A route flag is a *continuous hold*, not a one-off command: the controller
re-checks it every frame the request lives and cancels the request the
frame it drops (`FEATURE_DISABLED`), after which that bus's leg is
suppressed and never re-requested. Every decision arm therefore keeps a
route's flags as they are from the moment the controller holds a request
for one of its buses until the bus is served (`agent.check_locked`,
published to the model as `LOCKED_ROUTES` and enforced server-side in
`agent.anti_cheat`); an arm decides only *new* requests. Before the lock
covered the armed phase, every arm -- rule and pressure included --
re-derived its grants from each 4 s snapshot and withdrew most of them
mid-approach (cross-street load ticking over the bus's 45 passengers, or
the bus's landed ETA reaching zero at the bar): in the 2026-09-21
campaign `FEATURE_DISABLED` was 9 of the rule arm's 9 TSP denials and
29 of llama3.1's 34, and the buses concerned stopped for 13–33 s.
The snapshot lock alone still leaked (Run 8, 2026-09-21: 45 of 83
denials): a stale decision (`max(3 × tick, 12 s)` = 15 s at the 5 s tick then in use,
shorter than a slow model's latency), a guard-held all-off, or a decider
whose snapshot predated the request all cleared the flags. The lock is
therefore also enforced at the merge boundary from the *live* controller:
`main._set_ai_flags` leaves untouched every route in
`SignalController.routes_with_live_requests()`, whatever the decision (or
refusal) says, so a decision can only ever gate *new* requests and
`FEATURE_DISABLED` is reachable only by an operator toggling a route off.

**9.4 Dynamic Bus Lane.** A DBL reserves the kerb lane (lane 2) on the
bus's approach leg from the frame its request is raised until the bus has
cleared the node. The node's one armed slot (§9.3) is about TSP only: a DBL
is a lane reservation on the bus's own approach and touches no signal timing,
so it holds while its request is queued as well, several buses may hold one
approach's lane at once, and the opposite approach's lane is independent.
Through cars in that lane
are ordered to vacate (unconditionally along the whole approach, not only
near the bus); new left-turn arrivals, which must use lane 2, are retained
at the source, and left-turners already on the approach hold in their
general lane at the stop line rather than enter the reserved lane. The bus
merges into lane 2 when the lane is usable. A DBL request or grant is
vetoed whenever a standing or crawling vehicle is ahead of the bus in the
reserved lane (`vehicle.dbl_lane_queue_ahead`: standing ≤ 0.05 px/frame,
or ≤ 35 % of its own desired speed with a leader within 9 m), even once the
bus is in the lane; the veto is re-run every frame the request lives and
revokes DBL (`DBL_REVOKED_LANE_BLOCKED`) if a vehicle the gate accepted
while moving later stands ahead, the request continuing as plain TSP if it
also asked for TSP. With both treatments on, a bus that enters the zone
before reaching lane 2 checks in for TSP alone; when it reaches lane 2 that
request takes DBL on and its entry lane moves with the bus
(`SignalController._collect_priority_requests`). At the default 100 m zone
buses are in lane 2 well before the zone, so this matters only for zones that
begin before the merge (the 350 m option starts at the network entry).
Until 2026-09-24 DBL was granted only to the node's armed request, so a bus's
lane reservation waited behind any other request at that node; in the TSP+DBL
arm, buses that had given DBL up kept raising TSP requests that held the slot
for up to the 120 s request timeout, left-turners refilled the unprotected
lane and the next bus gave DBL up too, so lane 2 was reserved 44–318 s per
15-minute run against 3,000–4,800 s in the DBL-only arm. DBL-only and
TSP+DBL results from before that date measure the coupled mechanism. An optional
(non-route-required) merge is abandoned
after a bounded 5 s (300 frames) wait, or immediately and stickily on a
veto, so a DBL can never leave a bus worse off than baseline; a
route-required merge (the bus needs lane 2 to turn) is never abandoned.

**9.5 Gridlock discharge (operator/automatic recovery).** When queues lock
the network the controller can take exclusive ownership: arrivals, bus
dispatch and priority are suspended; recovery greens are exclusive per
node, staged downstream-first (EB opens B before A, WB opens A before B),
and each begins only after yellow, minimum all-red, conflict-area
clearance and receiving-space validation. Recovery greens last 3–10 s
(`discharge_min_green` 180, `discharge_max_green` 600 frames). Normal
timing resumes through yellow and all-red once both conflict areas are
empty, followed by a 10 s metered re-admission period. `GridlockMonitor`
samples every telemetry payload and reports onset/clearance by sustained
stopped share (crawl ≤ 0.1 px/frame counted as stopped) and a diagnosis of
queue heads stopped with an empty lane ahead.

### 10. Decision sources (the experimental arms)

All arms share one mechanism: Webster timing plus the TSP/DBL state
machine of §9. They differ only in *who sets the per-route TSP and DBL
request flags*, so a paired difference isolates decision quality. Every
arm's output passes the same validator (`guard.py`): exactly two boolean
lists of the canonical route count (`ROUTE_ORDER`, the six route IDs
sorted), anything else — wrong length, non-boolean, malformed JSON, an
exception, a timeout — becoming a complete all-off decision
(`HELD_ALL_OFF`) logged to `agent_rejects.log`. The live simulation merges
the latest guarded decision every 0.5 s (30 frames) and rejects one whose
timestamp is missing or older than max(3 × tick, 12 s), whose `run_uuid` or
model does not match the live run (`FOREIGN_DECISION`), or whose turn is not
newer than the last merged (`STALE_DECISION`), falling back to all-off.
A flag is a continuous hold (§9.3): every arm decides only the routes the
controller does not yet hold a request for, `agent.check_locked` /
`anti_cheat` keep a locked route's flags as they are, whatever the arm
returns for it, and the merge itself (`_set_ai_flags`, including the
all-off of every refusal) skips any route with a live controller request.
The tick is 2–120 s (`control_panel.TICK_SECONDS_MIN/MAX`, a slider on
both run cards), default **10 s**, one value for every arm. It follows from
the bus: an entering bus reaches Node A's stop line about 39 s after
entering (350 m at 9 m/s) and is inside the TSP eligibility zone for its
last 100 m (the default; configurable up to 350 m), about 11 s, so a 10 s
tick gives every bus about four decision points on its approach and at
least one inside the zone, where a 60 s tick left many reaching Node A
before any decision had seen them. (Route flags are a
continuous hold, so a long tick does not stop buses being treated; it
reduces the decider to a once-a-minute route switch.) The tick must also
clear the slowest arm's p95 latency: the agent skips-and-counts a grid
point that comes due while a call is still running (`SKIPPED_SLOW`), so a
tick below a model's latency measures a disconnected loop (Run 8 lost a
median 65 of 99 turns at 5 s with the slow models of the time; r = −0.87
between median latency and the share of decisions issued). The kept local
models answer in 0.7–1.7 s median, 2.0 s worst; the campaign summary names
any arm that skipped more than 5 % of its points, and one stuck call can
cost at most ⌈45 s / tick⌉ of them. The stale window follows as 3 × tick.

**Local model selection (2026-09-23).** Each installed Ollama model was run
through the agent's real turn on five telemetry snapshots (2.2–3.0k-token
prompts, 0.6× campaign demand, 6–16 buses) on the study machine (Ryzen 7
5800X, RTX 3070 8 GB, Ollama 0.34), with a campaign-density simulation
running alongside to measure the frame-time cost. Every model gets the same
8,192-token context (`agent.OLLAMA_NUM_CTX`; left to Ollama, the window
follows the model's maximum and small models spilled onto the CPU), 6
inference threads, and the simulator runs at above-normal priority. The
latency bars were set on the 200 m network of the time: a bus took about
22 s from entering to the first stop line, so a decision had to land within
that (worst turn ≤ 22 s) and, for the median bus, within half of it
(median ≤ 11 s). (On the 500 m network the approach is 350 m, about 39 s,
and the default eligibility zone 100 m, about 11 s; the kept models answer
in under 2 s either way.)

| Model | Placement | Median | Worst | Valid | Arm |
|---|---|---|---|---|---|
| llama3.2:3b | 100 % GPU | 0.7 s | 1.0 s | 5/5 | kept |
| phi3:3.8b | 100 % GPU | 1.4 s | 2.0 s | 5/5 | kept |
| llama3:latest (8B) | 100 % GPU | 1.5 s | 1.8 s | 5/5 | kept |
| llama3.1:8b | 100 % GPU | 1.7 s | 2.0 s | 5/5 | kept |
| gemma4:latest | 100 % GPU | 17.8 s | 26.1 s, then 45 s timeout | 4/5 | dropped: 1.3–2.3k reasoning tokens a turn |
| orca-mini:7b | 100 % GPU | — | 45 s timeout | 1/2 | dropped: 4k native context, timed out |
| nemotron-3-nano:4b | 100 % GPU | — | 45 s timeout | 0/1 | dropped: reasoning model, timed out |
| gemma4:12b | 31 % CPU | — | 45 s timeout | 0/1 | dropped: 8.9 GB exceeds VRAM |

With the kept models running, the simulator's frame stayed inside its
16.67 ms budget at campaign density. A 50-turn follow-up per kept model gave p95 latencies of 0.89 s
(llama3.2:3b), 1.76 s (phi3:3.8b), 1.85 s (llama3:latest) and 1.94 s
(llama3.1:8b), 200/200 valid, frame 11.6–12.9 ms: the 10 s decision tick
is at least 5× the slowest arm's p95. Model size alone did not predict speed:
before the context was fixed, phi3:3.8b ran 81 % on the CPU at 16.7 s while
the older 8B llama3 ran on the GPU at 1.5 s. Re-run the selection if the
GPU, the Ollama version or the prompt changes.

| Arm (`ai_runtime["model"]`) | Decision rule |
|---|---|
| **Baseline** (`None`) | No decisions and no treatment: `agent.guard_baseline` turns every turn into `OBSERVATION_ONLY`, the reset clears all route flags, and a summary row showing any decision or TSP treatment raises `BaselineContaminationError` (row refused, batch marked `FAILED`). |
| **Rule-based** (`rule-based`) | Deterministic conditional TSP: for each approaching bus that would otherwise stop at red and whose receiving lane is free, grant if the cross street's queued passengers < 45 (one bus load, `RULE_CROSS_QUEUE_THRESHOLD_PAX`); score = bus passengers − cross-street passengers; at most 1 grant per node per decision. DBL for every approaching bus unless telemetry reports the lane obstructed or queued. Zero latency by construction. |
| **Passenger-pressure TSP** (`passenger-pressure-tsp`) | Same DBL rule and per-node cap; TSP granted when the bus approach's pressure (queued passengers upstream plus the bus load, zero if its downstream is blocked) exceeds the conflicting approaches' summed pressure. It is a TSP *gate*, not a phase-selecting max-pressure controller. |
| **LLM, assisted** (any Ollama tag or Gemini model) | A separate-process LangGraph loop reads the telemetry snapshot every tick (2–120 s, default 10 s), renders a structured text "minimap" (per-node competing queues in passengers, per-route bus positions/ETAs/loads, receiving-lane state, lane obstruction) and asks the model for `{reason, tsp[6], dbl[6]}` with a prompt stating that Webster is the competent baseline, congestion is failure, all-off is often correct, and a bus must remain actionable after expected inference latency. A server-side `anti_cheat` step forces `dbl=False` for any new grant where telemetry shows the lane obstructed. Ollama calls time out at 45 s, Gemini at 30 s (temperature 0.2); a time-out is held all-off, and grid points that come due while the abandoned call still holds the provider are skipped (`SKIPPED_SLOW`), not held. |
| **LLM, decided** (`<model> [decided]`) | `control_mode = "configured"`: the model writes the timing plan itself — per node EW and NS green (clamped 5–90 s), `end_current_green_now`, and four commanded DBL lanes — through the same single writer and identity checks; `SignalController.apply_plan` is the only entry, Webster and the TSP state machine are off, and a commanded lane is shared with left-turners. This arm is a different mechanism and is paired only against the Webster baseline, never against an assisted arm as "decision quality". |

Every AI/rule turn is logged with the exact telemetry snapshot it saw,
its reasoning, latency and tokens (`agent_turn_log.jsonl`), and exported as
a State → Action + Reasoning → Outcome audit row whose outcome fields are
the state at the following decision.

### 11. Measures

All counters are accumulated once per simulated frame in
`main.accumulate_frame_metrics` and zeroed only by a full reset.

| Measure | Definition |
|---|---|
| Passenger throughput | Σ passengers of vehicles that left the surface with ≥ 1 rear-clear node crossing (car 4, truck 1, bus 45); also per mode, per vehicle, and as passengers/min cumulative and recent |
| **Delay incl. entry wait (primary)** | on-road travel delay (next row) **plus** passenger-hours of offered demand waiting at a blocked source (cars 4 / trucks 1 per queued arrival, 45 per pending bus trip, every frame). Without the second term an arm that holds traffic outside the model is credited for the delay it exported (FHWA Traffic Analysis Toolbox Vol. III). |
| Travel delay (on-road) | per vehicle per frame, lost = 1 − min(v, v_free)/v_free with v_free the vehicle's own desired speed; person-hours = Σ passengers × lost / (60 × 3600). Split bus/car. Counts crawl, not only stops. |
| Stopped delay (proxy) | frames with v < 0.25 px/frame (3.75 m/s); passenger-weighted per mode; labelled a proxy and never used to grade LOS |
| Control delay per vehicle | Σ lost frames over the whole route / 60 (both nodes for through traffic — conservative against a per-intersection HCM grade) |
| Level of service | HCM signalised-intersection thresholds on mean control delay: A ≤ 10, B ≤ 20, C ≤ 35, D ≤ 55, E ≤ 80, F > 80 s/veh |
| Per-vehicle means | every mean (`mean_*_delay_sec`, node means, live telemetry) divides *completed*-vehicle counters by the served count; the censored remainder is reported separately as `unfinished_stopped_person_hours` and `passenger_hours_in_network`, so an unfinished vehicle never sits in a numerator without its denominator |
| Bus service | trips scheduled / pending / missed, source delay, per-bus TSP/DBL event log (request, arm, action, gate reason, adjust frames, outcome) |
| Cross-street cost | `tsp_window_cross_street_person_hours`, descriptive only |
| Latent demand | vehicles still waiting to enter at the mark and their share of offered vehicles; mean entry delay per served vehicle |
| Capacity validation | in-network saturation flow from stop-bar headways (5th queued vehicle on, uninterrupted discharge, HCM Ch. 31) and its ratio to the calibrated S; GEH of entered vs offered hourly volume per source (FHWA TAT Vol. III target GEH < 5 on ≥ 85 %) |
| Surrogate safety | TTC conflicts (< 1.5 s, FHWA SSAM), braking-bound events and recovery clamps, each per vehicle-hour |
| Pace | achieved sim-seconds per wall-second; must be 1.0 for decision latency to mean what the agent assumes |
| Control delay | per decision, sim seconds from the telemetry frame the decider saw to the first frame the decision took effect (grid-point detection + model call + 30-frame merge), measured on the sim clock: `control_delay_sim_s` (audit sheet), `control_delay_sim_sec_median/_p95` (summary) |
| Trip records | one per vehicle (depart, arrival, duration, time loss, waiting time, depart delay, dwell, credited) in the workbook's Trip Info sheet, unfinished vehicles appended at export (SUMO tripinfo); optional floating-car data (`fcd_period_s`) |
| Calibration record | every run workbook's Calibration sheet (`main.calibration_rows`): network scale, demand, vehicle parameters, the saturation-flow sample (queue size, headways used and excluded, mean headway, S, measured l1), ITE inputs and intervals, HCM lost time, per-node Webster derivation (critical lane flows, y, Y, C0, cycle used and its source, displayed greens, X), coordination and priority settings, each with its unit and source; `calibration_report_<campaign>.html` gathers it per regime with each run's validation checks (`src/telemetry/calibration_report.py`, written at batch end) |
| Gridlock | onset/clearance times, stopped share, queue-head diagnosis |
| Real-world readouts | v/c per approach from `approach_critical_lane_flow_veh_hr` (so v/c and Webster's y agree by construction), queue lengths in m, downstream space per lane in m — display/export only, never fed back |

### 12. Experimental design and statistical treatment

- **Common random numbers.** Paired arms run on identical seeds; the
  offered demand (§7) is provably identical across arms
  (`demand_draw_hash`), as are each vehicle's behavioural draws and each
  bus trip's dwells, giving the standard variance-reduction design for
  paired simulation comparisons (Law, 2015).
- **Two-phase execution.** The non-LLM arms (baseline, rule-based,
  passenger-pressure-tsp) run first as parallel headless processes
  (`src/experiments/parallel_campaign.py`), several at once on the CPU; the
  LLM arms then run one at a time in the windowed application, with the
  model on the GPU and the simulation alone on the CPU, joining the same
  campaign. Every run follows the same per-run path (reset, frame step,
  telemetry, checkpoints, export), and runs are deterministic: a run's end
  state is bit-identical whether it ran alone or beside seven others
  (measured). Running the LLM arms alone keeps their conditions identical
  to one another: with six headless runs beside it, a model's latency was
  unchanged within noise but the windowed simulation slowed by 15–22 %.
- **Replications.** `print_campaign_summary` states, per arm, the runs
  needed for the mean paired DV to lie within ±10 % of itself at 95 %:
  N = (t₀.₉₇₅,ₙ₋₁ · s / e)² (FHWA Traffic Analysis Toolbox Vol. III), and
  flags an under-replicated arm.
- **Warm-up.** Every steady-state DV (`*_steady`, `converged`) discards the
  first 300 s (`warmup_discard_frames` = 18,000, about 2.5 crossings of the
  1.2 km arterial) via a snapshot taken once per
  run; cumulative columns are kept alongside. `converged` requires the
  cumulative pax/min within 5 % between 0.8 T and T *and* `vehicles_in_network`
  within 10 % over the same span -- a cumulative mean is stable by
  construction and on its own flagged runs whose accumulation was still
  climbing at the horizon. `delay_sec_per_pax_served_steady` (steady travel
  delay ÷ passengers served in the window) is the cross-arm comparable form
  of the DV: raw person-hours reward serving fewer people and
  `served_fraction_pax` moves with the offered demand, which differs
  between arms.
- **Duration and checkpoints.** Default benchmark 60 min; summary rows are
  written at 5, 10, 15, 30, 60 and 120 min marks, each carrying
  `checkpoint_min`.
- **Regime freezing.** `config_hash` is computed once at the end of the
  full reset, after calibration, over every regime-defining input (geometry,
  demand model signature, movement model, speed scale, approach and route
  configuration, Webster inputs) and deliberately excludes the treatment
  (`tsp_enabled`, `dbl_enabled`, `manual_dispatch`, model) — the arm is what
  the `model`/`control_mode` columns say.
- **Pairing.** `main.pair_against_baseline` forms a pair only within one
  campaign and only when the two rows agree on `config_hash`, `git_sha`,
  `demand_draw_hash`, `checkpoint_min` and `test_duration_min`
  (`PAIRING_MUST_MATCH`); otherwise the pair is refused and listed with the
  differing columns. The primary DV is
  `net_person_hours_saved = baseline − arm` on
  `total_person_hours_delay_incl_entry_steady`, with bus/car split, the
  on-road-only variant (`net_person_hours_saved_on_road`) and the
  stopped-delay variant, one row per (campaign, seed, arm). No per-run
  column claims "net saved": the baseline is another run.
- **Baseline integrity.** See §10; a contaminated baseline row is refused
  rather than exported.
- **Export self-checks.** A summary row is refused (the workbook is still
  written; the run is marked `FAILED`; nothing reaches
  the dated summary CSV) when `main._run_export_assertions` finds it
  inconsistent with its own inputs: steady window ≠ checkpoint − warm-up,
  passengers served ≠ bus + car, buses served ≠ treated + untreated (Bus
  Events log against network throughput), a person-hours total ≠ bus + car
  (totals are the sum of the rounded parts), or a median realised decision
  interval more than 20 % off the nominal grid. A refusal is fail-closed --
  a missing row, never a wrong one -- and the surviving workbook (Telemetry,
  Bus Events, AI Decision Audit) is enough to diagnose which check fired.
  `docs/MODEL_VALIDATION_AND_VERIFICATION_NOTE.md` §4.1 works through the
  2026-09-21 campaign, where 7 of 20 rows were refused.
- **Provenance.** Every run carries `run_uuid`, `git_sha`, `git_dirty`,
  `config_hash`, `demand_draw_hash`, movement-model signature, the full
  control-panel input table and the Webster calibration outputs
  (S, C, y, Y, cycle source) in its workbook and in
  `results/experiment_summary_<YYYYMMDD>.csv` (dated by the day the campaign
  started, so one batch is one file; schema version 9; a CSV whose header no
  longer matches is rotated, never appended to ragged).
- **Golden outputs.** `tests/test_golden_regression.py` pins the complete
  end state of two canonical scenarios; any change to what the simulation
  produces fails until deliberately re-pinned (`REPIN_GOLDEN=1`) and
  committed with its reason.

### 13. Verification summary

41 pytest modules (`tests/`), run through `tests/pytest.ini`. Those that
bear directly on model validity: `test_physical_calibration.py`
(§5.2 pins), `test_real_world_units.py` (unit anchors),
`test_webster_timing.py` (Webster equations and run-start calibration),
`test_adversarial_simulation.py` and `test_vehicle_safety.py` (non-overlap,
spillback, reservations), `test_signal_priority.py` and
`test_tsp_batch_regression.py` (TSP/DBL lifecycle and the seed-batch pin,
`TSP_BATCH_TESTS=1`), `test_dbl_lane_clearing.py` /
`test_dbl_merge_fallback.py` / `test_dbl_obstruction_telemetry.py` (DBL
veto and fallback), `test_step_equivalence.py` (one frame, one code path),
`test_baseline_arm.py` and `test_experiment_summary.py` (baseline
integrity, pairing contract, DV definitions), `test_rule_controller.py`,
`test_max_pressure_and_rl.py`, `test_llm_control_loop.py` and
`test_ai_configured.py` (every decision arm through the real guard and
merge), `test_gridlock_monitor.py`, `test_turn_options.py`,
`test_lane_change.py`, `test_motion_tuning.py`, and
`test_standards_conformance.py` (each standard-derived behaviour pinned to
its source: ITE/MUTCD intervals, HCM lost time, near-side turn signal
compliance and corner-sweep geometry,
coordination and recovery, dilemma zone, braking bound and SSM, insertion
speed, order-independent perception, latent demand, trip records, TCQSM
dwell and near-side check-in, FHWA replications), `test_campaign_integrity.py`
and `test_golden_regression.py`.

### 14. Limitations to state with any result

- Two intersections, fixed geometry, left-hand traffic, near-side turns
  only; no pedestrians, no far-side/crossing turns, no right turns.
- The legacy engine is not physically calibrated (§5.1) and must not be
  used for benchmark results; campaigns before 2026-09-23 used it.
- Box reservations are first-come in update order (§5.3); the residual
  order sensitivity and its size relative to ordinary run-to-run variation
  are reported in the validation note.
- The reservation/turning abstraction still leaves some braking-bound
  events and recovery clamps (square-corner turns at speed, conflicts that
  appear inside the booking distance); they are counted and reported per
  vehicle-hour, not eliminated. Turning speed is not reduced before a turn.
- North–south approaches are 56 m long, so under heavy demand queues reach
  the network boundary; the resulting latent demand is measured and charged
  in the primary DV, but a boundary that truncates queues is itself a
  limitation (FHWA TAT Vol. III advises extending the network until latent
  demand is negligible).
- Saturation flow is calibrated on one straight lane in isolation; the
  in-network value is measured and reported beside it, not fed back.
- Bus stops are in-lane (no bays); occupancy is fixed at 45 (boardings equal
  alightings in expectation). Dwell parameters are TCQSM-range defaults,
  not calibrated to a corridor.
- The network is synthetic: validation is against published standards and
  internal consistency, not field observations.
- Queue passenger *estimates* in live telemetry use 4 passengers per queued
  vehicle (the aggregated queue counter does not retain class); served
  throughput uses actual occupancy.
- LLM arms are non-deterministic across repeats of the same seed; only the
  demand is common.

---

## Part II — Implementation reference

### 15. Process topology

One Tk process hosts everything except model inference. `main.build_main_window()`
lays out three panes: the operator control panel (`control_panel.py`), the
simulation canvas (Pygame draws each frame onto an offscreen surface that is
pushed into a Tk canvas), and the telemetry dashboard
(`telemetry_dashboard.TelemetryDashboard`, mounted in-process). Only
`src/agents/agent.py` runs as a `subprocess.Popen` child, so inference latency
can never block the 60 Hz loop. One `WM_DELETE_WINDOW` handler stops the run,
terminates the agent and triggers the `atexit` export. `main.main()` takes an
OS lock on `data/simulator.lock` so two simulators never share the runtime
files.

### 16. Module ownership

| Concern | Owner | Consumers |
|---|---|---|
| Road geometry, rendering | `src/ui/canvas_gemini.py` | main, vehicle, signal_controller, telemetry |
| Operator configuration (`global_config`, `approach_configs`, `bus_routes_config`, `CONTROL_STRATEGIES`) | `src/ui/control_panel.py` | everything |
| Vehicle list, simulation clock, spawner, dispatch, metrics, exports, batch runner | `src/core/main.py` | controller, exporter, renderer |
| Vehicle kinematics, lane changes, spillback, route progression | `src/core/vehicle.py` | main, controller, exporter |
| Right-of-way, reservations, TSP/DBL state machine, discharge, `apply_plan` | `src/core/signal_controller.py` | vehicles, renderer, telemetry |
| Webster splits (pure) | `src/core/webster.py` | main, telemetry, UI |
| Live snapshot (`traffic_state_telemetry.json`) | `src/telemetry/telemetry_exporter.py` | dashboard, agent |
| Output validation (fail-closed) | `src/core/guard.py` | agent ⇄ main boundary |
| LLM / rule turn loop, single decision writer | `src/agents/agent.py` | writes `decision.json` only |
| Rule and pressure comparators | `src/agents/rule_controller.py` | agent |
| Headless engine, batch | `src/experiments/headless_run.py`, `batch_runner.py` | tests, training, campaigns |
| Parallel non-LLM campaign phase | `src/experiments/parallel_campaign.py` | campaigns (phase one) |
| Progress records and their monitor | `src/experiments/progress.py`, `run_monitor.py` | headless runs, campaigns, pytest sessions |
| Calibration report | `src/telemetry/calibration_report.py` | `print_campaign_summary`, CLI |
| Real-world unit conversion (display/export only) | `src/telemetry/real_world_units.py` | dashboard, exports |
| Bus TSP/DBL event log | `src/telemetry/bus_event_log.py` | exports |
| Gridlock monitor | `src/telemetry/gridlock_monitor.py` | main, `<workbook>_gridlock.xlsx`, CLI `--watch` |
| Offline saturation-flow diagnostic | `src/telemetry/measure_saturation.py` | research only, not imported at runtime |

The central invariant: a decision source may only *request* route flags (or,
in configured mode, a plan through `apply_plan`); `SignalController` alone
decides entry, clearance, reservations and spillback protection.

### 17. One frame

`main.step_simulation(vehicles, signals, frame, decide)`, in order:

1. unless discharge owns the network: offer/admit arrivals per source,
   dispatch due bus trips;
2. `decide(frame)` — production merges the guarded `decision.json` every 30
   frames; a trainer or replay supplies its own;
3. `signals.update(vehicles)` — phases, priority state machine, reservations;
4. one `get_all_signals` snapshot, then every vehicle's `update()` against
   it, with node-crossing recording, completion/throughput credit and bus
   event tracking;
5. `bus_event_tracker.observe`, `accumulate_frame_metrics`,
   `snapshot_warmup_baseline`.

The Tk callback and `headless_run.run` call this and nothing else; the Tk
callback additionally renders (interpolating between the last two physics
positions) and exports telemetry every `TELEMETRY_LOG_INTERVAL` = 60 frames.

### 18. Run lifecycle

- **START / RESET** — `perform_full_reset(vehicles, signals, telemetry)`:
  clears vehicles, controller, logs, throughput, spawner state and the
  exported-workbook flag; selects the movement model; seeds the demand
  streams; calibrates S and applies Webster; freezes `config_hash`; issues a
  fresh `run_uuid`; clears every route flag for a baseline test; restarts at
  frame 0.
- **STOP** — halts stepping, preserves vehicles/logs/throughput for export.
- **PAUSE / RESUME** — freezes in place.
- **Batch** — `poll_batch_runner` runs the queued (seed × arm) matrix; *Stop*
  pauses in place (timeout budget excludes the pause) and splits into
  *Resume* | *End*; *End* discards the run in flight and the queue, disarms
  the model and performs the full reset.
- **One workbook per run** — `export_test_workbook` (timed tests, batch runs,
  checkpoint marks) sets `_run_exported_workbook`; the `atexit` cleanup
  writes the session workbook only when that is still false.

### 19. Cross-process contract

All files use atomic replace (`os.replace` after a temp-file write) and are
gitignored.

| File | Writer | Reader | Content |
|---|---|---|---|
| `data/ai_control.json` | control_panel | agent | armed, model, control mode, tick seconds, sim-running |
| `data/traffic_state_telemetry.json` | telemetry_exporter | dashboard, agent | authoritative snapshot (`schema_version`, signal state incl. plan, queues, passengers, routes, active/approaching buses with ETA, `receiving_blocked`, `receiving_space_m`, `bus_distribution`, `downstream_space_m_by_lane`, discharge, `run_uuid`, frame) |
| `data/decision.json` | agent (single writer `publish_decision`) | main | guarded flags or `signal_plan`, `run_uuid`, `model`, `telemetry_frame`, turn, timestamp |
| `logs/telemetry_log.jsonl`, `agent_turn_log.jsonl`, `bus_events.jsonl`, `agent_rejects.log` | main / agent / guard | exports | append-only observability |
| `results/experiment_summary_<YYYYMMDD>.csv`, `paired_dv_<campaign>.csv`, `*.xlsx` | main | analysis | results |

Every key `run_forever` puts into the LangGraph state must be declared on
`agent.AgentState` — LangGraph silently drops undeclared keys, which once
left `run_uuid` empty and every live decision refused as foreign while the
unit tests stayed green; `test_llm_control_loop` now asserts identity through
the compiled graph.

### 20. Control strategy selector

`control_panel.CONTROL_STRATEGIES` groups deciders under Baseline,
Rule-Based, AI/LLM Assisted and AI/LLM Decided; the two LLM
strategies are the two control modes (`STRATEGY_CONTROL_MODE`). One
backend-neutral model string flows through `ai_runtime["model"]`,
`set_active_ai_model`, `ai_control.json`, the batch's `set_model` and every
export column. Add a decider by adding it to `strategy_models`; never teach
the agent or the exporter a family. `rule_controller.is_rule_model` is true
for every non-LLM arm and gates the no-latency/no-token handling.

### 21. Working rules

- Never hard-code a node x: use `canvas.INT_X[0]/[1]`
  (`control_panel.NODE_A_X/NODE_B_X`, `tests.helpers.NODE_A/NODE_B`).
- Change right-of-way only in `signal_controller.py` together with the
  `vehicle.py` request contract; never let a vehicle or a decision source set
  green directly.
- Change the LLM-facing schema in `agent.py` and `guard.py` together and
  mirror it in `rule_controller.py` if it must stay a fair comparator.
- Change telemetry fields in the exporter, dashboard, agent minimap and
  export mappings together; bump `schema_version` for breaking changes.
- Add a turning option only in `control_panel.APPROACH_TURN_OPTIONS`; the
  spawner and the Webster matrix follow.
- Keep `DEMAND_MODEL_SIGNATURE` and `movement_model_signature` bumped when
  the generator or the engine changes.
- Webster *output* is shown only in the dashboard's Summary tab; the control
  panel holds operator inputs.
- Run the suite through `tests/pytest.ini` (`--capture=sys` is load-bearing
  for Tk); after a movement/signal change also run a windowed run and inspect
  `traffic_state_telemetry.json`. `tests/conftest.py::isolate_runtime_files`
  points every `data/`, `logs/` and `results/` path of every module at a
  per-test temp directory (path defaults are resolved at call time, never
  bound as default arguments, so the redirect holds); still, never run the
  suite while a batch is in flight -- a live simulator's files are in the
  same working tree.
- Never narrow the route lock (`agent.check_locked`) back to granted /
  clearing requests: an armed request cancelled by a dropped flag ends
  `FEATURE_DISABLED` and that bus's leg is suppressed for good.
- Docs: `docs/NETWORK_GEOMETRY.md` and the routes table in
  `docs/SIMULATION_INPUT_PARAMETERS.md` are generated by
  `docs/generate_geometry_tables.py`; incident and audit reports go in
  `docs/audits/` with a `YYYY-MM-DD-` prefix; UI changes follow
  `docs/ui-ux-design-rulebook.md`.

### 22. Commands

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
.\.venv\Scripts\python.exe -m pytest -c tests\pytest.ini tests -q
.\.venv\Scripts\python.exe -m src.telemetry.gridlock_monitor --watch
.\.venv\Scripts\python.exe docs\generate_geometry_tables.py

# A campaign (commit first: rows pair only on one git_sha).
# Phase one, the non-LLM arms in parallel headless workers; prints the campaign id:
.\.venv\Scripts\python.exe -m src.experiments.parallel_campaign --arms baseline rule-based passenger-pressure-tsp --seeds 234 764 101 --minutes 60
# Phase two, the LLM arms in the windowed app, joining that campaign:
$env:TRAFFIC_JOIN_CAMPAIGN = '<campaign id>'; .\.venv\Scripts\python.exe run.py
# Live progress of headless runs, campaigns and pytest sessions:
.\.venv\Scripts\python.exe run_monitor.py
# Regenerate a campaign's calibration report (written automatically at batch end):
.\.venv\Scripts\python.exe -m src.telemetry.calibration_report [CAMPAIGN_ID]
```

Ollama (local) or a `GEMINI_API_KEY` is needed only for LLM arms; the
baseline, rule and pressure arms run without either.

### 23. References used by the model

- Webster, F. V. (1958). *Traffic Signal Settings*. Road Research Technical
  Paper 39. — cycle and split formula (§9.1).
- Transportation Research Board. *Highway Capacity Manual*. — saturation-flow
  measurement by queue discharge with start-up lost time excluded (§9.1), jam
  spacing anchor (§2), LOS thresholds (§11).
- Treiber, M., Hennecke, A., Helbing, D. (2000). Congested traffic states in
  empirical observations and microscopic simulations. *Phys. Rev. E* 62,
  1805. — IDM (§5.2).
- Kesting, A., Treiber, M., Helbing, D. (2007). General lane-changing model
  MOBIL for car-following models. *Transp. Res. Rec.* 1999, 86–94. — MOBIL
  (§5.2).
- Treiber, M., Kesting, A. (2013). *Traffic Flow Dynamics*. Springer. — urban
  IDM parameter values (§5.2).
- Toledo, T., Zohar, D. (2007). Modeling duration of lane changes.
  *Transp. Res. Rec.* 1999, 71–78. — 3 s lane change (§5.2).
