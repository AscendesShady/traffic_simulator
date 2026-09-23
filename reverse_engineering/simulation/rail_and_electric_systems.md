# Rail and electric systems

## Purpose and scope

This page maps two specialized runtime systems that cut across ordinary road
movement:

- railway conflict control, driveways, constraints, crossings, and deadlock
  relations;
- battery charging, charging-station selection, electric-hybrid vehicles,
  overhead wires, traction substations, and electrical circuits.

They share the normal vehicle, route, stopping-place, device, output, state,
and API infrastructure, but each owns additional state whose update order is
behaviorally significant.

## Responsibilities

- Turn rail routes, occupancy, foes, and timetable constraints into safe link
  states and deadlock relations.
- Derive road/rail crossing phases from approaching trains.
- Account for vehicle energy consumption, recuperation, storage, and stationary
  or in-transit charging.
- Select charging stops and rescue actions from route energy and station state.
- Couple electric-hybrid demand to overhead-wire and traction-substation circuit
  limits.
- Publish specialized state through outputs, state files, GUI, and APIs.

## Inputs and outputs

Inputs include loaded rail links/routes, traffic-light definitions, constraint
additionals, vehicle classes and trip IDs; vehicle/device parameters and motion;
charging-station/wire lane intervals; station occupancy; substation/circuit
topology; runtime options; state files; and TraCI/libsumo changes.

Outputs include rail link phases, driveway reservation/occupancy, wait/deadlock
relations, battery state of charge and energy counters, selected charging stops,
delivered current/voltage/energy, circuit limit reasons, tripinfo/specialized
files, GUI state, and domain API values.

## Dependencies and consumers

Both systems depend on `MSNet`, lanes/links/vehicles/routes, traffic-light and
stopping-place registries, `MSMoveReminder`, option/XML loading, output devices,
and state handling. Station finding additionally depends on the generic routing
and stopping-place-rerouter infrastructure; overhead supply depends on
`src/utils/traction_wire/`.

Consumers include vehicle/link admission, insertion and teleport handling,
routing/stops, device output, TraCI/libsumo domains, state save/load, and GUI
renderers. None of these adapters should duplicate the authoritative rail or
energy state.

## Important classes and functions

| Symbol | Architectural role |
|---|---|
| `MSRailSignal`, `MSDriveWay` | route-dependent signal state and protected path |
| `MSRailSignalControl` | active signals, wait graph, route-change notifications and deadlock checks |
| `MSRailSignalConstraint` | precedence/insertion constraint family and trip lookup |
| `MSRailCrossing` | road/rail crossing phase derivation |
| `MSDevice_Battery` | consumption, recuperation, storage and station charging |
| `MSDevice_StationFinder` | charging-stop search, scoring, rerouting and rescue |
| `MSChargingStation` | lane interval, power/delay/type policy and charging output |
| `MSDevice_ElecHybrid` | vehicle-side battery/wire energy exchange |
| `MSOverheadWire`, `MSTractionSubstation` | supply interval, shared electrical limits and output |
| `MSRailSignalControl::updateSignals()` | updates active rail signals before road TLS switching |
| `MSRailSignalControl::haveDeadlock()` | evaluates the recorded vehicle wait graph |
| `MSDevice_Battery::notifyMoveInternal()` | movement and charging energy update |
| `MSDevice_StationFinder::findChargingStation()` | scores and selects feasible station candidates |
| `MSDevice_ElecHybrid::computeChargedEnergy()` | applies propulsion/recuperation efficiency to net exchange |

## Rail subsystem

### Responsibilities

Rail control reserves or evaluates route-dependent driveways through signals,
prevents incompatible movements, applies timetable/insertion constraints, and
detects wait-relation cycles used by rail-deadlock handling. Rail crossings are
traffic-light logics driven by approaching rail traffic.

Primary source:

- `src/microsim/traffic_lights/MSRailSignal.*`
- `src/microsim/traffic_lights/MSRailSignalControl.*`
- `src/microsim/traffic_lights/MSRailSignalConstraint.*`
- `src/microsim/traffic_lights/MSDriveWay.*`
- `src/microsim/traffic_lights/MSRailCrossing.*`

### State and ownership

