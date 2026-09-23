# Vehicle System (MSBaseVehicle / MSVehicle / MSVehicleType / MSVehicleControl)

## Purpose
Represents individual road vehicles, their static type/behavioral parameters, and the fleet-wide bookkeeping (construction, id registry, statistics, teardown) that ties them into the simulation.

```
Source:
src/microsim/MSBaseVehicle.h / .cpp
src/microsim/MSVehicle.h / .cpp
src/microsim/MSVehicleType.h
src/microsim/MSVehicleControl.h / .cpp
```

## Class layering
- **`SUMOVehicle`** (in `src/utils/vehicle/SUMOVehicle.h`) — abstract
  interface implemented by both microscopic (`MSBaseVehicle`) and mesoscopic
  (`MEVehicle`) vehicles, plus GUI wrappers. This is why most of the engine
  (`MSEdge`, `MSInsertionControl`, `MSVehicleControl`) talks to
  `SUMOVehicle*` rather than `MSVehicle*` directly.
- **`MSBaseVehicle`** (`src/microsim/MSBaseVehicle.h/.cpp`) — the common microsim implementation shared by `MSVehicle` (car-following/lane-based) and meso vehicles. Owns: parameters (`myParameter`), the route (`myRoute`, `myCurrEdge` iterator into it), the type (`myType`), device list (`myDevices`, `MSVehicleDevice*`), departure/arrival bookkeeping, odometer, per-vehicle numeric id, route validity cache.
- **`MSVehicle`** (`src/microsim/MSVehicle.h/.cpp`, 2202/8473 lines — the largest class in the module) — the full microscopic vehicle: car-following state, lane-changing model, link-approach planning, driver state/impatience, collision handling, stopping, and TraCI "influencer" overrides.
- **`MSVehicleType`** (`src/microsim/MSVehicleType.h`) — immutable-ish (copy-on-write for vehicle-specific overrides) description of a vehicle class: length, minGap, max speed, car-following model instance (`MSCFModel`), lane-change model selector, emission class, capacities, etc. Multiple `MSVehicle`s normally share one `MSVehicleType*`.
- **`MSVehicleControl`** (`src/microsim/MSVehicleControl.h/.cpp`) — factory + registry + statistics keeper for all vehicles and vehicle types in the simulation; not itself a vehicle.

## What a vehicle owns (state)
From `MSBaseVehicle`:
- `SUMOVehicleParameter* myParameter` — parsed `<vehicle>`/`<flow>`/`<trip>` attributes (depart time/pos/speed/lane procedures, arrival procedures, color, line, stops list, etc.). Owned and deleted by the vehicle.
- `ConstMSRoutePtr myRoute` + `MSRouteIterator myCurrEdge` — the edge sequence and the vehicle's current position within it.
- `MSVehicleType* myType` — pointer to (usually shared) type; cloned into a vehicle-specific type when per-vehicle attributes are overridden (`isVehicleSpecific()`).
- `myChosenSpeedFactor`, `myDeparture`, `myDepartPos`, `myArrivalPos`, `myArrivalLane`, `myOdometer`, `myNumberReroutes`, `myRouteValidity` cache.
- `std::vector<MSVehicleDevice*> myDevices` — pluggable behaviors/output hooks (rerouting, tripinfo, battery, taxi, driver-state, etc.), each also registered into `myMoveReminders`.
- `MoveReminderCont myMoveReminders` — list of `(MSMoveReminder*, offset)` pairs the vehicle notifies as it moves (see `other`/`vehicle_removal` docs and `MSMoveReminder`).

