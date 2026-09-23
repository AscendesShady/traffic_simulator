# Public Transport and Transportable Travel Plans

## Purpose

Models how persons and containers ("transportables") move through the network via a sequence of discrete travel stages, including riding public/private vehicles (buses, trains, taxis, private cars) and waiting at/boarding from stops. This is the layer that connects the pedestrian system (walking stages) to the vehicle simulation (driving stages), and implements schedule-following public transport (lines, stops, capacities).

## Responsibilities

- Represent a transportable's entire journey as an ordered plan of `MSStage` objects and drive progression through it (`MSTransportable::proceed`).
- Resolve abstract "trip" requests (`MSStageTrip`, from a `<personTrip>`) into concrete walk/ride stages via the intermodal router, at the moment they are needed (routing may be deferred to parallel worker threads).
- Manage riding a vehicle (`MSStageDriving`): waiting for a matching line/vehicle, boarding, being carried, alighting at the right stop/position.
- Manage stopping-place semantics shared with public transport: capacity, waiting positions, access points (e.g., platform-to-train-door access for railways).
- Support taxi/ride-hailing as a variant of "driving" via reservations (`MSDevice_Taxi`).

## Inputs

- `SUMOVehicleParameter` / plan XML elements: `<person>`/`<container>` with `<walk>`, `<ride>`/`<transport>`, `<stop>`, `<personTrip>`/`<transportTrip>`.
- Network stopping places (`MSStoppingPlace`, bus/train/container stops) and their `lines` attribute (advertised line names).
- Running vehicles' schedules (their own `MSStop` list, `line` parameter, `until`/`triggered` departure).
- Intermodal router (`MSTransportableRouter`, via `MSNet::getIntermodalRouter`) for trip resolution.

## Outputs

- Stage transitions recorded for output: `tripinfo`, `personinfo`, `vehroute`/`personroute` XML (via `MSStage::tripInfoOutput`/`routeOutput`).
- Vehicle occupancy changes (`SUMOVehicle::addTransportable`/`removeTransportable`).
- Reservations for on-demand transport (`MSDevice_Taxi::addReservation`).

## State

- `MSTransportable::myPlan` (`vector<MSStage*>`) plus `myStep` iterator marking the current stage — the entire "state machine position" for a transportable.
- Per-`MSStageDriving` waiting state: `myVehicle` (nullptr while waiting), `myWaitingSince`, `myWaitingEdge`, `myWaitingPos`, `myOriginStop`.
- `MSTransportableControl::myWaiting4Vehicle` (map `MSEdge* -> vector<MSTransportable*>`) — who is currently waiting where for a ride; consulted by `loadAnyWaiting` whenever a vehicle stops.
- `MSStoppingPlace::myWaitingTransportables` — who is occupying/queued at a stop (capacity-checked via `hasSpaceForTransportable`/`checkPersonCapacity`).
- `MSStop::timeToBoardNextPerson` / `timeToLoadNextContainer` on the vehicle side — throttles boarding rate to model per-passenger loading time (`MSStop.h:81,83`).

## Dependencies

- `microsim/transportables/MSStage*`, `MSTransportable*`, `MSPerson` (this subsystem).
- `microsim/MSVehicle` (stop handling, boarding: `MSVehicle::boardTransportables`, `src/microsim/MSVehicle.cpp:1991`).
- `microsim/MSStoppingPlace`, `microsim/MSStop` (stop/schedule data).
- `microsim/devices/MSDevice_Taxi`, `MSDevice_Transportable`, `MSDevice_Tripinfo` (auxiliary devices hooking into stage lifecycle).
- `utils/router/IntermodalRouter` / `PedestrianRouter` (routing engine used by `MSStageTrip::reroute`).
- Pedestrian system (`MSPModel*`) for the walking portions of a plan.

## Consumers

