# Lane-Changing in SUMO

## Two layers: per-vehicle decision model vs. per-lane orchestration

SUMO splits lane-changing into two distinct layers, confirmed by the
directory structure and includes:

1. **Decision models** (`src/microsim/lcmodels/*`) — per-vehicle
   strategy objects (`MSAbstractLaneChangeModel` subclasses) that
   answer "does this vehicle want to change lanes, in which direction,
   how far laterally, and is it currently blocked". One instance is
   owned per `MSVehicle` (`myVehicle.getLaneChangeModel()`).
2. **Orchestration** (`src/microsim/MSLaneChanger.*`,
   `MSLaneChangerSublane.*`) — per-*lane* control loop that iterates
   candidate vehicles edge-by-edge, gathers neighbor/leader/follower
   info, calls into the decision model, and — if the model says "go"
   and the gap-acceptance checks pass — actually performs (or begins)
   the maneuver (`startChange`/`continueChange`, or
   `startChangeSublane`/`continueChangeSublane` for the sublane case).

This mirrors the CF split: `MSCFModel` decides "how fast", the lane
changer decides "which lane", and both write into a shared
`finalizeSpeed()`/`patchSpeed()` pipeline (see
`docs/reverse_engineering/simulation/car_following.md`).

## `MSAbstractLaneChangeModel` — the common interface

Key virtual methods (confirmed in
`src/microsim/lcmodels/MSAbstractLaneChangeModel.h`):

- `wantsChange(laneOffset, msgPass, blocked, leader, follower, neighLead,
  neighFollow, neighLane, preb, lastBlocked, firstBlocked)` — discrete
  (whole-lane) decision API, used by LC2013/DK2008. Returns a bitmask
  of `LaneChangeAction` flags (see below).
- `wantsChangeSublane(laneOffset, alternatives, leaders, followers,
  blockers, neighLeaders, neighFollowers, neighBlockers, neighLane,
  preb, lastBlocked, firstBlocked, latDist, targetDistLat, blocked)` —
  continuous-lateral-position variant used by SL2015; in addition to the
  bitmask it outputs `latDist` (how far to move laterally this step).
- `patchSpeed(min, wanted, max, cfModel)` — apply LC-driven speed
  changes (must return a value in `[min, max]`); used e.g. to slow down
  and let a cooperating vehicle in, or to cap speed during an ongoing
  maneuver.
- `inform(info, sender)` — message passing between vehicles that are
  cooperating/blocking each other (`MSLCMessager` wraps leader /
  neigh-leader / neigh-follower `inform` calls).
- `checkChangeBeforeCommitting(veh, state)` — last-chance veto hook
  (used by `MSLCM_LC2013_CC` to defer to `MSCFModel_CC`'s platoon
  commit logic).
- `startLaneChangeManeuver` / `updateCompletion` / `endLaneChangeManeuver`
  — generic (non-model-specific) maneuver lifecycle machinery for
  continuous (non-instantaneous) lane changes, including "shadow lane"
  bookkeeping (the vehicle partially occupies both lanes while
  changing) and target-lane reservations so following vehicles on the
  destination lane react in advance.

Source: `src/microsim/lcmodels/MSAbstractLaneChangeModel.h:145-680`.

### `LaneChangeAction` flags

Defined in `src/utils/xml/SUMOXMLDefinitions.h:2180-2261`. The
"currently wanted action" bits are: `LCA_STAY`, `LCA_LEFT`,
`LCA_RIGHT`, and the reason bits `LCA_STRATEGIC`, `LCA_COOPERATIVE`,
`LCA_SPEEDGAIN`, `LCA_KEEPRIGHT`, `LCA_TRACI`, plus `LCA_URGENT`. A
separate group of "external state" bits records *why a change failed*:
`LCA_BLOCKED_BY_LEFT_LEADER/FOLLOWER`, `..._RIGHT_...`,
`LCA_OVERLAPPING`, `LCA_INSUFFICIENT_SPACE`, `LCA_INSUFFICIENT_SPEED`,
`LCA_SUBLANE`. `LCA_CHANGE_REASONS` aggregates all "why" bits including
`LCA_SUBLANE` and `LCA_TRACI`. This enum is confirmed, not inferred —
`wantsChange`/`wantsChangeSublane` return exactly these bits.

## `MSLCM_LC2013` — SUMO's default decision model

Confirmed (constants, code) motivations, matching the flag names above:

- **Strategic** (`LCA_STRATEGIC`) — changes needed to stay able to
  follow the route (e.g. reach the correct lane before a turn, avoid
  a stopped leader that blocks the route). Governed by `myStrategicParam`
  and a look-ahead distance `laDist` derived from
  `myLookAheadSpeed * LOOK_FORWARD * myStrategicParam`. Setting
  `myStrategicParam < 0` disables strategic changes entirely
  (`laDist = -1e3`).