From `MSVehicle` (adds, on top of the above):
- `MSLane* myLane` — current lane (nullptr before insertion / after removal).
- `State myState` (nested class: `myPos`, `mySpeed`, `myPosLat`, `myBackPos`, `myPreviousSpeed`) — the core physical state updated every step by the car-following/movement code.
- `MSCFModel::VehicleVariables* myCFVariables` — per-vehicle car-following model scratch state (headway memory, etc.), created via `type->getCarFollowModel().createVehicleVariables()`.
- `MSAbstractLaneChangeModel* myLaneChangeModel` — built in `initDevices()` from `myType->getLaneChangeModel()`.
- `myFurtherLanes` / `myFurtherLanesPosLat` — lanes partially occupied by a vehicle longer than one lane (multi-lane occupation bookkeeping), including bidirectional-lane partners.
- `myWaitingTime`, `myWaitingTimeCollector`, `myTimeLoss` — impatience/statistics accumulators.
- `myDriverState` (`MSDevice_DriverState*`), `myFrictionDevice` — optional cached device pointers for hot-path access.
- `Influencer* myInfluencer` — TraCI/external speed-and-lane override hooks (only allocated on demand via `getInfluencer()`).
- `mySignals` — turn-signal/brake-light bitmask; `myAcceleration`, `myAngle` — derived kinematic/visual state.

## Vehicle lifecycle
1. **Construction** — `MSVehicleControl::buildVehicle()` computes the chosen speed-deviation factor, `new MSVehicle(pars, route, type, speedFactor)`, then `initVehicle()` calls `built->initDevices()` (attaches devices, builds the lane-change model and CF variables) and `built->addStops(...)` (registers `<stop>` elements from the route/vehicle definition). Vehicle is *not* yet on the road; `myLane == nullptr`.
   ```
   Source: src/microsim/MSVehicleControl.cpp (MSVehicleControl::buildVehicle, ::initVehicle)
   ```
2. **Registration** — `MSVehicleControl::addVehicle(id, v)` inserts into `myVehicleDict`, registers depart-triggered departure hooks (`handleTriggeredDepart`), and tracks public-transport vehicles. `myLoadedVehNo` was already incremented in `initVehicle`.
3. **Queued for departure** — `MSInsertionControl::add(veh)` (for `GIVEN`/`BEGIN` depart procedures) puts it into the time-sorted `MSVehicleContainer myAllVeh`. See `vehicle_insertion.md`.
4. **Insertion onto the network** — `MSEdge::insertVehicle()` (called from `MSInsertionControl::tryInsert`) picks a lane, position, and speed per the depart* attributes and calls lane-level insertion; on success the vehicle's `myLane`, `myState` are set, `onDepart()`/`vehicleDeparted()` fire, and `MSMoveReminder::NOTIFICATION_DEPARTED` is sent to move reminders.
5. **Per-step update** — while `isOnRoad()`, the vehicle participates in `MSEdgeControl::planMovements` → `executeMovements` → `changeLanes` each step (see `simulation_engine.md`); its `MSVehicle::State` is advanced by the car-following model, and move reminders are notified (`notifyMove`).
6. **Arrival / removal** — see `vehicle_removal.md`. Ends with `MSVehicleControl::scheduleVehicleRemoval()` (deferred; actual deletion happens in `removePending()`), which fires device output generation (tripinfo etc.) before `deleteVehicle()`/`deleteKeptVehicle()`.
7. **Destruction** — `~MSVehicle()` cleans up parking reservations, further-lane occupation, lane-change model, vehicle-specific type deregistration; `~MSBaseVehicle()` deletes devices, energy params, and the `SUMOVehicleParameter`.

