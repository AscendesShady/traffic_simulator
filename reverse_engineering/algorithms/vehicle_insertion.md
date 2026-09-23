# Vehicle insertion algorithm

## Purpose

Place due demand onto a valid departure lane without corrupting lane order or
starting in an unsafe state.

## Inputs

Due vehicle/flow parameters, route/type, departure procedures, candidate lanes,
current lane leaders/followers/pedestrians, permissions, capacity limits, and
configured insertion checks.

## Outputs

Successful on-road vehicle state, a retryable pending vehicle, or a discarded
vehicle with counters/reason side effects.

## State

Future and pending emit queues, flow counters/RNG, prechecked candidates,
vehicle dictionary/counters, and each edge's failed-insertion timestamp.

## Preconditions

Unique vehicle ID, valid type/route object, due departure time, and an existing
departure edge.

## Decision Process

`determineCandidates()` expands flows and exposes due vehicles. `emitVehicles()`
visits pending demand and `tryInsert()` enforces global vehicle count, then calls
`MSEdge::insertVehicle()`. The edge interprets depart-lane strategy; the lane
interprets depart position/speed/lateral position and calls
`isInsertionSuccess()`. Success commits via `incorporateVehicle()`; ordinary
failure is buffered for the next step; terminal failure deletes the vehicle.

## Mathematical Model

Candidate speed is bounded by lane/type limits and safe insertion speed against
leaders, followers, links and pedestrians. Probabilistic flows draw when
`rand < repetitionProbability * TS`; deterministic flows use repetition offsets
and scale/quota rounding.

## Constraints

`max-num-vehicles`, `max-depart-delay`, lane permissions, route-start validity,
gap checks, depart procedure semantics, vaporizing edges, and one successful
commit per vehicle.

## State Transitions

`FUTURE -> PENDING -> ON_ROAD`; retry loops `PENDING -> PENDING`; invalid/late/
aborted demand goes `PENDING -> DISCARDED`.

## Edge Cases

Multiple vehicles due simultaneously, probabilistic scale above/below one,
explicit unsafe speed/position, random-free search, all lanes prohibited,
precheck becoming stale, and TraCI-added vehicles.

## Implementation

`src/microsim/MSInsertionControl.cpp`, `MSEdge.cpp`, `MSLane.cpp`,
`MSVehicleControl.cpp`; details in `../simulation/vehicle_insertion.md`.

## Related Classes

`MSVehicleContainer`, `SUMOVehicleParameter`, `MSRoute`, `MSVehicleType`.

## Related Tests

Functional departure/flow tests under `tests/sumo/`, route-flow tests under
`tests/duarouter/flows/`, and `tests/complex/traci/vehicle/` add cases.

## Behavioural Invariants

Unique IDs; pending ownership is retained; counters change only on actual
build/depart/discard events; lane membership and route position agree.

## Reimplementation Notes

MUST preserve ordering, procedures, retry/discard rules, and RNG determinism.
MAY replace container structures and precheck optimization.

## Confidence

High.
