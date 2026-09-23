# Routing Subsystem

## Purpose

SUMO separates "routing" (computing a sequence of edges between an origin and destination under some
cost function) from two different callers:

1. **Offline routing tools** (`duarouter`, `od2trips`, `marouter`, `dfrouter`, `jtrrouter`) — batch-compute
   routes for a whole demand file before simulation starts, using the framework in `src/router/`.
2. **In-simulation dynamic (re)routing** — `sumo`/`libsumo` itself calling into the same generic
   template routers (`src/utils/router/`) at runtime, using live edge-speed measurements, to let vehicles,
   pedestrians, and transportables react to congestion, TraCI commands, rerouter edges, parking/charging
   station search, taxi dispatch, etc.

Both consumers share the *same* templated shortest-path algorithm implementations in `src/utils/router/`;
they differ only in which `E` (edge) / `V` (vehicle) template types they instantiate them with
(`MSEdge`/`SUMOVehicle` for simulation, `ROEdge`/`ROVehicle` for offline tools) and in how edge costs are
supplied (live simulation state vs. static/loaded weight files).

Source:
```
src/utils/router/SUMOAbstractRouter.h
src/microsim/MSRouterDefs.h
src/router/RORoutable.h (ROIntermodalRouter typedef)
```

## Responsibilities

- Provide shortest/cheapest-path computation between two edges (or edge+position pairs) for a given
  vehicle/vehicle-class at a given simulation time.
- Track and update time-dependent edge costs (travel time or generic "effort") used as routing input.
- Trigger rerouting of already-departed vehicles/persons when: a periodic timer elapses, a vehicle
  enters a designated "rerouter" edge, a stop/parking search fails, a TraCI `traci.vehicle.rerouteXxx`
  call is issued, or a rail signal forces an alternate path.
- Support multi-modal routing (car, pedestrian, public transport, intermodal person trips) via router
  wrappers built on the same abstract interface.

Source:
```
src/microsim/devices/MSDevice_Routing.h/.cpp
src/microsim/devices/MSRoutingEngine.h/.cpp
src/microsim/trigger/MSTriggeredRerouter.h/.cpp
src/microsim/MSEdgeWeightsStorage.h/.cpp
```

## Inputs

- **Network topology**: `MSEdge`/`MSLane`/`MSJunction` graph (simulation) or `ROEdge`/`RONode` graph
  (offline, built by `RONetHandler` from the `.net.xml`).
- **Demand**: parsed `<trip>`, `<flow>`, `<route>`/`<vehicle>` (with optional route alternatives) elements
  — see "Demand model" below.
- **Edge costs**: either
  - live simulation measurements (`MSEdge::getMeanSpeed()`, aggregated into `MSRoutingEngine`'s per-edge
    speed arrays), or
  - explicit weight files loaded via `-weight-files`/`--lane-weight-files` into `MSEdgeWeightsStorage`
    (simulation) / `ROEdge` static effort maps (offline), or
  - static edge attributes (`speed`, `length`) when no dynamic data exists.
- Router configuration options: algorithm choice (`--routing-algorithm`), permissions/restrictions,
  `weight-attribute` (traveltime, CO2, fuel, noise, etc.).

## Outputs

- A `std::vector<const E*>` of edges forming the computed path (returned via `SUMOAbstractRouter::compute`).
- For offline tools: `RORoute`/`RORouteDef` objects written back out as `<route>` elements (with or
  without route alternatives, exit times, costs).
- For simulation: a new `MSRoute` assigned to the vehicle via `MSBaseVehicle::replaceRoute`, changing its
  remaining edge sequence without restarting the vehicle.

## State

- `SUMOAbstractRouter::myEdgeInfos` — per-edge scratch state (effort, heuristic effort, previous edge,
  visited flag) reused across queries (`init()` resets only touched entries for performance).
- `MSEdgeWeightsStorage` — a `std::map<const MSEdge*, ValueTimeLine<double>>` per edge for travel time and
  effort, populated by TraCI (`traci.edge.setEffort/setTravelTime`) or `duarouter`/simulation weight files.
