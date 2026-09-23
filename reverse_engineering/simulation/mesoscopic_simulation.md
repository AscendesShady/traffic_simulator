# Mesoscopic simulation

## Purpose and scope

SUMO's mesoscopic mode replaces lane-level vehicle dynamics with an
event-driven queue model while retaining the runtime network, routes, vehicles,
stops, devices, signals, transportables, outputs, and control interfaces around
it. The mode is selected by `MSGlobals::gUseMesoSim`; it is not a post-processing
approximation of microscopic results.

Primary implementation: `src/mesosim/`, with the branch and surrounding step
contract in `src/microsim/MSNet.cpp`. GUI adapters live in `src/mesogui/`.

## Responsibilities

- Discretize runtime edges into capacity-constrained segments and permitted
  queues.
- Schedule only vehicles whose next segment/link decision is due.
- Resolve segment admission, headways, edge/link crossings, route progression,
  stops, arrival, and mesoscopic teleports.
- Translate aggregate movement into shared lifecycle/reminder/output state.
- Persist and expose mesoscopic state through the common engine, GUI, and API
  boundaries.

## Inputs

- The loaded runtime `MSEdge`, `MSLane`, `MSLink`, route, vehicle-type, stop,
  detector, and traffic-light objects.
- Global mesoscopic options registered by the simulation frame, including
  segment length, recheck intervals, lane/multiple queues, junction control,
  overtaking, penalties, and the optional link-transmission model.
- Per-edge-type `MESegment::MesoEdgeType` values stored by `MSNet` and loaded by
  `METypeHandler`.
- Departures from `MSInsertionControl`, route changes, TraCI/libsumo commands,
  rerouters, calibrators, and state restoration.

## Outputs

- Updated vehicle segment, queue, route, stop, event-time, waiting, and
  detector-reminder state.
- Segment occupancy, flow, speed, queue block times, and junction admission
  state.
- The same high-level lifecycle notifications, detector/output framework,
  simulation clock, API step boundary, and pending-removal pipeline used by the
  rest of `MSNet`.
- Mesoscopic values exposed to GUI and domain APIs. Lane-specific quantities may
  be aggregated, inferred, or unavailable because the model does not maintain
  microscopic trajectories.

## State owners

| Owner | State |
|---|---|
| `MELoop` | edge-to-first-segment map, time-ordered leader events, invalidated events, recheck intervals |
| `MESegment` | parent edge, next segment, queues, capacity, occupancy, speeds, penalties, block/event times, detectors |
| `MESegment::Queue` | permitted classes, ordered vehicles, occupied length, entry/block times, reminders |
| `MEVehicle` | current segment and queue, event/block/entry times, route and stops, mesoscopic movement state |
| `MEVehicleControl` | construction and fleet ownership for `MEVehicle` instances |
| `MSNet` | mode flag, global mesoscopic loop, edge-type parameter map, shared controllers and clock |

`MESegment::PARKING_QUEUE` is a distinguished queue index, not an ordinary
travel queue. A null vehicle segment can represent an in-progress mesoscopic
teleport; an invalid/vaporization segment is a separate sentinel condition.

## Dependencies and consumers

The mesoscopic engine depends on the runtime `MSNet` graph, `MSEdge`/`MSLane`/
`MSLink`, shared route and vehicle definitions, `MSInsertionControl`,
`MSVehicleControl`, traffic lights, move reminders, options, and output/state
services. It reuses these owners; it does not create a parallel input network.

Its consumers are the outer `MSNet` step, detectors and outputs, TraCI/libsumo,
state save/load, and the optional `mesogui` render adapters. Person/container
controllers and routing consume the shared network/vehicle interfaces on either
side of the central micro/meso branch.

## Important classes and functions

