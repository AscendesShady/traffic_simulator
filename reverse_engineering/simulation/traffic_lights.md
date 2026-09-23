# Traffic Lights Subsystem

Rail signals share the traffic-light/link infrastructure but use
route-dependent driveways, constraints, and a separate step update. See
`rail_and_electric_systems.md` for that specialized lifecycle.

## Purpose

Traffic-light-controlled junctions in SUMO's microscopic simulation (`src/microsim`) do not implement
right-of-way conflict resolution themselves — instead a `MSTrafficLightLogic` periodically writes a
per-link character state (`r`, `g`, `G`, `y`, ...) into every `MSLink` it controls, and `MSLink::opened()`
(see `docs/reverse_engineering/network/junctions.md`) consults that state exactly like it would consult a
priority junction's response bitset. The subsystem's job is: hold one or more named "programs" per
traffic-light id, decide when/how to advance from one phase to the next (fixed-time, gap-actuated,
delay-based, SOTL, swarm, rail-signal, ...), and push the resulting link states out every time a phase
changes.

Source:
```
src/microsim/traffic_lights/MSTrafficLightLogic.h/.cpp
src/microsim/traffic_lights/MSTLLogicControl.h/.cpp
```

## Responsibilities

- Own the ordered list of phases (`MSPhaseDefinition`) that make up a program and the current phase index.
- Decide, once per phase, how long to remain in it (`trySwitch()` returns the delay until the next call)
  — for fixed-time logic this is just the phase's configured duration; for actuated/delay-based/SOTL
  logic it depends on detector occupancy, gaps, queueing, or custom conditions.
- Translate the active phase's state string into `LinkState` values and push them to every controlled
  `MSLink` (`setTrafficLightSignals`), which is what actually gates `MSLink::opened()`.
- Track and switch between multiple named "programs" for the same tls id, including scheduled program
  switches ("WAUT"s), off-mode, and TraCI-driven overrides.
- For rail infrastructure, additionally arbitrate actual route/driveway conflicts (`MSRailSignal`,
  `MSDriveWay`), which is a materially different job from road intersection signal timing.

Source:
```
src/microsim/traffic_lights/MSTrafficLightLogic.cpp (setTrafficLightSignals, addLink)
src/microsim/traffic_lights/MSTLLogicControl.cpp (TLSLogicVariants)
```

## Inputs

- The parsed `.net.xml` / `.tll.xml` traffic-light-logic definitions: phase state strings, durations,
  min/max durations, offsets, per-tls parameters (`key="..." value="..."` used heavily by actuated/SOTL
  logic to configure thresholds).
- Live detector data for actuated families: `MSInductLoop` / `MSE2Collector` occupancy, gap, and
  time-loss measurements built by `NLDetectorBuilder` and referenced via `myDetectorPrefix`-named ids.
- `MSLink::setApproaching` registrations (arrival time/speed of vehicles heading for each controlled
  link) — used indirectly by SOTL/actuated policies and directly by rail signals.
- Simulation time (`MSNet::getCurrentTimeStep`) and the scheduling event queue
  (`MSEventControl`/`SwitchCommand`).

## Outputs

- `LinkState` values written into every `MSLink` this logic controls (`MSLink::setTLState`).
- The next absolute switch time, which reschedules the `SwitchCommand` in
  `MSNet::myBeginOfTimestepEvents`.
- Optional detector/queue-state introspection used by GUI and TraCI
  (`getDetectorStates`, `getTLQueueLength`, `getConditions`).
- Saved/restored program state for `--save-state`/`--load-state` (`saveState`/`loadState`).

## State

- `MSTrafficLightLogic::myLinks` / `myLanes` — per-signal-index vectors of controlled `MSLink*` / arrival
  `MSLane*`, filled by `addLink` while the network is parsed.
- `myOverridingTimes`, `myCurrentDurationIncrement` — one-shot duration overrides (TraCI
  `setPhaseDuration`, `setLinkState`, etc.).
- Per-phase mutable fields on `MSPhaseDefinition` (`myLastSwitch`, `myLastEnd`, `duration`) — the phase
  objects are shared, mutable state, not immutable configuration.
- `MSTLLogicControl::TLSLogicVariants` — one per tls id, holding all program variants, which one is
  currently active, and pending WAUT switch procedures.
- Actuated-family-only state: per-lane detector objects, `myConditions`/`myAssignments` maps for custom
  switching rules, `myLinkGreenTimes`/`myLinkRedTimes` (for `min-green`/coordination constraints).

