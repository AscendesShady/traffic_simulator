# Lanes: build-time records and runtime occupancy

## Purpose

A lane is both a geometric/permission-bearing part of a road and the ordered
container on which microscopic vehicles are inserted, planned, moved, and
removed. SUMO deliberately has two representations:

- `NBEdge::Lane` is mutable network-building data owned by `NBEdge`.
- `MSLane` is the runtime object loaded from a `.net.xml` file.

These representations are related by the network file, not by an in-memory
conversion inside `sumo`.

Primary implementation:

- `src/netbuild/NBEdge.h` (`NBEdge::Lane`, `NBEdge::Connection`)
- `src/microsim/MSLane.h`, `src/microsim/MSLane.cpp`
- `src/netload/NLEdgeControlBuilder.cpp`

Related tests:

- `tests/netconvert/inner_lanes/` and lane-related cases under
  `tests/netconvert/node_building/`
- `tests/sumo/lc_model/`, `tests/sumo/sublane_model/`
- `tests/traci/get_variable/lane/`, `tests/traci/set_variable/lane/`

## Build-time lane contract

`NBEdge` owns a vector of lane records. Each record carries speed, friction,
permissions, width, end offset, shape, stop offsets, lane-change permissions,
and loaded/generated connection information. `NBNode::computeLanes2Lanes()` and
`NBEdge::computeLanes2Edges()`/connection helpers complete topology after raw
import. `NWWriter_SUMO::writeLane()` serializes the result.

The lane index is edge-relative. Reordering, adding, or deleting lanes therefore
requires connection indices, traffic-light link indices, stop locations, and
permissions to be recomputed or validated.

## Runtime state

`MSLane` owns or references:

| State | Meaning |
|---|---|
| `myVehicles` | ordered full vehicles currently on the lane |
| temporary vehicle buffer | next-state collection integrated after movement |
| partial vehicles/reservations | vehicles overlapping the lane or reserving lateral space |
| links | legal outgoing `MSLink` movements |
| incoming lanes | predecessor lanes and their via links |
| speed/friction/length/width/shape | physical and regulatory lane properties |
| permissions | allowed vehicle classes, including transient changes |
| move reminders | detectors/devices notified by passage |

The precise fields are declared in `src/microsim/MSLane.h`; the behavioural
mutators are in `src/microsim/MSLane.cpp`.

## Execution

1. `MSEdgeControl::planMovements()` calls `MSLane::planMovements()` on active
   lanes.
2. The lane iterates vehicles, lets each vehicle compute a candidate move, and
   records junction approach information (`MSLane::setJunctionApproaches()`).
3. `MSEdgeControl::executeMovements()` calls `MSLane::executeMovements()`.
4. Each vehicle executes through `MSVehicle::executeMove()`; survivors are put
   in current/target lane buffers.
5. `MSLane::integrateNewVehicles()` restores the ordered lane container.
6. Lane changing is orchestrated through `MSLane::changeLanes()` and the owning
   edge's `MSLaneChanger`.

Source: `src/microsim/MSEdgeControl.cpp`, `src/microsim/MSLane.cpp:1595`
(`planMovements`), `:2318` (`executeMovements`), `:2498` (`changeLanes`), and
`:2605` (`integrateNewVehicles`).

## Insertion

`MSLane::insertVehicle()` interprets the chosen departure procedure and delegates
to `freeInsertion()`, `lastInsertion()`, or explicit-position checks.
`isInsertionSuccess()` checks leaders, followers, junction constraints,
pedestrians, vehicle-class permission, and configured insertion-check flags.
`incorporateVehicle()` is the commit point that associates the vehicle with the
lane and activates movement reminders.

Never bypass `incorporateVehicle()` merely by appending to `myVehicles`: doing
so skips route/lane state, detector notifications, ordering, and counters.

## Occupancy and leader queries

The lane is spatially ordered. `getLeader()`, `getFollower()`,
`getLeaderOnConsecutive()`, `getFollowersOnConsecutive()`, and
`getLeadersOnConsecutive()` traverse the current lane and connected lanes.
Occupancy getters distinguish brutto (vehicle plus gap) and netto vehicle
length. Partial vehicles and lateral overlap matter in sublane mode.

## Edge cases

- A lane may be internal, normal, crossing, priority crossing, or walking area;
  `isInternal()`, `isCrossing()`, and related predicates are behavioural.
- Bidirectional/opposite lanes use coordinate conversion (`getOppositePos()`) and
  additional leader/follower queries.
- Permissions can change transiently; cached restrictions must be invalidated.
- A vehicle can overlap multiple lanes or reserve target-lane space during a
  continuous lateral maneuver.
- Collision checks may warn, teleport, remove, or stop participants according
  to options initialized by `MSLane::initCollisionOptions()`.

## Modification points

- Geometry/permissions in generated networks: `NBEdge::Lane` and netbuild.
- Runtime insertion: `MSLane::insertVehicle()` and `isInsertionSuccess()`.
- Longitudinal storage/movement: `planMovements()`, `executeMovements()`, buffer
  integration.
- Lane changing: change the lane-change models and orchestrator together; see
  `../simulation/lane_changing.md`.

## Confidence

High — lifecycle and calls were traced through `MSNet`, `MSEdgeControl`,
`MSLane`, netload, netbuild, and functional tests.