- `MSRoutingEngine` static state: `myEdgeSpeeds`/`myEdgeBikeSpeeds` (current smoothed speed per edge id),
  `myPastEdgeSpeeds` (ring buffer for moving average), `myCachedRoutes` (map from origin/destination edge
  pair to a cached `MSRoute`, cleared every adaptation interval), `myAdaptationInterval`/`myAdaptationWeight`
  controlling the smoothing of live speeds into routing costs.

Source:
```
src/microsim/devices/MSRoutingEngine.cpp (myEdgeSpeeds, myCachedRoutes, adaptEdgeEfforts)
src/microsim/MSEdgeWeightsStorage.h
```

## Dependencies

- `src/utils/router/*` generic algorithm templates (Dijkstra/A*/CH/etc.) — the actual pathfinding.
- `MSNet`, `MSEdgeControl` (list of all `MSEdge`s), `MSEventControl` (scheduling periodic reroute commands
  as `WrappingCommand`/`StaticCommand`).
- `MSVehicle`/`MSBaseVehicle`/`MSTransportable` — objects that own a current route and call into routers.
- FOX thread pool (`HAVE_FOX`) for optional multi-threaded rerouting (`device.rerouting.threads`).

## Consumers

- `MSDevice_Routing` (per-vehicle periodic/pre-insertion reroute device, "rerouting" device).
- `MSDevice_Transportable`/`MSTransportableDevice_Routing`, `MSStageWalking`, `MSStageTrip` (pedestrian /
  intermodal person routing).
- `MSTriggeredRerouter` (edge-based rerouters defined in additional files, e.g. incident response,
  closingLane, parking-area rerouting via `MSParkingArea`/`MSStoppingPlaceRerouter`).
- `MSDevice_Taxi` (dispatch routing), `MSDevice_StationFinder` (charging/parking search rerouting).
- `MSRailSignal` (rail-specific rerouting to resolve conflicts, `device.rerouting.railsignal`).
- TraCI/libsumo `vehicle.rerouteTraveltime`/`rerouteEffort` API calls.
- Offline tools: `duarouter`, `marouter`, `od2trips` link against `src/router/` which itself wraps
  `src/utils/router/` routers via `RouterProvider`.

## Execution

**Simulation (dynamic) path**:
1. `MSDevice_Routing::buildVehicleDevices` attaches a routing device to vehicles/flows equipped via
   `--device.rerouting.probability` or an explicit `VEHPARS_FORCE_REROUTE` (i.e., `<trip>`/`<flow>` always
   get one; `<vehicle>`+`<route>` only if force-rerouted or randomly equipped).
2. On insertion (`notifyEnter` with `NOTIFICATION_DEPARTED`), a repeating `WrappingCommand` is scheduled
   every `device.rerouting.period` seconds via
   `MSNet::getBeginOfTimestepEvents()`. A one-shot
   `preInsertionReroute` command may also run before departure (`device.rerouting.pre-period`) so that
   `departLane="best"` has meaningful data.
3. `MSRoutingEngine::adaptEdgeEfforts` runs periodically (`device.rerouting.adaptation-interval`, default
   1s) as an end-of-timestep event, updating `myEdgeSpeeds` from `MSEdge::getMeanSpeed()` for every "delayed"
   edge, either via exponential or windowed moving average, and clears the route cache.
4. `MSDevice_Routing::reroute()` skips recomputation if edge weights haven't changed since the vehicle's
   last routing attempt (`myLastRouting >= MSRoutingEngine::getLastAdaptation()`), then calls
   `MSRoutingEngine::reroute(vehicle, ...)`, which picks the router (`getRouterTT`), calls
   `router.compute(...)`, and — if a cheaper route is found beyond `device.rerouting.threshold.factor`/
   `.constant` (see `sufficientSaving`) — calls `vehicle.replaceRoute(...)`.
5. `MSTriggeredRerouter` uses `MSMoveReminder::notifyEnter` on rerouter edges to intercept vehicles and
   either assign a route from a probability-weighted closed set, or invoke the router with a new
   destination (e.g. closingReroute/parkingAreaReroute/overtakeReroute logic in
   `MSTriggeredRerouter.cpp`).

**Offline (duarouter) path**: see `docs/reverse_engineering/tools/duarouter.md`.

## Important Classes