Source:
```
src/microsim/traffic_lights/MSTrafficLightLogic.h (myLinks, myLanes, mySwitchCommand)
src/microsim/traffic_lights/MSActuatedTrafficLightLogic.h (detector/condition state)
```

## Dependencies

- `MSLink` — the only channel through which a tls affects vehicle behaviour; a tls never touches vehicles
  directly.
- `MSNet` / `MSEventControl` — scheduling of `SwitchCommand` executions.
- `NLDetectorBuilder`, `MSInductLoop`, `MSE2Collector` — detector construction/read-out for actuated,
  delay-based and SOTL logics.
- `MSJunction` only indirectly: a `MSTrafficLightLogic` is attached at the `MSLink` level, not the
  junction level, so one tls program can span links belonging to several `MSJunction` objects (common for
  joined/clustered intersections).
- Rail-specific logics additionally depend on `MSDriveWay`, `MSRailSignalConstraint`,
  `MSRailSignalControl` for train route conflict resolution.

## Consumers

- `MSLink::opened()`/`blockedAtTime()` read `myState`/`myLogic` to decide whether the connection may be
  crossed (see `docs/reverse_engineering/network/junctions.md`).
- TraCI's `trafficlight` domain (`libsumo::TrafficLight`) reads/writes phase index, phase duration,
  program, and complete state strings through `MSTrafficLightLogic`/`MSTLLogicControl`.
- GUI (`GUITrafficLightLogicWrapper` et al., not covered here) visualizes current state per link and lets
  users force phase switches.

## Execution

Every simulation step, `MSNet::simulationStep()` calls `myLogics->check2Switch(myStep)`
(`src/microsim/MSNet.cpp`, around the collision-check block for `STAGE_EVENTS`), which walks all TLS and
invokes any due `SwitchCommand`. Each `SwitchCommand::execute()` calls the logic's own
`trySwitch()`, which:
1. Possibly returns early with a pure delay (duration increment, custom hold) without changing phase.
2. Otherwise decides the next phase index; sets `MSPhaseDefinition::myLastSwitch`; calls
   `setTrafficLightSignals` to push the new per-link states.
3. Returns the number of simulation steps to wait before `trySwitch()` is called again — this can be as
   small as one step for actuated logic re-checking a gap, or the full phase duration for static logic.

This happens *before* `myEdges->planMovements`/`setJunctionApproaches`/`executeMovements` in the same
step, so vehicles always plan their move against the traffic light state that is current for that step.

Source:
```
src/microsim/MSNet.cpp (simulationStep — myLogics->check2Switch(myStep))
src/microsim/traffic_lights/MSTrafficLightLogic.cpp (SwitchCommand::execute)
```

## Important Classes

| Class | Role |
|---|---|
| `MSTrafficLightLogic` | Abstract base: link/lane bookkeeping, phase-agnostic scheduling (`SwitchCommand`), TraCI-facing accessors. |
| `MSSimpleTrafficLightLogic` | Concrete fixed-time ("static") logic; also the base class most actuated/delay/SOTL/rail-crossing logics derive from for phase-list bookkeeping. |
| `MSActuatedTrafficLightLogic` | Gap-based actuated logic driven by induction-loop/E2 detectors, custom conditions, min/max green, coordination (`earliestEnd`/`latestEnd`). |
| `MSDelayBasedTrafficLightLogic` | Extends green while approaching vehicles have accumulated timeloss above a threshold, else advances after `minDuration`. |
| `MSTLLogicControl` | Registry of all TLS ids and their program variants (`TLSLogicVariants`); handles program switching, WAUTs, save/load state. |
| `MSPhaseDefinition` | One phase: state string, duration/min/max, `earliestEnd`/`latestEnd`, `nextPhases`, SOTL transient/commit/target flags. |
| `MSRailSignal` | Not a phase-cycling logic at all — computes green/red per request by checking driveway conflicts (`MSDriveWay`) for trains. |

## Important Functions

- `MSTrafficLightLogic::setTrafficLightSignals(SUMOTime t)` — applies the current phase's state string to
  every controlled `MSLink::setTLState`.
- `MSSimpleTrafficLightLogic::trySwitch()` — advances `myStep` to `nextPhases.front()` or `myStep+1 (mod N)`
  and returns the new phase's `duration` (fixed-time case).
  Source: `src/microsim/traffic_lights/MSSimpleTrafficLightLogic.cpp`.
- `MSActuatedTrafficLightLogic::gapControl()` / `::duration()` — the actuated extension algorithm; see
  `docs/reverse_engineering/algorithms/traffic_light_control.md`.
- `MSDelayBasedTrafficLightLogic::proposeProlongation()` — sums estimated time-to-junction for vehicles
  whose accumulated timeloss exceeds `minTimeloss`, on green lanes only.
