# Car-Following in SUMO

## Overview

SUMO's microscopic car-following is factored behind a single abstract
interface, `MSCFModel` (`src/microsim/cfmodels/MSCFModel.h` /
`MSCFModel.cpp`). Each vehicle type owns one concrete `MSCFModel`
subclass instance (constructed once per `MSVehicleType`, shared by all
vehicles of that type; per-vehicle mutable state lives in a
`MSCFModel::VehicleVariables` object attached to the `MSVehicle`).
`MSVehicle::planMove()` calls into this interface every simulation step
(or every "action step" for vehicles with a longer reaction time) to
decide the vehicle's next speed; `MSCFModel::finalizeSpeed()` is the
single choke point where the model's raw proposal is clipped against
acceleration/deceleration bounds, stop constraints, and the lane-change
model's speed patch.

Confirmed by source: `src/microsim/cfmodels/MSCFModel.h` lines 92-271
(the `CalcReason` enum and the "Methods to override by model
implementation" section).

## The common interface

Every concrete model must implement:

- `followSpeed(veh, speed, gap2pred, predSpeed, predMaxDecel, pred, usage)`
  — desired speed while following a leader (may dawdle/undershoot the
  safe speed for realism).
- `stopSpeed(veh, speed, gap, decel, usage)` — desired speed while
  approaching a fixed obstacle (lane end, stop, red light) at distance
  `gap`.
- `insertionFollowSpeed` / `insertionStopSpeed` — safety-only variants
  (no dawdling) used once, at vehicle insertion, since a freshly
  inserted vehicle has no `speed` history to dawdle from.
- `freeSpeed(veh, speed, seen, maxSpeed, onInsertion, usage)` — speed
  when no obstacle is near, bounded by the upcoming speed limit
  reachable within `seen` meters.
- `getModelID()`, `duplicate()`, optional `createVehicleVariables()`.

`CalcReason` (`CURRENT`, `FUTURE`, `CURRENT_WAIT`, `LANE_CHANGE`) tells
the model what the returned speed will be used for; e.g. `FUTURE` calls
(used for lookahead/lane-change gap-acceptance calculations) suppress the
emergency-deceleration relaxation that live speed calls get.
Source: `src/microsim/cfmodels/MSCFModel.h:92-104`, `:807-856` (relaxEmergency logic).

`MSCFModel` itself supplies non-virtual/shared machinery that concrete
models reuse:

- `brakeGap(speed, decel, headwayTime)` — distance needed to stop,
  including a headway term (Euler-discrete or continuous form
  depending on the global update scheme).
- `maximumSafeFollowSpeed(gap, egoSpeed, predSpeed, predMaxDecel, onInsertion)`
  and `maximumSafeStopSpeed(gap, decel, currentSpeed, onInsertion, headway, relaxEmergency)`
  — the generic "safe speed to avoid a collision" formulas, with
  separate Euler (`maximumSafeStopSpeedEuler`) and ballistic
  (`maximumSafeStopSpeedBallistic`) implementations chosen via
  `MSGlobals::gSemiImplicitEulerUpdate`.
- `calculateEmergencyDeceleration` / `getSpeedAfterMaxDecel` — fallback
  when the "comfortable" `myDecel` cannot avoid a collision; the vehicle
  is allowed to brake up to `myEmergencyDecel` amplified by
  `EMERGENCY_DECEL_AMPLIFIER` (1.2).
- `getSecureGap(veh, pred, speed, leaderSpeed, leaderMaxDecel)` — the
  gap the lane-changing code treats as "safe enough to change into",
  derived from comparing follower and leader brake-gaps.

Source:
```
src/microsim/cfmodels/MSCFModel.cpp (MSCFModel::brakeGap, maximumSafeFollowSpeed,
                                      maximumSafeStopSpeedEuler, maximumSafeStopSpeedBallistic,
                                      getSecureGap, finalizeSpeed)
```

## finalizeSpeed(): the per-step pipeline

`MSCFModel::finalizeSpeed()` (called once per vehicle per action step
from `MSVehicle`) composes the final next-step speed from several
inputs, in this order (source: `MSCFModel.cpp:198-265`):

1. `vStop = min(vPos, veh->processNextStop(vPos))` — clip against any
   pending stop.
2. Compute `vMinEmergency` (allowed emergency braking floor) and
   `vMin` (normal deceleration floor, but never above `vPos`/`vMinEmergency`
   when an emergency is required).
3. Compute `vMax` from the vehicle's acceleration capability
   (`aMax`), a friction-derived speed-limit factor, and `vStop`.
4. `vNext = patchSpeedBeforeLC(veh, vMin, vMax)` — model-specific hook
   (Krauss injects "dawdling" here, see algorithm doc).
5. `vNext = veh->getLaneChangeModel().patchSpeed(vMin, vNext, vMax, *this)`
   — the active lane-change model may further reduce/increase the
   speed (e.g. to open a gap for a cooperating neighbor, or to
   decelerate while executing a maneuver).
6. `vNext = applyStartupDelay(veh, vMin, vNext)` — models a delayed
   throttle response after standing still (`myStartupDelay`).

This shows car-following and lane-changing are **not** independent:
the lane-change model gets a final say over the CF-proposed speed via
`patchSpeed`, and CF models call back into
`veh->getLaneChangeModel()` for things like `getSafetyFactor()`
(assertiveness) when computing secure gaps.

## Krauss (SUMO's default): `MSCFModel_Krauss` / `MSCFModel_KraussOrig1`

`MSCFModel_Krauss` (`SUMO_ATTR_CAR_FOLLOW_MODEL="Krauss"`, the default)
derives from `MSCFModel_KraussOrig1`, the direct 1998 Krauss model. The
base `vsafe()` formula and the "dawdling" (stochastic imperfection)
step live in `KraussOrig1`; `Krauss` overrides `patchSpeedBeforeLC` to
add optional temporally-correlated dawdling (`sigmaStep`) and overrides
`stopSpeed`/`followSpeed` to route through the shared
`maximumSafeFollowSpeed`/`maximumSafeStopSpeed` helpers instead of the
original closed-form `vsafe`. See the algorithm doc for the exact
equations.

Source:
```
src/microsim/cfmodels/MSCFModel_KraussOrig1.cpp (vsafe, followSpeed, stopSpeed, dawdle)
src/microsim/cfmodels/MSCFModel_Krauss.cpp (patchSpeedBeforeLC, followSpeed, stopSpeed, dawdle2)
```

## IDM: `MSCFModel_IDM`

Implements the Intelligent Driver Model (Treiber et al.). Unlike
Krauss, IDM is not a closed-form "safe speed" formula but an
acceleration law integrated over `myIterations` sub-steps per
simulation step (`_v()` in `MSCFModel_IDM.cpp`) — a smoother,
non-collision-truncated model. `MSCFModel_IDM` also implements the
"IDMM" variant (`idmm=true`) which adds a slowly-adapting "level of
service" headway-time multiplier (`myAdaptationFactor`,
`myAdaptationTime`) representing driver frustration/adaptation.

