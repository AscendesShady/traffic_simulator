# SUMO conformance audit

## Why this exists

`reverse_engineering/` (untracked, added 2026-09-21) is a from-source
reverse-engineering knowledge base of Eclipse SUMO — the field's reference
open-source microscopic traffic simulator. It is not our documentation; it is
an external checklist. This audit compares this simulator's actual behavior
against the invariants and MUST-PRESERVE requirements that corpus states for
SUMO, so that when someone asks "how does this actually perform" or "is this a
real traffic model," the answer is backed by a specific comparison rather than
an assertion.

**Scope boundary, stated up front:** this simulator was never built to be
SUMO-compatible. It models two connected signalized intersections, six fixed
bus routes with TSP/DBL, and Webster-derived timing — a research tool, not a
general-purpose multimodal microsimulator. Per the corpus's own
`14_REIMPLEMENTATION_SPEC.md` ("Undeclared SUMO parity... is out of scope.
Passing a simple route is not evidence of full compatibility"), the correct
move is not to chase full parity — it's to state which parts are comparable,
verify those, and explicitly disclaim the rest. That is what this document
does.

**Out of scope for comparison** (present in SUMO, absent here by design, not
by omission): pedestrians/persons, rail and electric-vehicle systems,
mesoscopic simulation mode, TraCI/libsumo remote-control protocol, multi-format
network import (OSM/OpenDRIVE/etc.), and GUI/editor (Netedit) parity. None of
these are claimed anywhere in this repo's docs, so there is nothing to correct.

## Method

Read the SUMO corpus's algorithm docs (`car_following.md`, `lane_changing.md`,
`junction_behaviour.md`, `vehicle_insertion.md`, `vehicle_removal.md`,
`collision_handling.md`, `traffic_light_control.md`, `demand_generation.md`),
`09_INVARIANTS.md`, and `14_REIMPLEMENTATION_SPEC.md`, then checked each
applicable requirement against this repo's actual source
(`src/core/main.py`, `signal_controller.py`, `vehicle.py`) rather than against
CLAUDE.md's description of it — CLAUDE.md is accurate everywhere checked, but
the requirement here is behavioral, not documentary.

## Findings

