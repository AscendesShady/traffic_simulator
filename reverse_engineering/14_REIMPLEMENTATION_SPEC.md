# Reimplementation specification

## 1. Objective

Define a simulator that can reproduce a clearly declared subset of SUMO's
observable traffic semantics without copying SUMO's internal class hierarchy.
Compatibility is feature-profile based: an implementation must name the models,
formats, APIs and options it supports and pass their behavioral fixtures.

This specification distinguishes:

- **MUST PRESERVE** — required for the supported compatibility profile.
- **MAY CHANGE** — internal design can differ without changing results.
- **OPTIONAL** — valid later profiles, not required for a minimal engine.
- **OUT OF SCOPE** — excluded until explicitly selected and specified.

## 2. Functional Requirements

### MUST PRESERVE

- Deterministic load–initialize–step–output–shutdown lifecycle for a fixed seed,
  inputs, versioned behavior profile and platform-precision policy.
- Explicit separation of static network, demand definitions and live state.
- Stable object IDs and reference validation.
- Declared error behavior for malformed/missing/incompatible inputs.
- A discoverable mapping from every implemented behavior to source-inspired
  requirement and regression test.

### MAY CHANGE

Language, class names, allocation strategy, ECS versus OO organization,
parallel scheduler, database/container choices and module boundaries.

### OPTIONAL

GUI, editor, external network importers, mesoscopic simulation and specialized
devices/controllers.

### OUT OF SCOPE

Undeclared SUMO parity. Passing a simple route is not evidence of full SUMO
compatibility.

## 3. Simulation Requirements

### MUST PRESERVE

- Fixed simulation-time representation and explicit conversions among integer
  clock, seconds, speed and acceleration units.
- Canonical dependency order: control/events before planning; planning before
  approach publication; movement before lane changing; removal outside lane
  iteration; demand before insertion; output after completed state; one clock
  advance.
- Synchronous plan/commit semantics so vehicles do not see an arbitrary mixture
  of old/new positions within one movement phase.
- Clean termination by configured end or absence of future activity, with
  unfinished-output policy.
- If move-only external stepping is supported, represent it as an incomplete
  step and complete post-processing exactly once.

### MAY CHANGE

Work partitioning and parallel implementation, provided ordering, tie-breaking,
RNG streams and observable results follow the declared profile.

### OPTIONAL

Mesoscopic segment/queue path, multi-threaded lane execution, state reload loop.

## 4. Network Requirements

### MUST PRESERVE

- Directed edges containing indexed lanes with length, geometry, speed,
  friction/permissions and outgoing lane-to-lane connections.
- Junctions and links as explicit movement/conflict data, including optional
  internal/via lanes.
- Vehicle-class permission and route-continuation checks at lane/link level.
- Consistent connection order, right-of-way/foe data and signal link indices.
- Validation that every reference and lane index resolves before stepping.

### MAY CHANGE

Use immutable arrays/IDs instead of pointers and preprocess conflict zones into
another representation.

### OPTIONAL

Geographic projection, pedestrian walking areas, rail/water/bidirectional lanes,
rich shapes and runtime topology mutation.

## 5. Vehicle Requirements

### MUST PRESERVE

- Unique identity; type/physical parameters; route and current route position;
  lane/longitudinal/lateral position; speed/acceleration/prior state; departure
  and arrival procedures; stop plan; lifecycle state.
- Globally owned lifetime distinct from lane placement references.
- Atomic insertion, lane crossing/change, off-road transfer and removal state
  transitions.
- Device/reminder/listener hooks at declared lifecycle points if those outputs
  or APIs are supported.
- Route replacement validation and explicit behavior for invalidated stops.

### MAY CHANGE

Object layout, inheritance, memory management and component storage.

### OPTIONAL

Rail-specific state, electric/battery/emissions devices, taxi/ToC/SSM and other
device families.

## 6. Car-Following Requirements

### MUST PRESERVE

- At least one explicitly named model; for SUMO-default compatibility implement
  the applicable Krauss variant and its parameters, stochastic behavior and
  integration assumptions.
- Model interface capable of follow, stop and free-speed constraints plus
  secure/braking gap and acceleration/deceleration bounds.
- Combination with lane speed, stop, junction/link, lane-change and external
  constraints before movement.
- Model-specific state and RNG checkpointing when relevant.

### MAY CHANGE

Represent speed constraints as pure functions or a constraint graph.

### OPTIONAL

IDM/EIDM, ACC/CACC, Wiedemann/W99, Kerner, rail and other concrete models.

### OUT OF SCOPE

Calling one generic acceleration formula “SUMO car-following compatibility.”

## 7. Lane-Changing Requirements

### MUST PRESERVE