Source: `src/microsim/cfmodels/MSCFModel_IDM.cpp` (`_v`, `followSpeed`,
`freeSpeed`, `stopSpeed`, `insertionFollowSpeed`).

## Other car-following model variants (skim-level)

All of the following inherit `MSCFModel` and implement the same
`followSpeed`/`stopSpeed`/`freeSpeed` triad; they differ in the
internal law used to pick a speed/acceleration. Table built by reading
each file's header comment and constructor (not full algorithm
verification):

| Model (file) | What it represents |
|---|---|
| `MSCFModel_KraussOrig1` | The original 1998 Krauss safe-speed model (`vsafe` closed form). Base for `Krauss`. |
| `MSCFModel_Krauss` | Default in SUMO: `KraussOrig1` + acceleration-decrease/faster-start "dawdling" refinements and optional `sigmaStep` temporal correlation. |
| `MSCFModel_KraussPS` | Krauss with acceleration/speed reduced by road slope ("PS" = power/slope). |
| `MSCFModel_KraussX` | Krauss variant adding an "overbraking" experiment (threshold-based extra deceleration), tunable via generic `tmp1`/`tmp2` params. |
| `MSCFModel_IDM` | Intelligent Driver Model (and IDMM variant), continuous acceleration law, iteratively integrated. |
| `MSCFModel_EIDM` | Extended/enhanced IDM (adds features such as driving-imperfection / estimation error, more parameters — not deep-read). |
| `MSCFModel_ACC` | Adaptive Cruise Control model (Milanes/Xiao et al., constant-time-gap controller), for automated vehicles; cites transportation-research papers in the file header. |
| `MSCFModel_CACC` | Cooperative ACC — like ACC but uses V2V-communicated leader data (spacing policy with communicated acceleration) for tighter platooning. |
| `MSCFModel_CC` | Michele Segata's "Cruise Control" library-integration model used together with `MSLCM_LC2013_CC` for platoon-oriented lane-change gating. |
| `MSCFModel_PWag2009` | Peter Wagner's scalable model based on Krauss, adds an "action point probability" (drivers don't react every step) and separate last-braking tau. |
| `MSCFModel_Kerner` | Kerner's three-phase-traffic model (synchronized flow / free / jam phases), from a 2007 arXiv testbed paper. |
| `MSCFModel_SmartSK` | "A smarter SK" — an enhanced Krauss/SK variant with extra tunable `tmp*` parameters. |
| `MSCFModel_NaSch` | Nagel-Schreckenberg cellular-automaton model; `myCellSpeed` doubles as the CA cell length, discretizing to `accel`-sized cells. |
| `MSCFModel_Rail` | Physically-based train model using resistance coefficients (`TrainParams::getResistance`) instead of a psychological car-following law — for rail vehicles. |
| `MSCFModel_Wiedemann` | Psycho-physical Wiedemann model (perception thresholds, not a continuous safe-speed formula); references several comparison papers in-file. |
| `MSCFModel_W99` | Wiedemann-99 parameterization, code adapted from a third-party (MIT-licensed) reference implementation. |
| `MSCFModel_Daniel1` | An experimental/teaching model parameterized by generic `tmp1..tmp5` attributes; Krauss-like structure (`myTauDecel`). |