- **Cooperative** (`LCA_COOPERATIVE`) — changes made to help another
  vehicle that is blocked (`amBlockingFollowerPlusNB()`), or to use
  inner roundabout lanes (`myRoundaboutBonus`/`getRoundaboutDistBonus`
  in `MSLCHelper.cpp`), gated by `myCooperativeParam`
  ("inconvenience" tolerance) and willingness checks
  (`MSLCHelper::unwillingToHelp`).
- **Speed gain** (`LCA_SPEEDGAIN`) — tactical desire to go faster.
  Tracked via a persistent, hysteresis-smoothed "probability" counter
  per direction (`mySpeedGainProbabilityLeft/Right`, fixed-point
  integers scaled by `HYST_PRECISION`) that accumulates
  `relativeGain = (neighLaneVSafe - thisLaneVSafe)/max(neighLaneVSafe, ...)`
  over time and decays (`*= pow(0.5 or 0.8, dt)`) when the current
  lane is not worse. A change is requested once the probability
  crosses `myChangeProbThresholdLeft/Right`; if it additionally
  crosses `mySpeedGainUrgency` the request is flagged `LCA_URGENT`.
- **Keep-right** (`LCA_KEEPRIGHT`) — "Rechtsfahrgebot" default-lane
  discipline: separately tracked `myKeepRightProbability`, decremented
  proportionally to how long the vehicle could continue at full speed
  on the right lane (`fullSpeedDrivingSeconds`) relative to
  `myKeepRightAcceptanceTime`/`KEEP_RIGHT_TIME`, and compared against
  `-myChangeProbThresholdRight`.

Both the speed-gain and keep-right decisions also feed
`myLCAccelerationAdvices`/`addLCSpeedAdvice`, which become the actual
value returned by `patchSpeed` for the *current* step (e.g. slowing
down to avoid overtaking on the right, or matching helped vehicle's
new advised speed).