| Class | File | Role |
|---|---|---|
| `SUMOAbstractRouter<E,V>` | `src/utils/router/SUMOAbstractRouter.h` | Common interface + shared bookkeeping (EdgeInfo heap state, prohibitions, query stats) for all routing algorithms. |
| `DijkstraRouter<E,V>` | `src/utils/router/DijkstraRouter.h` | Label-setting Dijkstra over `getViaSuccessors`. |
| `AStarRouter<E,V,M>` | `src/utils/router/AStarRouter.h` | A* with Euclidean-distance-at-max-speed heuristic, optionally landmark- or full-distance lookup tables. |
| `CHRouter<E,V>` / `CHRouterWrapper<E,V>` | `src/utils/router/CHRouter.h`, `CHRouterWrapper.h` | Contraction-hierarchy bidirectional search; wrapper falls back to Dijkstra/CH per-vehicle-class when permissions/preferences make static contraction invalid. |
| `AFRouter` | `src/utils/router/AFRouter.h` | Arc-flags routing using a `KDTreePartition`. |
| `RailwayRouter<E,V>` | `src/utils/router/RailwayRouter.h` | Rail-specific router handling bidirectional track use and reversal penalties. |
| `PedestrianRouter`, `IntermodalRouter` | `PedestrianRouter.h`, `IntermodalRouter.h` | Multi-modal graphs built from `IntermodalNetwork`/`IntermodalEdge` wrapping the base edge graph plus walking/transfer edges. |
| `RouterProvider<E,L,N,V>` | `RouterProvider.h` | Bundles vehicle/pedestrian/intermodal/railway routers for a given run; clones per-thread. |
| `MSRoutingEngine` | `src/microsim/devices/MSRoutingEngine.h/.cpp` | Simulation-side singleton: maintains live edge speeds, builds/holds router instances (`getRouterTT`), route cache, adaptation loop. |
| `MSDevice_Routing` | `src/microsim/devices/MSDevice_Routing.h/.cpp` | Per-vehicle device scheduling periodic/pre-insertion reroute events. |
| `MSTriggeredRerouter` | `src/microsim/trigger/MSTriggeredRerouter.h/.cpp` | Edge-triggered rerouting (closures, parking search, rail overtaking). |
| `MSEdgeWeightsStorage` | `src/microsim/MSEdgeWeightsStorage.h/.cpp` | Time-indexed travel-time/effort storage per edge, used by TraCI-set weights and `--weight-files`. |
| `RONet`, `ROEdge`, `RORouteDef`, `RORouteHandler`, `ROVehicle`, `ROLoader` | `src/router/*` | Offline framework: network representation, route/alternatives model, demand-file parsing, driving the router over a whole demand file. |

## Important Functions

- `SUMOAbstractRouter::compute(from, to, vehicle, msTime, into, silent)` — abstract entry point implemented
  by each algorithm (`src/utils/router/SUMOAbstractRouter.h:198`).
- `SUMOAbstractRouter::getEffort` / `getTravelTime` — apply the configured `Operation` function pointer
  (edge cost function) plus temporary-prohibition adjustment (`SUMOAbstractRouter.h:378,258`).
- `MSRoutingEngine::getEffort(edge, vehicle, t)` — default simulation cost function: `length / max(currentSpeed, eps)`
  clamped to the edge's minimum travel time (`MSRoutingEngine.cpp:184`).
- `MSRoutingEngine::adaptEdgeEfforts` — periodic smoothing of live speeds into routing costs
  (`MSRoutingEngine.cpp:220`).
- `MSDevice_Routing::reroute` / `MSDevice_Routing::sufficientSaving` — gate on stale weights and on
  improvement threshold before swapping routes (`MSDevice_Routing.cpp:300,312`).
- `MSBaseVehicle::replaceRoute` (declared in `MSBaseVehicle.h`, used throughout the reroute call sites) —
  swaps in a new `MSRoute` for a vehicle already in the network.
- `RORouteDef::buildCurrentRoute` / `preComputeCurrentRoute` / `addAlternative` — offline equivalent:
  build or select a route, optionally maintaining multiple alternatives with probabilities
  (`src/router/RORouteDef.h:83,88,101`).

## Behaviour

- Routing is always edge-to-edge; internal (junction) edges are skipped over via
  `getViaSuccessors`/`updateViaEdgeCost`, whose cost is folded into the edge-to-edge transition cost rather
  than treated as separate hops in the returned path.