- `MSInsertionControl` / departure logic inserts the initial `WAITING_FOR_DEPART` stage transportable into the simulation.
- GUI / TraCI read stage state (`getCurrentStageType`, `isWaitingFor`, `getVehicle`) to visualize and control transportables.
- Statistics/output devices (`MSDevice_Tripinfo`, vehroute-output) summarize completed stages.

## Execution

Each simulation step, `MSNet` processes transportables whose current stage has ended or who are event-driven (waiting-until commands, routing commands). The core loop is `MSTransportable::proceed` (`src/microsim/transportables/MSTransportable.cpp:102`):

1. Calls `prior->setArrived(...)` to close out the just-finished stage (records timing/distance).
2. Removes the transportable from the prior edge's occupancy tracking.
3. Advances `myStep` to the next stage.
4. Determines whether an implicit `ACCESS` stage is needed (e.g., stop's platform is on a different edge than the next stage's edge) via `checkAccess` (implemented on `MSPerson`, not shown here in full — invoked from `MSTransportable::proceed`).
5. Calls `(*myStep)->proceed(net, this, time, prior)` — each concrete stage's `proceed` sets up its own internal state (e.g., `MSStageDriving::proceed` computes where/what to wait for; `MSStageTrip::proceed` triggers routing).
6. If `myStep == myPlan->end()`, the transportable has arrived: registers with `MSTransportableControl::addArrived()` and returns `false`, signalling the caller to erase it.

`MSStageTrip` is special: it does not represent physical movement. Its `proceed` calls the intermodal router (`reroute`, `src/microsim/transportables/MSStageTrip.cpp:184`) which produces a list of concrete stages (walks/rides) and **inserts them into the plan** right after the trip stage (`transportable->appendStage(stage, idx++)` in `setArrived`, `MSStageTrip.cpp:395-398`), so by the time `proceed` finishes, the trip has "expanded" into real stages. Routing may run inline or (with `HAVE_FOX`/thread pool) asynchronously on a worker thread (`TripRoutingTask`), with completion polled via `MSTransportableControl::processPendingRouting`.

## Important Classes

| Class | File | Role |
|---|---|---|
| `MSTransportable` | `MSTransportable.h/.cpp` | Base for persons/containers; owns the plan and drives `proceed`. |
| `MSStage` | `MSStage.h/.cpp` | Abstract stage interface: destination, timing, position/angle accessors, output methods. `MSStageType` enum: `WAITING_FOR_DEPART, WAITING, WALKING, DRIVING, ACCESS, TRIP, TRANSHIP`. |
| `MSStageWaiting` | `MSStageWaiting.h/.cpp` | Stationary stage — initial depart placeholder or a planned `<stop>`; tracks `myWaitingDuration`/`myWaitingUntil`. |
| `MSStageTrip` | `MSStageTrip.h/.cpp` | Unresolved intermodal request; expands into concrete stages at `proceed`/`setArrived` time via the router. |
| `MSStageMoving` | `MSStageMoving.h/.cpp` | Shared base for stages driven by a pedestrian-style movement model (walking, tranship); delegates per-step motion through `MSTransportableStateAdapter`/`MSPModel`. |
| `MSStageWalking` | `MSStageWalking.h/.cpp` | Concrete walking stage over an edge route; owns move reminders, exit-time tracking. |
| `MSStageTranship` | `MSStageTranship.h/.cpp` | Container analogue of walking (movement of goods between points, always via `MSPModel_NonInteracting`). |
| `MSStageDriving` | `MSStageDriving.h/.cpp` | Riding a vehicle: waiting for a matching line, boarding, tracking distance/time-loss while aboard, alighting. |
| `MSPerson::MSPersonStage_Access` | `MSPerson.h` | Implicit stage bridging a stop's platform access point and the edge a walk/ride actually starts/ends on. |
| `MSTransportableControl` | `MSTransportableControl.h/.cpp` | Registry of all live transportables of one kind (person or container); owns `myWaiting4Vehicle`, statistics counters, and the movement-model instances. |
| `MSStoppingPlace` | `MSStoppingPlace.h/.cpp` | Bus/train/container stop: capacity, waiting positions, `Access` points (with `AccessExit`: PLATFORM/DOORS/CARRIAGE). |
| `MSStop` | `MSStop.h` | A vehicle-side scheduled stop entry: timing, `line`, `triggered`/`containerTriggered`, per-passenger boarding throttle fields. |
| `MSDevice_Taxi` | `devices/MSDevice_Taxi.h/.cpp` | Reservation-based dispatch for taxi/ride-hail "lines"; `MSStageDriving` defers to it when `myLines` names a taxi service. |