- Route-strategic, cooperative, speed-gain and keep-side motivations for the
  selected model; explicit priority/tie behavior.
- Neighbor leader/follower and secure-gap safety checks.
- Speed advice/cooperation applied before longitudinal execution.
- Lane membership and partial/shadow reservation consistency through maneuver.
- External lane-change mode semantics if exposed.

### MAY CHANGE

Flag encoding, scan data structures and lateral-coordinate implementation.

### OPTIONAL

Continuous sublane SL2015 behavior, opposite overtaking and multi-lane maneuvers.
A minimal profile may support only discrete LC2013-like changes.

## 8. Routing Requirements

### MUST PRESERVE

- Directed admissible graph filtered by class/permissions/restrictions.
- Deterministic path and tie policy for the selected cost/algorithm profile.
- Edge sequence validity, mandatory via order and failure for unreachable paths.
- Time-dependent weights/rerouting cache invalidation if supported.
- Separation of path computation from vehicle motion and insertion.

### MAY CHANGE

Use any shortest-path data structure/algorithm that meets declared results.

### OPTIONAL

A*, CH/CHWrapper, arc flags, intermodal routing, emissions costs, TAZ routing,
dynamic user assignment orchestration.

## 9. Traffic-Control Requirements

### MUST PRESERVE

- TLS ID/program/phase model, valid phase state length and mapping to movement
  links.
- Fixed-time phase/offset/next-switch semantics for a minimal signal profile.
- Apply signal state before current-step movement planning.
- Treat green as one link constraint, not a bypass of conflict/downstream checks.
- Program/phase changes and state queries if corresponding API is supported.

### MAY CHANGE

Implement controllers as explicit state machines or rules rather than SUMO's
inheritance structure.

### OPTIONAL

Actuated, delay-based, SOTL, NEMA, WAUT coordination, rail and custom expression
controllers.

## 10. Public-Transport Requirements

### MUST PRESERVE (when PT profile enabled)

- Ordered vehicle stops, arrival/dwell/departure conditions, capacity,
  boarding/alighting and line matching.
- Person ride-stage ownership and transition between waiting, vehicle and next
  stage.
- Route/stop compatibility after rerouting and state restore.

### MAY CHANGE

Waiting indexes, stop spatial data structures and dispatch implementation.

### OPTIONAL

GTFS import, parking-area stop behavior, containers, taxi/DRT and triggered
departures. All PT is optional for the minimal vehicle-only profile.

## 11. Input Requirements

### MUST PRESERVE

- Versioned schema/model for supported network, routes/trips/flows/types,
  configuration and optional additional/state files.
- Explicit units, defaults, relative-path resolution and precedence (CLI over
  configuration if SUMO-style CLI is claimed).
- Reference validation and actionable errors.
- Streaming or equivalent bounded loading for large time-ordered demand if
  advertised.

### MAY CHANGE

Internal parser technology and native internal format. A converter can isolate
SUMO XML compatibility from the engine.

### OPTIONAL

OSM/OpenDRIVE/VISUM/Vissim/MATSim/shapefile import and synthetic generation.

## 12. Output Requirements

### MUST PRESERVE

- Define the state boundary and timestamp for every output.
- Deterministic ordering/precision for compatibility-tested outputs.
- Final per-vehicle output exactly once, including configured unfinished policy.
- Correct interval reset/aggregation for supported detectors.
- Well-formed closure on normal shutdown and documented behavior on failure.

### MAY CHANGE

Offer an internal event stream and adapt it to XML/CSV/Parquet writers.

### OPTIONAL

The full SUMO exporter/device catalog. Minimal profile should select a small
acceptance set such as summary, tripinfo and trajectory/FCD.

## 13. API Requirements

### MUST PRESERVE (for claimed API compatibility)

- Domain/object/variable model, command types, units, errors, simulation-step
  synchronization and subscription timing.
- Atomic/scheduled semantics of vehicle, route, lane and traffic-light mutation.
- Equivalent results between in-process and remote adapters for shared domains.
- Protocol constants and compound-value field order for TraCI wire parity.

### MAY CHANGE

Core engine API can be idiomatic and instance-based; implement compatibility as
adapters rather than static global domains.

### OPTIONAL

TraCI binary protocol, libsumo-shaped bindings, C++/Java/C#/Python bindings, FMI.

## 14. State Requirements

### MUST PRESERVE

- Single owners and valid cross-references described in `05_STATE_MODEL.md`.
- Ordered lane occupancy and consistent vehicle route/lane/lifecycle state.
- Explicit transient planning/approach/reservation state reset each appropriate
  phase.
- Checkpoint completeness for every feature claimed resumable, including RNG.
- Transactional load failure: do not continue with a half-built world.