| Owner | State |
|---|---|
| `MSRailSignal` | controlled links, route-specific driveways, current derived phase, constraints and diagnostic vehicle lists |
| `MSDriveWay` | route span, conflict/flank protection, occupied/reserved state, reminders and follower relationships |
| `MSRailSignalControl` | registered signals, signalized/moving-block classes, wait relations, deadlock checks and driveway followers |
| `MSRailSignalConstraint` subclasses | trip/vehicle ordering or precedence condition and active state |
| `MSRailCrossing` | crossing links and derived open/closing/closed/opening phase |

Driveways are derived from a vehicle route through controlled links; they are
not static signal phases. The `MSBaseVehicle::replaceRouteEdges()`/
`replaceRoute()` path schedules `activateRemindersOnReroute()` as a begin-step
event so rail driveways are updated before the next signal update.

### Step integration

`MSNet::simulationStep()` executes begin-step events and then calls
`MSRailSignalControl::updateSignals(myStep)` before ordinary
`MSTLLogicControl::check2Switch()` and before movement planning/execution. This
lets current route occupancy and constraints determine link states used in the
same step.

After movement and person/container processing, `MSNet` calls
`resetWaitRelations()` before vehicle insertion. The comment preserves wait
relations created by insertion for evaluation in the next step. Reordering
these calls can hide or invent deadlocks.

`MSRailSignal::trySwitch()` does not perform the normal phase update; the
central rail control invokes `updateCurrentPhase()` instead. A generic TLS
refactor must preserve that exception.

### Admission and deadlocks

`MSRailSignal` finds the driveway corresponding to the approaching vehicle's
route. Permission depends on occupancy, conflicting driveways, flank switches,
foes, moving-block policy, and configured constraints. When blocked, the
control records which vehicle/constraint caused the wait.

`MSRailSignalControl::haveDeadlock()` follows wait relations to detect cycles.
`MSLane` uses this result with the configured rail-signal deadlock teleport
threshold. This is separate from ordinary gridlock and disconnected-route
teleport tests.

Insertion constraints are checked from `MSLane` through
`MSRailSignal::hasInsertionConstraint()`. Constraint state and trip-ID lookup
are persisted by `MSRailSignalConstraint`; signal control and constraint
registries are explicitly cleared during network teardown/state reset.

### Rail invariants and edge cases

- A signal's displayed link state must correspond to its currently permitted
  route-dependent driveway set.
- A route change invalidates or rebuilds affected driveway/reminder state before
  the next rail update.
- Wait relations have a defined step lifetime; stale relations must not leak
  into later deadlock checks.
- Moving-block and fixed-block vehicle classes may use different admission
  rules.
- Timetable constraints identify trips and may need a trip-to-vehicle mapping
  after state load or insertion.
- Crossing phases are derived from rail approach timing rather than advancing
  as an independent fixed cycle.
- Diagnostic blocking/rival/priority vehicle lists exposed through TraCI must
  be populated for the requested signal/link before retrieval.

## Battery and charging-station subsystem

### Responsibilities and flow

`MSDevice_Battery` attaches to a vehicle through the normal device builder. Its
movement callback computes consumption or recuperation, updates energy and
state-of-charge fields, applies charge-rate/curve limits, and interacts with an
`MSChargingStation` when the vehicle is within its lane interval and satisfies
the station's stopped/in-transit and delay policy.

`MSChargingStation` is an `MSStoppingPlace`. It owns power, efficiency, charge
type, delay, per-vehicle charge records, total charged energy, and output
ordering. Battery state is saved/loaded with the vehicle device; charging
station interval output is a separate observable record.

`MSDevice_StationFinder` is both a vehicle device and an
`MSStoppingPlaceRerouter`. It estimates route energy demand, scores candidate
stations, adds or revises charging stops, applies charging strategies, and can
trigger rescue behavior when state of charge is too low. Its decisions depend
on station capacity/occupancy, route cost, expected consumption, charge power,
waiting time, and user-configurable scoring components.

### Battery invariants

- Stored energy remains within the configured usable/capacity limits, subject
  to explicit validation/clamping behavior.
- Propulsion consumption, recuperation, and station charging are accounted for
  once per movement/time interval.
- Station power, vehicle maximum charge rate, temporary strategy limit,
  efficiency, delay, and charge curve jointly bound accepted energy.
- Charge type compatibility is checked; fuel tracking and electrical charging
  are not silently interchangeable.