Priority/short-circuit order inside `wantsChange` (source-confirmed by
reading top-to-bottom): (1) urgent strategic override (route-forcing
change even against other preferences, including "overtake a stopped
leader" and "don't strand on a highway on-ramp"), (2) roundabout
cooperative bonus, (3) cooperative help for a blocked follower,
(4) pedestrian-speed adaptation, (5) speed-gain/keep-right probability
accumulation and thresholding, (6) a final low-priority "drift toward
best lane if it doesn't hurt speed" strategic nudge. Each branch may
`return` immediately via `cancelRequest`/explicit `return ret | req`,
so later (weaker) motivations are skipped once a stronger one commits.

Source:
```
src/microsim/lcmodels/MSLCM_LC2013.cpp (MSLCM_LC2013::wantsChange, ~line 1230-1909)
src/microsim/lcmodels/MSLCHelper.cpp (getRoundaboutDistBonus, unwillingToHelp, updateBlockerLength)
```

## `MSLCM_SL2015` — sublane / continuous-lateral model

Where LC2013 only ever produces "move fully into lane N-1/N+1",
`MSLCM_SL2015` operates on **continuous lateral position** within a
lane (subdivided conceptually into "sublanes", controlled by
`MSGlobals::gLateralResolution`). Confirmed structural differences from
its header (`MSLCM_SL2015.h`):

- `wantsChangeSublane(...)` is the primary API; it takes
  `MSLeaderDistanceInfo` (multi-vehicle, per-sublane leader/follower
  sets: `leaders`, `followers`, `blockers`, `neighLeaders`,
  `neighFollowers`, `neighBlockers`) rather than single
  `pair<Vehicle*,double>` leader/follower — because several vehicles
  can be laterally beside the ego vehicle at once.
  `wantsChange(...)` still exists as a thin wrapper for API
  compatibility with the discrete lane-changer.
- It outputs `latDist` (and `maneuverDist`) — a real-valued lateral
  distance to travel this step — instead of a binary
  left/right/stay decision; a maneuver can be partially executed over
  several steps at a bounded lateral speed
  (`computeSpeedLat`, `myMaxSpeedLatFactor`, `myMaxSpeedLatStanding`),
  producing gradual, angled lane changes rather than an instantaneous
  jump.
- `getSafetyFactor()`/`getOppositeSafetyFactor()` are overridden,
  meaning SL2015 has its own (looser/stricter) notion of how much a
  vehicle may violate the nominal safe gap when "assertive".
- `updateExpectedSublaneSpeeds` and `decideDirection` are implemented
  (both are `throw ProcessError` stubs in the abstract base) — SL2015
  is the model that actually needs to evaluate speed prospects
  sublane-by-sublane and arbitrate when both directions look
  desirable simultaneously.

Both models share `MSLCHelper.cpp` utility functions (roundabout
bonus, blocker-length bookkeeping, bidi-lane/rail helpers, "unwilling
to help" checks, `getSpeedPreservingSecureGap`), so the actual gap-math
building blocks are common; SL2015 differs primarily in *granularity*
(sublane vs. lane) and *output type* (continuous distance vs. discrete
direction).

Source: `src/microsim/lcmodels/MSLCM_SL2015.h/.cpp` (4263 lines; only
the header and structural entry points were read closely per task
scope — deep algorithmic verification of every SL2015 branch was not
performed).

## Other decision models (skim-level)

| Model (file) | What it represents |
|---|---|
| `MSLCM_DK2008` | Daniel Krajzewicz's original (2004-2010) discrete lane-change model, predecessor to LC2013. Simpler: single `myChangeProbability` counter, no separate strategic/cooperative/speedgain/keepright split visible from the constructor (uses `LOOK_FORWARD_FAR/NEAR`, `JAM_FACTOR` constants directly). |
| `MSLCM_LC2013_CC` | Thin subclass of `MSLCM_LC2013` that overrides only `checkChangeBeforeCommitting` to defer to `MSCFModel_CC::commitToLaneChange`, gating lane changes for vehicles under the CACC/platooning "CC" car-following model. |

## `MSLaneChanger` — per-edge orchestration (discrete/LC2013 case)

`MSLaneChanger::laneChange(t)` runs once per lane-changing edge per
step. It builds one `ChangeElem` per lane (leader/follower bookkeeping,
`lastBlocked`/`firstBlocked` tracking used for cooperative-help
signaling) and repeatedly calls `change()` for the current back-most
unhandled vehicle on each lane (`findCandidate`). `change()`
(`src/microsim/MSLaneChanger.cpp:330-445`) does, in order:

1. If the vehicle is mid-maneuver, delegate to `continueChange`.
2. Skip vehicles that aren't allowed to change, already changed this
   step, or are stopped.
3. Skip (but let TraCI override) vehicles that aren't in an "active"
   (action) step.
4. If there's only one lane, or all normal changes are disallowed, try
   an opposite-direction (overtaking) change instead
   (`changeOpposite`).
5. Otherwise call `checkChangeWithinEdge(-1, ...)` (right) and
   `checkChangeWithinEdge(1, ...)` (left), each of which asks the
   vehicle's `wantsChange()` and checks resulting blockage
   (`LCA_BLOCKED`). If a direction is desired and unblocked,
   `startChange()` executes it immediately (or begins a continuous
   maneuver if `gLaneChangeDuration > 0`).
6. If **both** sides are urgently wanted, right is preferred
   (`stateLeft = 0`) — a hard-coded tie-break, source-confirmed at
   `MSLaneChanger.cpp:428-432`.
7. Otherwise register the vehicle unchanged for this step, still
   recording blocked/urgent state so blocking neighbors can be
   informed (cooperative help) on the next step.

`MSLaneChangerSublane` overrides `change()`/`checkChangeSublane()` etc.
to work in terms of `latDist`/`maneuverDist` and continuous shadow-lane
occupation instead of a single atomic hop; the class list
(`checkChangeToNewLane`, `continueChangeSublane`, `startChangeSublane`,
`abortLCManeuver`) shows sublane changes are explicitly resumable /
abortable across steps, unlike the (mostly atomic) LC2013 case.

Source:
```
src/microsim/MSLaneChanger.h/.cpp (ChangeElem, change, checkChangeWithinEdge, checkChange, startChange, continueChange)
src/microsim/MSLaneChangerSublane.h/.cpp (checkChangeSublane, startChangeSublane, continueChangeSublane)
```

## Interaction with `MSLane`

- `MSLane::planMovements()` builds the `MSLeaderInfo` used both by CF
  (`veh->planMove`) and, indirectly, by the lane changer (leaders are
  frozen for the step before lane-change decisions run).
- `MSLane::executeMovements()` commits actual lane hops after both CF
  and LC decisions for the step are finalized.
- Lane changing can only enter `MSCFModel::finalizeSpeed()`'s
  `patchSpeed()` step and cannot skip the CF safety envelope: the
  lane-change model is required to return a speed within
  `[min, max]` as passed in, so a lane change can slow a vehicle down
  (to open a gap) but not violate the CF-computed emergency floor.

## Tests

- No dedicated `unittest/src/microsim` files for lane-changing were
  found (only CF-model unit tests exist under that directory as of
  this reading).
- Functional/system tests: `tests/sumo/lc_model/continuous_lanechange`,
  `tests/sumo/action_step_length/LC_dynamics/continuous_lanechange`,
  `tests/sumo/sublane_model/*` (SL2015-specific scenarios such as
  `1edge_2lanes_compact_cars`, `1edge_2lanes_lane_discipline`, both with
  `.sumo`/`.sumo.ballistic` expected-output variants),
  `tests/sumo/opposite_direction_driving/{...,SL2015}` (opposite-side
  overtaking, including an SL2015-specific case),
  `tests/sumo/bugs/0xxx/lanechange*` (regression tests for specific
  historical bugs), and `tests/complex/state/continous_lanechange`.
  As with CF, these are end-to-end expected-output regression tests
  rather than unit tests of individual decision branches.