| Area | SUMO requirement | This simulator | Verdict |
|---|---|---|---|
| Step/clock | One completed step advances time exactly once, fixed step length | `dt_step = 1/60` fixed, `step_simulation` is the single frame authority for both the Tk loop and headless runs (`tests/test_step_equivalence.py` enforces this) | **Match** |
| TLS state for planning | Signal state used for vehicle planning must be the state selected *before* that step's movement phase | `step_simulation` (main.py:3592-3613) runs `decide()` then `signals.update()` then takes one `active_signal_data` snapshot, passed unchanged to every vehicle's `update()` in the loop that follows | **Match** |
| Plan-before-commit motion | "Planning precedes execution; vehicle positions are not incrementally changed while gathering same-phase lane plans" (SUMO: `planMove()` for all, then `executeMove()` for all) | `vehicles[:]` is iterated once and each `v.update()` both plans and commits that vehicle's new position immediately, querying `all_vehicles` — a live list that mixes vehicles already moved this frame with ones not yet moved. No shuffle/sort precedes the loop, so it's spawn-order-dependent | **Deliberate divergence — worth naming.** Not a bug (nothing in this codebase claims otherwise), but it means a vehicle earlier in `vehicles` sees its leader's *already-updated* position while a later vehicle sees the same leader's *stale* position, within one frame. At 60 Hz with px-scale following gaps this is a small, probably invisible bias, but it is real and unmeasured. See recommendation below. |
| Ordered lane occupancy for leader queries | Lane vehicle containers stay ordered by longitudinal position; leader/follower lookup uses that order | No per-lane ordered container — every leader/follower/gap query (`vehicle.py:257,281,323,353,443,476,510,638,741,842,1269`) is a linear scan over the full `all_vehicles` list | **Deliberate divergence, correctness-neutral.** O(n) per query, O(n²) per frame; fine at this network's scale (dozens of vehicles), not fine if the network ever grows by an order of magnitude. Flag as a scalability ceiling, not a bug. |
| Junction/link conflict arbitration | Right-of-way is a preventive layer (link `opened()`/`blockedAtTime()`); a *separate* recovery layer (`detectCollisions`, teleport/remove) exists for whatever preventive logic misses | `signal_controller.py` reservations are the sole preventive authority (matches SUMO's preventive layer in spirit). There is **no recovery layer at all** — no collision detection, no teleport, no remove-on-overlap. `tests/test_adversarial_simulation.py` instead asserts non-overlap as a hard invariant that must never be violated | **Deliberate divergence — the most important one to be explicit about.** SUMO treats collision as a possible outcome of an imperfect preventive model and recovers from it. This simulator treats collision as *structurally impossible* and has no fallback if that assumption is ever wrong. That is a stronger claim, not a weaker one — but it means a bug in the reservation logic has no safety net, whereas SUMO would at least log/teleport and continue. Worth saying explicitly rather than leaving it implicit. |
| Vehicle insertion / retry / discard | Blocked departures retry (`PENDING -> PENDING`), configured terminal failure discards with counters, one commit per vehicle | Per-source `state["arrivals"]` deque (capped `MAX_PENDING_ARRIVALS = 5000`, overflow counted) holds offered-but-unentered vehicles; bus trips that can't depart accumulate in `bus_pending_trips` and are counted once each via `bus_trips_missed` | **Match in spirit.** Different data structure, same lifecycle contract: retry until admitted, count if not. |
| Vehicle removal / exactly-once counters | Final output/counter change happens exactly once per vehicle, on a defined completion boundary | `passed_nodes` non-empty gates throughput credit, then `vehicles.remove(v)` — one credit, one removal, no double-count path found | **Match** |
| Car-following model | MUST name an explicit model; Krauss-family only if claiming SUMO-default compatibility | Two named, declared engines (`legacy` gap/speed rule, `idm` per-class IDM), selected by `global_config["movement_model"]`, folded into `config_hash`. Neither is Krauss/LC2013 and nothing claims otherwise | **Match — no compatibility claimed, none needed.** This is exactly the corpus's own "name your model, don't call one generic formula SUMO compatibility" rule, already followed. |
| Lane-changing | Named motives (route/strategic, cooperative, speed-gain, keep-side) with leader/follower safety gating | MOBIL-lite (legacy) / full MOBIL (IDM) discretionary changes are speed-gain + safety gated; route-necessity is handled separately and unconditionally (left-turners forced into lane 2, not a MOBIL decision); DBL-vacate is a purpose-built cooperative mechanism, not a generalized cooperative motive | **Match in substance, narrower surface.** The motive categories map cleanly (strategic → deterministic lane-2 routing, cooperative → DBL vacate, speed-gain/keep-outer → MOBIL), it's just not phrased as one unified weighted-motive model the way LC2013 is. Fine for this network's fixed 3-lane geometry. |
| Traffic-light control | TLS ID/program/phase model; green is one constraint among several, never a bypass | Per-node fixed cycle (`EW_GREEN -> EW_YELLOW -> ALL_RED -> NS_GREEN -> ...`) plus a TSP/DBL request state machine layered on top, never granting green without clearance; `SignalController` is documented as the sole authority | **Match.** Actuated/SOTL/NEMA/WAUT are explicitly OPTIONAL in the SUMO spec too — nothing lost by not having them. |
| Demand generation / RNG determinism | Deterministic RNG stream ownership, auditable generation vs. insertion separation | Each source has its own seeded `random.Random`, every arrival's full attribute set is drawn at *offer* time and queued, `demand_draw_hash` fingerprints the whole offered schedule, and it's asserted immutable across controller arms | **Match, arguably exceeds.** SUMO's own corpus doesn't describe an equivalent paired-arm common-random-numbers harness as standard tooling — this is closer to a designed experiment than SUMO's demand tools typically are used for. |

## Recommended documentation updates

1. **State the scope boundary explicitly, once, near the top of an
   architecture doc** (now done in §1 of `docs/TRAFFIC_SIMULATOR_METHODOLOGY_AND_ARCHITECTURE.md`):
   this simulator does not claim SUMO model parity (car-following,
   lane-changing, and network-import formats are intentionally custom/HCM-
   calibrated, not Krauss/LC2013/SUMO-XML), so that question is answered before
   it's asked.
2. **Name the sequential-update ordering as an accepted simplification**, one
   sentence in the Movement model section of CLAUDE.md, next to the existing
   ±0.05 px/frame and MOBIL-lite notes — e.g. "vehicles plan and commit within
   the same pass over `all_vehicles`, in spawn order, not SUMO's two-phase
   plan-then-commit; this is a small, unmeasured order bias, not corrected
   because no test has ever shown it to matter at this network's scale."
3. **Say explicitly that collision handling is 100% preventive with no
   recovery layer**, and point at `tests/test_adversarial_simulation.py` as the
   evidence that this is continuously checked, not assumed. This is the one
   place where being asked "what happens if two vehicles collide" deserves a
   precise answer: "it's prevented by construction; if that construction were
   ever wrong there is no teleport/remove fallback, only a failing test."

None of these require behavior changes — the audit found no correctness bug,
only two structural simplifications (sequential frame update, no collision
recovery layer) that are reasonable for this network's scale but were
previously implicit rather than stated.

## What this audit deliberately did not do

Chase parity with pedestrians, rail/electric devices, mesoscopic mode,
TraCI/libsumo, multi-format network import, or GUI/editor internals. Per the
corpus's own reimplementation spec, claiming compatibility with an
unimplemented profile is worse than not claiming it — P0-P2 (core/urban-micro
coverage) is the applicable comparison band here, and that's what was checked.

## Confidence

High for the comparisons made (each backed by a specific file/line on both
sides). This is a one-time structural comparison against a documentation
corpus, not a differential/trajectory-level conformance test — no claim is
made that outputs would match SUMO numerically, only that the same categories
of invariant are (or are knowingly not) upheld.