- `MSPhaseDefinition::isGreenPhase()` / `isAllRedPhase()` — pure string inspection (`gG` present and no
  `yY`; all `r`) used by actuated/SOTL logic to decide which phases are stretchable.

## Behaviour

- A phase's state string has exactly one character per controlled-link index (`myNumLinks`); index
  assignment is fixed once at `addLink` time and shared across all programs of the same tls id.
- `MSPhaseDefinition::isActuated()` reports true whenever `minDuration != maxDuration` (or
  `minDuration == OVERRIDE_DURATION`), i.e. any phase whose length is not hard-fixed is "actuated" in this
  loose sense, independent of `TrafficLightType`.
- `MSTrafficLightLogic::getLogicType()` records the configured family (`STATIC`, `ACTUATED`, `NEMA`,
  `DELAYBASED`, `SOTL_*`, `SWARM_BASED`, `HILVL_DETERMINISTIC`, `RAIL_SIGNAL`, `RAIL_CROSSING`, `OFF`); the
  base class dispatch is virtual (`trySwitch`), not a switch on this enum.
- Switching off a tls program (`OFF` type / `MSOffTrafficLightLogic`) sets each link's `myOffState`,
  usually derived from the network's default priority so the junction behaves like an uncontrolled
  right-of-way junction (with all-way-stop as an additional off-mode option, see
  `docs/reverse_engineering/network/junctions.md`).

### TLS Strategy Catalog

| Class | Strategy family | Notes |
|---|---|---|
| `MSSimpleTrafficLightLogic` | Fixed-time (static) | Cycles through phases with fixed durations; base for most subclasses below. |
| `MSActuatedTrafficLightLogic` | Actuated (gap-based) | Induction-loop gap acceptance, min/max green, optional custom `condition`/`assignment` expressions, coordination via `earliestEnd`/`latestEnd`. |
| `MSDelayBasedTrafficLightLogic` | Actuated (delay/timeloss-based) | Extends green while vehicles on approach accumulate timeloss above threshold; E2-detector based. |
| `NEMAController` / `TrafficLightType::NEMA` | Actuated (US NEMA ring-and-barrier) | Ring/barrier phase structure compliant with NEMA controller conventions; vehext/yellow/red timings on `MSPhaseDefinition`. |
| `MSSOTLPhaseTrafficLightLogic`, `MSSOTLPlatoonPolicy`, `MSSOTLPhasePolicy`, `MSSOTLMarchingPolicy`, `MSSOTLRequestPolicy`, `MSSOTLCongestionPolicy`, `MSSOTLWaveTrafficLightLogic` | SOTL (Self-Organizing Traffic Lights) | Bio-inspired policies (request/phase/platoon/marching/wave/congestion) choosing the next green target from local sensor stimuli; `MSSOTLPolicy` and `PushButtonLogic`/`SigmoidLogic` mixins supply shared math. |
| `MSSwarmTrafficLightLogic` (extends `MSSOTLHiLevelTrafficLightLogic`) | Swarm intelligence | Pheromone-like learning across SOTL policies to pick a policy per junction adaptively. |
| `MSDeterministicHiLevelTrafficLightLogic` (extends `MSSOTLHiLevelTrafficLightLogic`) | Deterministic high-level SOTL dispatch | Chooses among SOTL sub-policies deterministically rather than via swarm learning. |
| `MSRailSignal` | Rail-specific (not a phase cycle) | Grants/denies passage per train request based on `MSDriveWay` conflict + `MSRailSignalConstraint`s, not a fixed phase list. |
| `MSRailCrossing` (extends `MSSimpleTrafficLightLogic`) | Rail-specific fixed logic | Level-crossing barrier logic keyed off approaching-train detection rather than cyclic phases. |
| `MSOffTrafficLightLogic` | Off / disabled | No phases; links fall back to their `myOffState` (typically blinking/priority). |

Source:
```
src/microsim/traffic_lights/MSSOTLPolicy.h, MSSOTLTrafficLightLogic.h, MSSOTLHiLevelTrafficLightLogic.h
src/microsim/traffic_lights/MSSwarmTrafficLightLogic.h
src/microsim/traffic_lights/MSRailSignal.h, MSRailCrossing.h, MSDriveWay.h
src/microsim/traffic_lights/NEMAController.h
```

### Phase State Character Codes

(from `enum LinkState` in `src/utils/xml/SUMOXMLDefinitions.h`, values are the literal chars used in phase
state strings)