| Symbol | Architectural role |
|---|---|
| `MELoop` | builds segment chains and owns the due-leader event queues |
| `MESegment` | base capacity/queue/admission/transfer model |
| `MELSegment` | link-transmission-model overrides |
| `MEVehicle` | mesoscopic vehicle route, stop, queue, and event state |
| `MEVehicleControl` | creates `MEVehicle` instances through shared fleet ownership |
| `MEInductLoop` | segment-aware induction-loop output |
| `METriggeredCalibrator` | segment-aware flow calibration/emission/removal |
| `MELoop::simulate()` | drains due events up to the current step time |
| `MELoop::checkCar()` | selects and schedules one leader's next transition |
| `MELoop::changeSegment()` | performs admission/link/arrival transition logic |
| `MESegment::hasSpaceFor()` | selects a queue and earliest entry time |
| `MESegment::send()` / `receive()` | paired segment membership/occupancy transition |

## Construction

`MSNet` creates `MELoop` when mesoscopic mode is active. During network loading,
`MELoop::buildSegmentsFor()` divides each eligible edge into at least one
segment. The requested edge length is rounded to a segment count, then actual
segment length is derived from the edge length.

Segments form a forward chain. `--meso-ltm` selects `MELSegment`, whose headway
and insertion rules implement the link-transmission variant. Multiple queues
are enabled from lane-queue policy or, initially, for branching multi-lane
edges. Queue permissions preserve vehicle-class feasibility even though normal
movement is not lane resolved.

## Execution order

The outer `MSNet::simulationStep()` contract is shared with microscopic mode:

1. process TraCI commands and possible early return;
2. save scheduled state and execute begin-step events;
3. update rail signals, wait for asynchronous routing, check switches;
4. call `MELoop::simulate(myStep)` instead of microscopic plan/move/lane-change;
5. flush pending removals and load routes;
6. advance waiting persons/containers and pending routing;
7. determine and emit vehicle insertions, retry transfers;
8. execute end-step work, outputs, and clock advancement.

The mesoscopic call therefore changes only the central traffic-resolution
phase. Moving it across the surrounding event, signal, insertion, removal, or
output boundaries changes observable behavior.

## Event-driven movement

`MELoop` stores `LeaderEvent` objects ordered by event time and a monotonically
increasing tie-break counter. `MELoop::simulate(tMax)` repeatedly processes due
events, skips explicitly invalidated copies, and calls `checkCar()`.

For a due vehicle:

1. `nextSegment()` selects the following segment, internal following edge, next
   route edge, or arrival.
2. `changeSegment()` asks the destination `MESegment::hasSpaceFor()` for a queue
   and earliest feasible entry.
3. Link admission is checked through `MEVehicle::mayProceed()` when an edge
   boundary is crossed.
4. A successful transfer calls the old segment's `send()` and the new segment's
   `receive()`, including the appropriate `MSMoveReminder::Notification`.
5. A blocked vehicle receives another event time based on destination state,
   link/full recheck intervals, and teleport thresholds.

Only queue leaders need scheduled movement checks. `addLeaderCar()` registers
the next event and approaching-link state. Removing or rescheduling a leader
uses the invalidation queue rather than mutating the priority queue in place.

## Capacity, headway, and queues

`MESegment::hasSpaceFor()` is the admission boundary. It chooses a permitted
queue and returns either the requested time, a later time, or `SUMOTime_MAX`.
The decision depends on occupied vehicle length, capacity/jam threshold, queue
entry block time, successor state, and model variant.

`MESegment::send()` and `receive()` are the state transition pair. They update
vehicle order, occupancy, event/block times, detector reminders, edge changes,
and the next leader event. Junction control and TLS/minor-link penalties apply
at the last segment of an edge. Overtaking and multi-queue settings change
queue selection/order but do not create microscopic lateral trajectories.

`MELSegment` overrides headway, send, entry-block, and insertion-space behavior.
Do not merge it into the base formula merely because both classes expose the
same segment interface.

## Insertion, stops, arrival, and teleport

`MEVehicleControl::buildVehicle()` supplies the mesoscopic vehicle type to the
shared control layer. Departure succeeds only if the first segment accepts the
vehicle through `MESegment::initialise()` and its insertion-space rules.

Stops are processed by `MEVehicle`; parking uses the distinguished parking
queue. The source explicitly warns that join stops are not available in meso.
At route completion, `changeSegment()` marks arrival and schedules deferred
removal through `MSVehicleControl`.

