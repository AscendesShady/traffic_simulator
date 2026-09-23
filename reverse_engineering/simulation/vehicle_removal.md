# Vehicle removal, arrival, parking, and teleport transfer

## Purpose

SUMO separates detecting that a vehicle should leave a lane/network from final
destruction. This preserves iteration safety, output generation, state
listeners, passenger/device cleanup, and deterministic removal order.

Primary implementation:

- `src/microsim/MSVehicle.cpp` (`executeMove`, `onRemovalFromNet`, arrival)
- `src/microsim/MSLane.cpp` (`removeVehicle`)
- `src/microsim/MSVehicleControl.cpp` (`scheduleVehicleRemoval`,
  `removePending`, `deleteVehicle`)
- `src/microsim/MSVehicleTransfer.cpp`
- `src/microsim/MSNet.cpp` (phase ordering and shutdown)

Related tests: arrival/route-end scenarios under `tests/sumo/basic/` and
`tests/sumo/spec/`; teleport tests under `tests/sumo/extended/` and bugs;
parking/state tests under `tests/complex/state/`; tripinfo/vehroute output tests.

## Normal arrival

When movement reaches the configured arrival condition on the final route edge,
the vehicle leaves its lane, move reminders/devices receive a removal reason,
and `MSVehicleControl::scheduleVehicleRemoval()` defers deletion. After movement
and lane changing, `MSNet::simulationStep()` calls `removePending()`.

`removePending()` sorts vehicles by numerical ID, updates travel/running
counters, publishes `VehicleState::ARRIVED`, asks every device to generate final
output, closes/flushes tripinfo as needed, and deletes immediately or schedules
delayed deletion according to keep time.

## Non-arrival deletion

Insertion discard, vaporizing edges, invalid starts, explicit API removal,
collision policy, and shutdown can delete vehicles with different notification
or `discard` semantics. Do not report all deletion as successful arrival.

`MSVehicle::onRemovalFromNet()` removes approaching-link information, ends lane
change state, notifies devices/reminders, and clears network membership. Callers
must supply the correct `MSMoveReminder::Notification` reason.

## Transfer subsystem

`MSVehicleTransfer` temporarily owns vehicles that are:

- teleporting around gridlock/collision blockage;
- parked off the traffic stream;
- performing a scheduled jump.

`add()` ends lane-changing, emits start state, removes on-road membership, and
records transfer/proceed time. `checkInsertions()` runs once per step. Parking
vehicles process stop state and attempt safe reinsertion; teleported vehicles
attempt insertion on the current logical edge or advance virtually after a
travel-time delay. Reaching beyond the route end schedules removal (with a taxi
exception that avoids teleporting beyond its route).

## State transitions

```text
ON_ROAD -> ARRIVAL_PENDING -> DEVICE_OUTPUT -> DELETED/KEPT
ON_ROAD -> TELEPORTING -> ON_ROAD
ON_ROAD -> TELEPORTING -> ARRIVAL_PENDING
ON_ROAD -> PARKING -> REINSERTING -> ON_ROAD
LOADED/PENDING -> DISCARDED -> DELETED
```

## Invariants

- Lane membership and vehicle on-road state change together.
- Pending removal is processed outside lane iteration.
- Final device output is generated once before destruction.
- Running count is decremented exactly once for a departed vehicle.
- Transfer-owned vehicles remain in the global dictionary and preserve route/
  stop state while absent from normal lane occupancy.
- Approach records and lane-change reservations are removed on network exit.

## Source anomaly requiring review

In this checkout, the non-FOX branch of
`MSVehicleControl::isPendingRemoval()` compares `std::find(...) == end`, whereas
the FOX branch returns `contains(veh)`. Taken literally, the non-FOX result is
inverted relative to the function name and `scheduleVehicleRemoval()` caller.
This knowledge base does **not** treat that expression as desired behavior; it
should be covered by a headless unit/regression test before reimplementation.

## Modification points

Change arrival conditions in vehicle movement, cleanup protocol in
`MSVehicleControl`, and teleport/parking behavior in `MSVehicleTransfer`.
Cross-test tripinfo, passengers, devices, save/load, GUI, and TraCI remove.

## Confidence

High for the normal lifecycle and transfer flow. Medium for every specialized
removal reason; device-specific cleanup is distributed across many classes.