| Char | Meaning |
|---|---|
| `G` | Green, major (may pass without braking for priority) |
| `g` | Green, minor (must yield/brake for foes even though green) |
| `y` | Yellow, minor (must brake anyway) |
| `Y` | Yellow, major (may still pass) |
| `r` | Red (must brake/stop) |
| `u` | Red-yellow (red but indicates upcoming green) |
| `o` | TLS off, blinking (must brake, like an uncontrolled minor link) |
| `O` | TLS off, steady/no-signal (may pass) |
| `s`/`w`/`Z`/`M`/`m`/`=`/`-` | Non-TLS uncontrolled states (stop sign, all-way stop, zipper, major/minor priority, right-before-left, dead end) reused by the same `LinkState` enum for uncontrolled junctions — see `docs/reverse_engineering/network/junctions.md`. |

## Edge Cases

- **Multiple programs per tls id**: `MSTLLogicControl::TLSLogicVariants` holds several programs (e.g. a
  time-of-day plan, an actuated variant, a TraCI-injected `"online"` program); only one is active
  (`isActive`) and `getDefault()` returns the last-used non-TraCI program for restoring after a TraCI
  session ends.
- **Actuated logic with `multiTarget` phases**: when a phase has more than one possible `nextPhases`
  entry, `gapControl()`'s single-gap answer is not used directly — `decideNextPhase()` picks by summed
  detector "priority" across candidate target phases instead (`MSActuatedTrafficLightLogic::trySwitch`).
- **`myLinkMinGreenTimes`/`myLinkGreenTimes`**: per-link minimum green constraints can force `trySwitch()`
  to defer a chosen phase change by returning a short retry delay instead of committing immediately.
- **Coordinated/offset programs**: `getEarliest`/`getLatest` (in `MSSimpleTrafficLightLogic`) can push a
  phase's end into the *next* cycle to satisfy `earliestEnd`/`latestEnd` constraints, meaning the same
  phase index can run for longer than one nominal cycle.
- **Meso simulation**: `MSGlobals::gUseMesoSim` short-circuits several actuated-logic considerations (e.g.
  `MSLink::opened()` treats a fully impatient meso vehicle as passing regardless of tls state unless on a
  roundabout) and TLS effects are instead applied as `myMesoTLSPenalty`/`myGreenFraction` time penalties on
  `MSLink` rather than hard link-state gating.
- **Off-mode retains "cont" status**: `MSLink::checkContOff()` decides whether a link that was allowed to
  advance into the junction while a downstream signal was red keeps that "cont" permission once the tls is
  switched off.

## Tests

- `tests/sumo/tls/` — static/actuated/NEMA/delay-based/off/offset/WAUT scenario tests (qualitative
  coverage, not exhaustively enumerated here); subfolders include `actuated`, `delay_based`, `NEMA`,
  `off`, `off_allwayStop`, `wauts`.
- No dedicated unit tests for TLS logic were found under `unittest/src/microsim` (that directory currently
  only covers car-following models and event control) — TLS behaviour is exercised primarily via the
  scenario-level `tests/sumo` suite driven through the full `sumo` binary.

Confidence: Medium — unit test coverage claim is based on directory listing at time of writing, not an
exhaustive search of the whole `unittest/` tree.

## Modification Points

- Adding a new TLS strategy: subclass `MSTrafficLightLogic` (or `MSSimpleTrafficLightLogic` for anything
  phase-list-based) and implement `trySwitch()`; register it in the network-loading factory (not covered by
  this scope — see `netload`) and add a new `TrafficLightType` enum value in `SUMOXMLDefinitions.h`.
- Changing actuated gap-acceptance behaviour: `MSActuatedTrafficLightLogic::gapControl()`/`duration()`.
- Changing which phase char codes exist or their pass/brake semantics: `enum LinkState` plus every
  `MSLink::have*()`/`opened()` branch that switches on it (tight coupling — see junctions doc).
- Adding new detector-driven inputs: extend `NLDetectorBuilder`-built detector wiring in the relevant
  logic's `init()`.

## Confidence

High for `MSTrafficLightLogic`, `MSSimpleTrafficLightLogic`, `MSActuatedTrafficLightLogic`,
`MSDelayBasedTrafficLightLogic`, `MSTLLogicControl`, and the `LinkState` character table — all read
directly from source. Medium for the SOTL/Swarm/NEMA/rail catalog table — these were skimmed (class
declarations, headers, one or two key methods) rather than deep-read, per task scope; exact tuning
formulas inside SOTL policy classes (`MSSOTLPolicy3DStimulus`, `MSSOTLPolicy5DStimulus`, etc.) are not
verified here.
