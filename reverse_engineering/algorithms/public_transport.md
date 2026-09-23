# Public-transport behavior

## Purpose

Represent scheduled line vehicles, stopping places, passenger waiting/boarding/
alighting, and multi-stage person plans involving rides.

## Inputs

Vehicle routes/types/line IDs, stop definitions and timing, person plans,
boarding/alighting durations, capacities, triggered rules, and routing/dispatch
information.

## Outputs

Vehicle stop state, waiting/aboard person state, delay/line information,
departures after stops, and stop/trip outputs/API state.

## State

`MSStoppingPlace`/`MSBusStop` maintain spatial stop and access/waiting data;
vehicles own `MSStop` lists and transportable devices; `MSTransportableControl`
owns waiting/running persons; `MSStageDriving` represents a ride stage.

## Preconditions

Stops reference valid lanes/positions; a ride has compatible lines or vehicle;
capacity and vehicle/person classes permit the operation.

## Decision Process

Person-plan stages register waiting demand. A line vehicle reaches a stop via
normal movement constraints, enters stop processing, determines eligible
alighting and boarding passengers/containers, applies access and duration rules,
and remains until duration/until/trigger conditions allow departure. The person
advances to its next stage on alighting.

## Mathematical Model

Dwell time combines configured minimum duration/until with boarding/loading
times and trigger conditions. Vehicle motion to/from a stop remains governed by
car-following and insertion safety; intermodal path cost is handled by the
router, not the dwell controller.

## Constraints

Capacity, boarding position/access, stop order along route, timing windows,
line matching, triggered departure, parking/roadside behavior, and vehicle
insertion after off-road parking.

## State Transitions

Person: `WAITING -> BOARDING -> RIDING -> ALIGHTING -> NEXT_STAGE`.
Vehicle: `MOVING -> APPROACH_STOP -> STOPPED -> BOARD/ALIGHT -> MOVING`.

## Edge Cases

Missed/full vehicle, wrong line, triggered vehicle with absent passenger,
overtaking/parking stop, duplicate stop, reroute invalidating stop order,
parking-area egress, and save/load mid-dwell.

## Implementation

`src/microsim/MSStoppingPlace.*`, `MSStop.h`, vehicle stop methods in
`MSBaseVehicle.cpp`/`MSVehicle.cpp`, `src/microsim/transportables/`, and vehicle
transportable devices. See `../simulation/public_transport.md`.

## Related Classes

`MSBusStop`, `MSVehicle`, `MSPerson`, `MSStageDriving`,
`MSTransportableControl`, `MSDevice_Taxi` (adjacent on-demand service).

## Related Tests

Public-transport/stop cases across `tests/sumo/`, GTFS and taxi tutorials,
`tests/complex/traci/busstop/`, person/vehicle API tests, and state tests.

## Behavioural Invariants

Each person has one active stage/location; onboard users are owned by a live
vehicle/device; stop order and route remain compatible; capacity never silently
goes negative; state restore preserves dwell and waiting relationships.

## Reimplementation Notes

MUST preserve stage/stop/boarding semantics for supported features. MAY model
waiting indexes differently. OPTIONAL: GTFS import, taxi/DRT and advanced access
geometry in a minimal simulator.

## Confidence

Medium-high — lifecycle is explicit, but timing and triggered-stop variants are
distributed across several classes.