## MSVehicleControl's role
- **Factory**: `buildVehicle()` / `initVehicle()` are the only path to creating a live vehicle object; also builds/owns default vehicle *types* (`initDefaultTypes()` creates `DEFAULT_VTYPE_ID`, `DEFAULT_PEDTYPE_ID`, `DEFAULT_CONTAINERTYPE_ID`) and vehicle-type distributions.
- **Registry**: `myVehicleDict` (id → `SUMOVehicle*`) is the canonical lookup used by TraCI, output modules, and route handling (`getVehicle(id)`).
- **Deferred deletion queue**: `scheduleVehicleRemoval()` pushes onto `myPendingRemovals`; `removePending()` (called once per step from `MSNet::simulationStep`) sorts them deterministically by id, fires per-device `generateOutput()` (tripinfo etc.), then either `deleteVehicle()` immediately or `deleteKeptVehicle()` (schedules an `MSEventControl` command to delete after `myKeepTime`, used so TraCI/GUI can still query a just-arrived vehicle for a grace period).
- **Fleet statistics**: running/loaded/ended/discarded counts, collision and teleport counters (by cause: collision, jam, yield, wrong-lane), emergency-stop/braking counts, total departure delay, total travel time, max speed factor / min deceleration (used e.g. for junction/braking-distance safety margins across the whole fleet).
- **State I/O**: `saveState()`/`setState()`/`clearState()` support simulation checkpointing (see `other_simulation_behaviour.md`).

```
Source:
src/microsim/MSVehicleControl.cpp (buildVehicle ~109, initVehicle ~120, scheduleVehicleRemoval ~136, removePending ~155, deleteVehicle ~367, vehicleDeparted ~200)
```

## Dependencies
`MSVehicleType`/`MSCFModel` (car-following), `MSAbstractLaneChangeModel`, `MSRoute`, `MSEdge`/`MSLane` (insertion & movement), `MSMoveReminder` subclasses (detectors/devices), `MSVehicleDevice` subclasses (`microsim/devices/*`), `MSVehicleTransfer` (teleport), `MSNet` (singleton access to controls), `libsumo` (TraCI influencer hooks).

## Consumers
`MSEdgeControl`/`MSLane` (movement), `MSInsertionControl` (departure), `MSVehicleTransfer` (teleport), output modules (tripinfo, vehroute, summary), TraCI/libsumo, GUI rendering.

## Behaviour / Edge cases
- A vehicle can be **vehicle-specific typed**: if per-vehicle attributes differ from its named type, a private clone of `MSVehicleType` is created (`isVehicleSpecific()`); `MSVehicleControl::removeVType` cleans this up on vehicle destruction.
- **Flows** generate vehicles with ids `"<flowId>.<index>"`; `MSBaseVehicle::getFlowID()` derives the flow id by stripping the last `.`-suffix, used to skip route-removal bookkeeping for flow members before the flow itself finishes.
- **hasArrived()** is purely route-position-based: `succEdge(1) == nullptr`, i.e., there is no next edge after the current one — independent of physical position on the last edge (see `vehicle_removal.md` for how arrival position/lane refine this).
- Devices are attached generically through `MSDevice::buildVehicleDevices` and treated uniformly as move reminders — this is the main "pluggable behavior" extension point for vehicles (batteries, rerouting, taxi service, driver state/imperfection, friction/weather).

## Tests
No `MSVehicle`/`MSVehicleControl`-specific unit tests found under `unittest/src/microsim`; only car-following model unit tests (`MSCFModelTest.cpp`, `MSCFModel_IDMTest.cpp`) exercise vehicle-adjacent logic directly. Vehicle lifecycle correctness is otherwise validated by the large scenario-based functional test suite in `tests/sumo`.

## Modification Points
- New vehicle-level behavior is best added as an `MSVehicleDevice` (registered as a move reminder) rather than modifying `MSVehicle` directly, matching the existing extension pattern.
- New fleet-wide statistics belong in `MSVehicleControl` alongside the existing counters, with `vehicleDeparted()`/`removePending()` as the natural update points.
- Changing vehicle physical state representation would require touching `MSVehicle::State` and every car-following/lane-changing call site — a large, invasive change.

## Confidence
High for structural claims (class layout, lifecycle sequence) — read directly from constructors/destructors and the `MSVehicleControl` implementation. Medium for the intent behind some device/idle interactions (e.g., emission handling in `initDevices`) — inferred from surrounding code rather than doc comments.
