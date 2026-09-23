# Vehicle insertion

## Purpose

Insertion converts loaded vehicle/flow demand into a running vehicle occupying
a valid lane without violating configured safety, capacity, permission, route,
or departure constraints.

Primary implementation:

- `src/microsim/MSInsertionControl.*`
- `src/microsim/MSEdge.cpp` (`insertVehicle`, lane selection)
- `src/microsim/MSLane.cpp` (`insertVehicle`, `isInsertionSuccess`,
  `incorporateVehicle`)
- `src/microsim/MSVehicleControl.cpp` (`buildVehicle`, `initVehicle`,
  `vehicleDeparted`)

Related tests: departure/insertion scenarios under `tests/sumo/basic/`,
`tests/sumo/spec/`, flow cases under `tests/sumo/`, insertion-related bug tests,
and TraCI vehicle-add tests in `tests/complex/traci/vehicle/`.

## Inputs

Vehicle parameters include depart time/procedure, route, type, vehicle class,
depart lane/position/speed/lateral-position procedures, insertion checks, and
stops. Flow definitions additionally carry repetition count/period/probability,
end time, scaling, and RNG state.

## State

`MSInsertionControl` maintains future scheduled vehicles (`myAllVeh`), pending
emits, optional prechecked candidates, flow definitions/counters, aborted
emits, scaling/quota data, and a flow RNG. `MSVehicleControl` owns the vehicle
dictionary and loaded/running/ended counters.

## Execution

1. Route parsing calls `MSVehicleControl::buildVehicle()` and `addVehicle()`;
   scheduled departures enter `MSInsertionControl`.
2. `determineCandidates(time)` expands due flows into uniquely suffixed vehicle
   IDs and moves due vehicles to the pending list.
3. If routing-device precheck is enabled, `checkCandidates()` calls edge
   insertion in precheck mode and records candidates.
4. Insertion events execute.
5. `emitVehicles()` retries pending vehicles in deterministic list order.
6. `tryInsert()` asks the departure edge to select a lane and insert.
7. `MSLane::isInsertionSuccess()` computes a safe placement/speed; on success
   `incorporateVehicle()` commits membership and `vehicleDeparted()` updates
   counters/listeners.

Source: `src/microsim/MSInsertionControl.cpp:127-316`,
`src/microsim/MSEdge.cpp:630-900`, `src/microsim/MSLane.cpp:457-950`.

## Failure and retry

A failed insertion normally remains pending for the next step. It is deleted
instead when maximum departure delay is exceeded, the edge is vaporizing, the
emit was aborted, or route-start lane/permission validity fails. The edge records
its last failed insertion time. `max-num-vehicles` can defer otherwise valid
departures.

Precheck is advisory: final insertion re-evaluates mutable lane state. A
reimplementation must not reserve space based only on precheck.

## Flow generation

Deterministic-period flows advance repetition offsets; probabilistic flows draw
once per eligible step from `myFlowRNG`. Demand scaling can produce a quota of
zero, one, or multiple vehicles. Changing RNG call order, rounding, or ID index
updates changes observable demand.

## Invariants

- A vehicle ID is unique in `MSVehicleControl`.
- A pending vehicle is not yet on-road; successful insertion commits exactly
  once.
- Departure edge/lane permits the vehicle class and is route-compatible.
- Running count increments only on committed departure.
- Rejected-but-retryable vehicles remain owned and reachable.

## Modification points

Change scheduling/scaling in `MSInsertionControl`, lane selection in `MSEdge`,
gap/safety checks in `MSLane`, and defaults/parsing in vehicle parameters. Test
explicit/random/free lane and position modes, congestion, permissions,
max-delay, flows, and TraCI insertion.

## Confidence

High — the scheduling, retry, and lane commit path was followed end to end.