### MAY CHANGE

Serialization format and storage layout unless SUMO state-file compatibility is
claimed.

### OPTIONAL

Binary/delta snapshots and cross-version migration.

## 15. Behavioral Invariants

All applicable invariants in `09_INVARIANTS.md` are normative. Highest priority:

1. one completed step/one time advance;
2. consistent route–edge–lane–link state;
3. ordered unique lane occupancy;
4. signal/foe/link mapping consistency;
5. plan-before-commit motion;
6. insertion/removal exactly-once lifecycle counters and hooks;
7. no stale pointers/IDs in approaches, reservations, devices or transfers;
8. deterministic RNG stream ownership for reproducible profiles.

## 16. Edge Cases

### MUST PRESERVE

For every supported feature, define/test at least: zero traffic, no leader,
invalid/disconnected route, prohibited/unavailable lane, red and downstream-
blocked link, insertion retry and terminal discard, arrival/removal, collision
policy, extreme congestion, unusual supported step length, and save/load if
enabled. Use `10_EDGE_CASES.md` as the full candidate matrix.

### OPTIONAL

Unsupported modes may fail fast with an explicit capability error rather than
silently approximating them.

## 17. Compatibility Requirements

### Profiles

Recommended staged profiles:

| Profile | Required surface |
|---|---|
| P0 core | native normalized network/demand, fixed step, one CF model, no LC or signals, basic outputs |
| P1 urban micro | discrete LC, right-of-way/internal links, fixed TLS, insertion/removal/collisions |
| P2 multimodal | persons, one pedestrian model, stops/public transport |
| P3 control | stable direct API plus selected TraCI domains/subscriptions |
| P4 extended | additional CF/LC/TLS/routing models, devices/detectors, state |
| P5 ecosystem | importers, GUI/editor, meso, bindings and advanced tools |

Compatibility must be claimed per profile, model and format version, with a
machine-readable capability manifest. Numerical tolerances must be justified;
exact XML text equality is necessary only where it is the declared contract.

## 18. Testing Requirements

### MUST PRESERVE

- Unit tests for formulas, state transitions and invariants absent from SUMO's
  current narrow gtest layer.
- Golden scenario tests derived from applicable SUMO functional fixtures.
- Cross-module tests for step order, link/TLS conflicts, insertion under
  congestion, lane-change+CF coupling, removal/device output and state restore.
- Deterministic seeds and recorded version/profile/options.
- Differential tests comparing trajectories/events/outputs against this pinned
  SUMO revision for legally and technically approved fixtures.
- Adversarial tests for stale approach/reservation state, duplicate removal,
  invalid references and half-step misuse.

### MAY CHANGE

Test framework and storage of expected data. Prefer semantic/tolerance-aware
comparisons where exact serialization is not a requirement.

## 19. What Must Be Preserved

For any selected feature:

- observable state transitions and ordering;
- units, defaults and parameter meanings;
- topology/permission/conflict semantics;
- selected algorithm equations and stochastic behavior;
- lifecycle and output/API effects;
- edge-case and error policy;
- deterministic reproducibility contract;
- license/attribution obligations for any reused material (legal review needed).

## 20. What Can Be Reimplemented Differently

- Programming language, package layout and naming.
- Singleton/static-global removal in favor of world instances/dependency
  injection.
- Raw pointers replaced by stable handles/generational IDs.
- Immutable compiled network plus transactional dynamic overlays.
- Constraint/event pipeline replacing deeply coupled method calls.
- Parallel scheduling with explicit barriers and deterministic reductions.
- Renderer/UI as an external client consuming snapshots and sending commands.
- Native data and checkpoint formats, with explicit import/export adapters.
- More granular unit/property/model-checking tests than SUMO currently has.

## Recommended architecture for a new implementation (inference)

This is a design inference, not a claim about SUMO internals:

```text
Network compiler -> immutable network image
Demand loader    -> scheduled entity definitions
World state      -> authoritative vehicles/persons/controllers
Step pipeline    -> ordered plan/resolve/commit barriers
Event stream     -> detectors, outputs, subscriptions, GUI snapshots
Command layer    -> transactional external mutations
Adapters         -> SUMO XML, TraCI, chosen outputs
```

The structure isolates compatibility at boundaries while retaining SUMO's
important ordering/state semantics.

## Source baseline and uncertainty

Requirements are grounded in the detailed documents and source maps for revision
`d8461b9d3306a4b26cf25b3dd49da551e459bef1`. Specialized models, every XML
attribute, device and exporter are not fully specified. Do not expand a profile
without inspecting its concrete source/tests and updating this specification.

## Confidence

High as a scoped reimplementation contract; not a promise of whole-SUMO parity.
