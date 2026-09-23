# Vehicle removal algorithm

## Purpose

Finalize arrivals and exceptional removals safely after movement, producing
device/output side effects exactly once.

## Inputs

Arrival condition, removal reason, current lane/route state, vehicle devices,
persons/containers, pending-removal set, keep time, collision/teleport policy.

## Outputs

Lane/network detachment, state-listener events, final trip/device output,
updated counters, and eventual object destruction.

## State

Lane membership, link approaches, lane-change reservations, global dictionary,
running/ended/discard counters, pending removal list, optional kept-vehicle
timer, and transfer list.

## Preconditions

The vehicle has a defined owner and is removed from mutable iteration only via
the deferred protocol, except for explicitly non-running discarded demand.

## Decision Process

Movement or an API detects arrival/removal, calls lane/vehicle cleanup, and
schedules removal. `MSNet` flushes pending removals after movement/lane change.
`MSVehicleControl::removePending()` sorts by numerical ID, emits ARRIVED state,
generates device output, updates statistics, and deletes now or after keep time.
Teleport/parking vehicles instead pass through `MSVehicleTransfer` until safe
reinsertion or route-end removal.

## Mathematical Model

Arrival position/speed are calculated from vehicle parameters and route end;
transfer progress uses edge current travel time with a minimum teleport speed.
Removal itself is a discrete lifecycle procedure.

## Constraints

One finalization, deterministic order, valid notification reason, no stale
approach/reminder/reservation links, and passenger/device cleanup before delete.

## State Transitions

See `../simulation/vehicle_removal.md`; terminal paths end in `DELETED`, while
parking/teleport may return to an on-road state. These are conceptual lifecycle
states rather than a single source enum.

## Edge Cases

Collision remove, teleport beyond route, taxi exception, keep-after-arrival,
vaporization, never-departed discard, parking egress blocked, state shutdown.

## Implementation

`src/microsim/MSVehicle.cpp`, `MSLane.cpp`, `MSVehicleControl.cpp`,
`MSVehicleTransfer.cpp`.

## Related Classes

`MSDevice_Tripinfo`, `MSMoveReminder`, `MSTransportableControl`, `MSNet`.

## Related Tests

Arrival, tripinfo, teleport and state cases under `tests/sumo/` and
`tests/complex/state/`; TraCI removal under `tests/complex/traci/vehicle/`.

## Behavioural Invariants

Final output and counter decrement happen once; deleted pointers are absent
from lane, approach, transfer, and dictionary structures.

## Reimplementation Notes

MUST preserve lifecycle events/output ordering. MAY use handles/GC instead of
raw pointers. Audit the non-FOX pending-removal anomaly recorded in the subsystem
document rather than copying it as a requirement.

## Confidence

High for normal paths, medium for specialized devices.