When a vehicle exceeds the relevant waiting threshold, `MELoop::teleportVehicle()`
first tries a later segment. If none is available it removes the vehicle from
the current segment, advances its route using speed-limit travel time, and
schedules a later reinsertion event. Detector notifications distinguish
teleport start, continuation, and arrival. With remove-gridlocked enabled, the
vehicle is scheduled for removal instead.

## Detectors, calibrators, GUI, and APIs

- `MEInductLoop` is the mesoscopic induction-loop output implementation.
- Segment queues attach `MSMoveReminder` instances and prepare detector data
  before interval writes.
- `METriggeredCalibrator` adapts emission/removal and capacity logic to segments.
- `GUIMEVehicle`, `GUIMEVehicleControl`, and `GUIMEInductLoop` expose aggregate
  state to the GUI; lanes cache their mesoscopic GUI segment associations.
- Shared TraCI/libsumo domains remain callable, but callers must not assume
  lane-level positions, accelerations, or neighbor relations have microscopic
  meaning in this mode.

## Save/load behavior

`MEVehicle::saveState()`/`loadState()` persist vehicle segment, queue and timing
state. `MESegment::saveState()`/`loadState()` persist queue contents and block
times. `MELoop::clearState()` clears both event queues before restoration.
Restoration must rebuild consistent queue membership and leader events; loading
vehicle fields without segment occupancy is not a valid checkpoint.

The comment in `MELoop.h` marks segment save/load handling as an area needing
care. Treat state-file compatibility as a behavior to test, not as guaranteed
by the presence of serialization methods.

## Edge cases and invariants

- Every simulated edge has at least one segment and the edge-to-first-segment
  map is indexed by the edge numerical ID.
- A vehicle belongs to at most one normal segment queue at a time.
- Queue occupancy equals the space attributed to its members and must not go
  negative across send/receive/removal.
- Event times processed by `MELoop::simulate()` are nondecreasing; stale events
  are invalidated rather than executed.
- Permission filtering remains active even when lanes are aggregated.
- Internal edges may be traversed when internal-lane mode is enabled.
- Full queues, closed links, disconnected routes, parking, vaporization, and
  teleportation have different transitions and notifications.
- Microscopic collision and lane-change phases are deliberately skipped in
  mesoscopic mode.

## Tests

Primary functional coverage is under `tests/sumo/meso/`, with additional
mesoscopic variants selected by the TextTest configuration. Cross-cutting state,
TraCI, routing, detector, stop, calibrator, and output tests may exercise the
same shared interfaces in meso mode.

High-value focused regressions should cover:

- equal-time event ordering and event invalidation;
- segment and multi-queue capacity at exact thresholds;
- route branching with vehicle-class restrictions;
- junction/TLS block and recheck timing;
- insertion failure and retry;
- parking and non-parking stops;
- single-step and multi-edge teleportation;
- save/load with blocked queues and pending leaders;
- detector counts across segment boundaries;
- API values that differ from microscopic resolution.

## Modification points

- Change discretization and queue capacity in `MELoop::buildSegmentsFor()` and
  `MESegment::initSegment()`.
- Change admission in `MESegment::hasSpaceFor()` and
  `hasSpaceForInsertion()`.
- Change headways/flow propagation in `MESegment::computeHeadway()` or the
  `MELSegment` overrides.
- Change link and route progression in `MELoop::changeSegment()`, `checkCar()`,
  and `nextSegment()`.
- Change teleport semantics in `MELoop::teleportVehicle()`.
- Change mesoscopic vehicle state/stop behavior in `MEVehicle`.

Any such change should be checked against insertion, reminders, signals,
removal, state files, API output, and both segment implementations.

## Confidence

High for construction, state ownership, event ordering, movement transitions,
and integration boundaries because these were traced directly through
`MSNet`, `MELoop`, `MESegment`, `MELSegment`, and `MEVehicle`. Medium for
numerical equivalence of all mesoscopic parameter combinations; that requires
running the full meso regression matrix.