## Important Functions

- `MSTransportable::proceed` (`MSTransportable.cpp:102`) — the stage-advance state machine described above.
- `MSStageDriving::proceed` (`MSStageDriving.cpp:226`) — computes `myWaitingEdge`/`myWaitingPos` from the origin stop or previous stage, checks for an already-triggered vehicle waiting at the spot, else calls `registerWaiting`.
- `MSStageDriving::registerWaiting` (`MSStageDriving.cpp:302`) — books a taxi reservation if applicable, else registers the transportable in `MSTransportableControl::addWaiting` and on the edge.
- `MSStageDriving::isWaitingFor` (`MSStageDriving.cpp:437`) — line-matching predicate: matches by vehicle ID, by `line` parameter (or the wildcard `LINE_ANY`), additionally verifying the vehicle actually stops at the destination edge/stop, or via taxi-compatible line matching.
- `MSTransportableControl::loadAnyWaiting` (`MSTransportableControl.cpp:320`) — called every time a vehicle is stopped; scans transportables waiting on that edge, boards everyone whose `isWaitingFor(vehicle)` matches and who fits within the stop-tolerance position window, applying a per-passenger `loadingDuration` that can extend the vehicle's stop.
- `MSVehicle::boardTransportables` (`MSVehicle.cpp:1991`) — vehicle-side entry point invoked while stopped; calls `loadAnyWaiting` for both persons and containers and updates `stop.triggered` bookkeeping once boarding deadline (`stop.endBoarding`) passes.
- `MSStageDriving::canLeaveVehicle` (`MSStageDriving.cpp:604`) — decides whether a riding transportable may alight at the vehicle's current stop, checking arrival position tolerance and destination-stop access-edge tolerance.
- `MSStageTrip::reroute` (`MSStageTrip.cpp:184`) — builds candidate vehicles per allowed mode (car/taxi/bike), calls the intermodal router once per candidate, keeps the minimum-cost result, and materializes the winning `TripItem` sequence into `MSStageWalking`/`MSStageDriving` objects.
- `MSStageDriving::setArrived` (`MSStageDriving.cpp:485`) — on alighting, computes accumulated distance/time-loss and, for stops with DOORS/CARRIAGE access (rail platforms), randomizes/derives an exact door-based alighting position using `MSTrainHelper`.

## Behaviour

- A transportable's plan always starts with a `WAITING_FOR_DEPART` stage (`MSStageWaiting` constructed with `initial=true`), even if the person departs immediately — this stage never contributes to reported duration (`MSStageWaiting::getDuration` returns 0 for it, `MSStageWaiting.cpp:75-77`).
- Line matching accepts a wildcard `LINE_ANY` (used when the router determines "any vehicle serving this route will do" — typically after taxi/public-line routing when `persontrip.ride-public-line` is false, `MSStageTrip.cpp:319`).
- Waiting-for-vehicle capacity is enforced at boarding: the triggered-departure
  special case can warn about capacity, while regular boarding calls
  `MSBaseVehicle::allowsBoarding()`. That method rejects exceeded person or
  container capacity, enforces an optional permitted-ID set, and delegates
  taxi-specific acceptance to `MSDevice_Taxi`.