- A station-finder route or stop change must go through shared rerouting and
  stop-consistency machinery.
- Saving/loading must preserve enough battery and planned-charging state for a
  continuous result after restart.

## Electric-hybrid and overhead-wire subsystem

### Responsibilities and state

`MSDevice_ElecHybrid` models a vehicle that can draw from or feed an overhead
electrical supply while maintaining onboard battery state. Its movement
callback locates the active `MSOverheadWire`, computes requested current and
energy with propulsion/recuperation efficiencies, and records the prior/current
wire association so a vehicle is not left connected after moving away.

`MSOverheadWire` is a lane-bound stopping-place-like interval connected to an
`MSTractionSubstation`. A substation supplies one or more wire sections and owns
voltage/current constraints plus circuit/output state. Electrical network
helpers under `src/utils/traction_wire/` solve the circuit and report limiting
conditions.

The electrical result is coupled: vehicle demand affects wire voltage/current,
substation limits affect delivered energy, and delivered energy updates the
vehicle battery. Replacing this with independent per-vehicle charging changes
multi-vehicle behavior.

### Electric invariants and edge cases

- A vehicle draws from at most its currently occupied compatible wire segment.
- Wire sections must reference a valid traction substation/circuit topology
  before coupled calculation.
- Aggregate requested current may exceed supply; the circuit solution and
  limiting reason are observable through output/API state.
- Recuperation can reverse energy flow where the model and circuit permit it.
- Entering/leaving adjacent wire segments must not double charge or retain a
  stale connection.
- A depleted onboard battery, missing wire, disconnected circuit, voltage
  collapse, and current limiting are distinct states.
- Circuit topology or solver changes require conservation, limit, and
  multi-vehicle regression tests, not only single-vehicle energy checks.

## Interfaces and outputs

TraCI/libsumo expose traffic-light rail diagnostics and charging/overhead-wire
domains or parameters through their normal domain wrappers. Tripinfo and
specialized charging/wire/substation outputs are compatibility surfaces: IDs,
time intervals, units, accumulated versus per-step values, and write ordering
must remain stable for consumers.

GUI classes render rail signal/link states and electric infrastructure from
runtime objects. Rendering is a consumer; it must not become the owner of
reservation, charge, or circuit state.

## Tests

Primary functional suites include:

- `tests/sumo/rail/` for rail signals, crossings, constraints, insertion,
  moving blocks, route changes, and deadlock behavior;
- `tests/sumo/devices/battery/` for battery consumption and station charging;
- `tests/sumo/devices/elechybrid/` for overhead-wire/electric-hybrid behavior;
- `tests/sumo/devices/stationfinder/` for selection, rerouting, rescue, and
  charging strategies;
- `tests/complex/traci/chargingstation/` and
  `tests/complex/traci/overheadwire/` for remote-control/query surfaces.

High-value additions include save/load during an active rail wait cycle,
route replacement immediately before a signal update, equal-priority rail
approaches, multiple vehicles sharing a current-limited substation, adjacent
wire transitions, and station selection when occupancy changes after scoring.

## Modification points

- Rail phase and driveway permission: `MSRailSignal::updateCurrentPhase()` and
  `MSDriveWay` conflict methods.
- Rail lifecycle/deadlock graph: `MSRailSignalControl::vehicleStateChanged()`,
  `addWaitRelation()`, `haveDeadlock()`, and `updateSignals()`.
- Timetable/insertion policy: `MSRailSignalConstraint` subclasses and
  `MSRailSignal::hasInsertionConstraint()`.
- Road/rail crossing timing: `MSRailCrossing::updateCurrentPhase()`.
- Battery accounting: `MSDevice_Battery::notifyMoveInternal()` and charge-rate
  helpers.
- Station search: `MSDevice_StationFinder` candidate scoring, consumption
  estimation, strategy, and rescue methods.
- Hybrid/wire exchange: `MSDevice_ElecHybrid::notifyMoveInternal()` and
  `computeChargedEnergy()`.
- Supply network: `MSOverheadWire`, `MSTractionSubstation`, and
  `src/utils/traction_wire/`.

## Confidence

High for ownership, lifecycle, step ordering, primary transitions, and test
locations because these were traced from source. Medium for numerical energy
and circuit equivalence and for every rail constraint subtype; those require
model-specific equation review and runtime regression execution.