None of these override the shared `finalizeSpeed()`/`patchSpeed()`
pipeline except where noted (IDM overrides `finalizeSpeed` to update
its adaptation state); they only change what `followSpeed`/`stopSpeed`
compute.

## Interaction with lanes and lane-changing

- `MSLane::planMovements()` iterates each lane's vehicles back-to-front,
  builds a `MSLeaderInfo` of everything ahead (including partially-lapped
  vehicles from ongoing lane changes and maneuver reservations from the
  sublane model), and calls `veh->planMove(t, leaders, cumulatedVehLength)`,
  which is where `followSpeed`/`stopSpeed`/`freeSpeed` get invoked with
  concrete leader/gap arguments. Source: `src/microsim/MSLane.cpp:1594-1631`.
- `MSLane::executeMovements()` later commits the planned motion
  (`veh->executeMove()`), moving vehicles between lanes as needed.
- The car-following model exposes `getSecureGap`, `getMaxDecel`,
  `getEmergencyDecel`, `getApparentDecel`, `maximumSafeFollowSpeed`
  etc. as building blocks that the lane-change code (`MSLaneChanger`,
  `MSLCM_LC2013`, `MSLCHelper`) reuses directly to decide whether a
  gap on a neighboring lane is safe to enter, and to compute
  "cooperative help" speeds for blocking followers.

## Tests

- Unit tests: `unittest/src/microsim/MSCFModelTest.cpp` (tests
  `brakeGap`/static `freeSpeed` helpers) and
  `unittest/src/microsim/MSCFModel_IDMTest.cpp`. Coverage is narrow —
  a handful of `TEST_F` cases checking specific closed-form values, not
  exhaustive per-model coverage.
- Functional/system tests: `tests/sumo/cf_model/{ACC,CACC,EIDM,IDM,Krauss,
  KraussPS,W99,Wiedemann,...}` — SUMO's standard "run the simulator,
  diff textual output" regression tests, organized per model and per
  scenario (`follow_slow`, `jam_resolution`, `tau`, `stop_at_position`,
  `drive_in_circles`, `slope`, `rail`, etc.). These are behavioral
  regression tests (expected output XML), not unit-level assertions on
  formulas.