- `SUMOAbstractRouter` supports "bulk mode" / "auto bulk mode": if consecutive queries share the same
  `(from, vehicle, msTime)` key, the frontier already explored is reused instead of restarting Dijkstra,
  an optimization for computing many destinations from one source (e.g., turn-by-turn OD matrices).
- Live rerouting is throttled twice: globally by the `device.rerouting.adaptation-interval` (how often
  edge weights refresh) and per-vehicle by `device.rerouting.period` plus the "no route change unless
  cheaper by `threshold.factor`/`threshold.constant`" rule — this avoids oscillating/flapping routes.
- Route replacement does not restart or re-insert the vehicle; only the remaining (not-yet-driven) part of
  the edge list is affected — vehicles keep their position/lane/speed state.
- `MSRoutingEngine::myCachedRoutes` caches OD-edge-pair routes and is invalidated every adaptation interval,
  so multiple vehicles rerouted between two full weight updates share one computed path when using TAZ
  connectors.

## Edge Cases

- Prohibited/forbidden edges (`RouterProhibition`, vehicle-class permissions/restrictions) are checked both
  before starting a query (`from`/`to` themselves) and per-edge during expansion (`isProhibited`), with a
  time window (`prohibitionBegin`/`prohibitionEnd`) allowing an edge to become passable again mid-query.
- No-route-found: `compute` returns `false` and (unless `silent`) reports via `MsgHandler` — the offline
  router either ignores it (`--ignore-errors`) or aborts.
- Looped routes (from==to but the vehicle should traverse a loop, e.g. taxi/return trips) are special-cased
  in `computeLooped`, trying each via-successor of the start edge.
- Rerouting while a vehicle is stopped is deferred: `MSDevice_Routing::reroute` sets `myRerouteAfterStop`
  and only reroutes in `notifyStopEnded`.
- `CHRouter` requires no permissions/speed-restrictions/preferences on the network (falls back to
  `CHRouterWrapper`, which is slower but per-vClass-correct, otherwise).
- A* heuristic requires an actual geographic distance and speed cap; it is only admissible for the
  travel-time measure — a comment in `AStarRouter.h` states "for routing by effort a novel heuristic would
  be needed", i.e. non-time efforts should not use A*.

## Tests

- `tests/duarouter/*` (e.g. `alternatives`, `dua`, `logit`, `closingLane`, `trips`, `vclasses`,
  `personFlow`) — end-to-end textual regression tests comparing generated route files for various routing
  algorithms/options, run through the standard SUMO test harness (`testsuite.duarouter`).
- No dedicated GoogleTest unittest directory for `src/utils/router` was found under `unittest/` at the time
  of this review (searched `unittest/src` for `*router*`, no matches) — algorithm correctness is validated
  primarily via the black-box `tests/duarouter` and `tests/sumo` suites.

Confidence: Medium — inferred from directory search; a router-specific unit test could exist elsewhere
under a name that doesn't match "router".

## Modification Points

- New/alternate cost functions: add an `Operation` function (edge, vehicle, time) -> double, matching
  `MSRoutingEngine::getEffort`/`ROEdge::getTravelTimeStatic` signatures, and wire it into
  `RODUAFrame`/`MSRoutingEngine::getRouterTT` selection logic.
- New shortest-path algorithm: subclass `SUMOAbstractRouter<E,V>`, implement `compute`/`clone`, and add a
  branch in `duarouter_main.cpp::computeRoutes` (`--routing-algorithm`) and/or
  `MSRoutingEngine::getRouterTT`.
- Adjusting reroute cadence/hysteresis: `device.rerouting.period`, `.threshold.factor/.constant`,
  `.adaptation-interval/.weight/.steps` options in `MSDevice_Routing::insertOptions`.
- Edge-triggered rerouting logic (incidents, parking, overtaking) lives entirely in
  `MSTriggeredRerouter.cpp` and is a natural extension point for new triggered-reroute behaviors.

## Confidence

High for algorithm inventory and the simulation reroute trigger chain (directly read from source). Medium
for some cross-file behavioral claims (e.g., exact taxi/rail-signal reroute call sites) which were located
by grep but not fully read line-by-line.