- Access stages bridge a mismatch between where a stage physically starts/ends and a stop's platform edge (e.g., rail platforms with `DOORS`/`CARRIAGE` access types) — see `MSTransportable::proceed`'s `checkAccess` calls and `MSPerson::MSPersonStage_Access`.
- Rerouting mid-trip (`MSTransportable::reroute`, `MSTransportable.cpp:383`) is supported only for stages that originated from an `MSStageTrip` and only while not already riding a vehicle; it re-invokes the router from the current position and splices in the new stage sequence, removing now-obsolete future stages of the same trip.
- Parking-area rerouting (`MSTransportable::rerouteParkingArea`, `MSTransportable.cpp:522`) patches a person's plan in place when the vehicle they are riding gets redirected to a different parking area than originally planned, rewriting the ride's destination and any subsequent walk/trip stage's origin.

## Edge Cases

- **No connection found by router**: `MSStageTrip::reroute` falls back to inserting a raw walking stage across the full origin-to-destination gap so the GUI/state stays consistent, then returns an error string; if `MSGlobals::gCheckRoutes` is set this becomes a hard error, otherwise the pedestrian silently "teleports" via an unrealistic direct walk (`MSStageTrip.cpp:347-355`).
- **Vehicle never arrives / long wait**: `MSTransportable::setAbortWaiting`/`abortStage` schedule a forced teleport after `time-to-teleport.ride` (`MSTransportableControl::myAbortWaitingTimeout`), counted via `registerTeleportAbortWait`.
- **Triggered departure as first real stage**: `MSStageDriving::proceed` special-cases stage index 1 (right after `WAITING_FOR_DEPART`) with `departProcedure == TRIGGERED`, looking up the named vehicle directly rather than waiting for it to arrive at an edge (`MSStageDriving.cpp:232-266`).
- **Taxi/reservation edge permission mismatch**: if the waiting edge or destination edge doesn't allow `SVC_TAXI`, `registerWaiting` walks the stop's access points looking for one that does (`MSStageDriving.cpp:305-330`).
- **State save/load**: `MSStageDriving::saveState`/`loadState` persist whether a vehicle was already assigned, the waiting spot index, and stop wait position, so a resumed simulation reconstructs exact boarding state (`MSStageDriving.cpp:640-689`).
- **Same-stop trip with zero distance**: `MSStageTrip::reroute` special-cases origin stop == destination stop by emitting a zero-duration `MSStageWaiting` rather than a degenerate walk (`MSStageTrip.cpp:356-366`).

## Tests

Verified test roots include `tests/sumo/basic/person/`,
`tests/sumo/basic/personFlow/`, `tests/sumo/basic/container/`,
`tests/sumo/devices/taxi/`, `tests/sumo/devices/transportable/`, and person/PT
output suites under `tests/sumo/output/`. Remote-control behavior is covered by
`tests/complex/traci/person/`, `tests/complex/traci/busstop/`, and vehicle-domain
scenarios.

## Modification Points

- New transport modes are added by extending `MSStageTrip::getVehicles`/`reroute`'s mode-set handling (`SVC_PASSENGER`/`SVC_TAXI`/`SVC_BICYCLE`/`SVC_BUS` branches, `MSStageTrip.cpp:133-153`) plus a corresponding `MSStage` subclass if the mode isn't already covered by walk/drive.
- Boarding throttling/capacity policy is centralized in `MSTransportableControl::loadAnyWaiting` and `MSVehicle::boardTransportables` — the natural place to change how multiple passengers board per stop-step.
- Line-matching semantics (what counts as "the vehicle I'm waiting for") is entirely inside `MSStageDriving::isWaitingFor` — a single well-isolated predicate.

## Confidence

High for the stage state machine, boarding/alighting, capacity checks, and
access-stage insertion, read directly from `MSStageDriving`, `MSBaseVehicle`,
and `MSPerson`. Medium for exhaustive assertion-level test coverage and
optional taxi/rail variants.
