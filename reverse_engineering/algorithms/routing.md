# Routing Algorithms

## Purpose

Compute a minimum-cost sequence of edges between two points in a directed multigraph (the road/rail/
pedestrian network), for a specific traffic participant (vehicle class, max speed, permissions), at a
specific point in simulated or scenario time, under a pluggable edge-cost function. A second, distinct
concept — demand assignment (how a whole population of trips converges toward an equilibrium) — is
layered on top by the `duarouter` tool and external iteration scripts, not by the shortest-path routers
themselves.

## Algorithms actually implemented (verified by reading headers/class bodies)

All of the following derive from `SUMOAbstractRouter<E,V>` (`src/utils/router/SUMOAbstractRouter.h`) and
are template classes parameterized on edge type `E` and vehicle type `V` (instantiated with
`MSEdge`/`SUMOVehicle` in simulation and `ROEdge`/`ROVehicle` offline):

| Name (string used in `--routing-algorithm`) | Class | File | Technique |
|---|---|---|---|
| `dijkstra` | `DijkstraRouter<E,V>` | `DijkstraRouter.h` | Classic label-setting Dijkstra with a binary heap (`std::push_heap`/`pop_heap`) over `EdgeInfo::effort`. |
| `astar` | `AStarRouter<E,V,M>` | `AStarRouter.h` | A* using `heuristicEffort = effort + heuristic_remaining`; heuristic is Euclidean distance from edge to target divided by an effective max speed, or a precomputed lower-bound lookup table. |
| — (lookup table for A*) | `FullLookupTable<E,V>` / `LandmarkLookupTable<E,V,M>` | `AStarLookupTable.h` | Precomputed all-pairs or landmark-based lower bounds loaded via `--astar.all-distances` / `--astar.landmark-distances`, built from forward+backward Dijkstra runs (`ReversedEdge`). |
| `CH` | `CHRouter<E,V>` | `CHRouter.h`, `CHBuilder.h` | Contraction Hierarchies: precomputed node ordering/shortcut edges (`CHBuilder`), bidirectional search meeting in the middle (`Unidirectional` forward/backward searches). Requires a homogeneous vehicle class (no per-vehicle permissions/speed restrictions) since the hierarchy is built once. |
| `CHWrapper` | `CHRouterWrapper<E,V>` | `CHRouterWrapper.h` | Wraps `CHRouter`, building/caching one hierarchy per distinct vehicle class/weight period and falling back so permissions and time-varying weight periods can still be honored. |
| `arcflag` | `AFRouter<E,N,V,M>` | `AFRouter.h`, `AFBuilder.h`, `AFCentralizedSPTree.h` | Arc-flags: partitions the network with a `KDTreePartition`, precomputes per-partition-boundary reachability flags per edge, then runs a flag-pruned Dijkstra. |
| (rail-specific, always used automatically for networks with bidirectional track) | `RailwayRouter<E,V>` | `RailwayRouter.h` | Wraps a base router (usually Dijkstra) adding train-length and bidirectional-track reversal-penalty handling (`weights.reversal-penalty`, `railway.max-train-length`). |
| (pedestrian) | `PedestrianRouter<E,L,N,V>` | `PedestrianRouter.h` | Dijkstra/A* over a `PedestrianEdge`-augmented graph limited to walkable lanes. |
| (multi-modal person trips) | `IntermodalRouter<E,L,N,V>` | `IntermodalRouter.h`, `IntermodalNetwork.h`, `IntermodalEdge.h` | Builds a combined graph of car/walk/public-transport/access edges (`IntermodalEdge`, `AccessEdge`, `PublicTransportEdge`, `StopEdge`) and runs the base shortest-path search over it; supports taxi/park-and-ride via `carWalk` transfer flags and `FareModul`/`FareZones`/`FareToken` fare-based cost extensions. |
| (utility) | `Node2EdgeRouter` | `Node2EdgeRouter.h` | Adapts a node-based query onto the edge-based router interface. |

Source (algorithm selection in the offline tool, confirming exact string identifiers and instantiation):
```
src/duarouter/duarouter_main.cpp (computeRoutes: "dijkstra", "astar", "CH", "CHWrapper", "arcflag")
```

Simulation-side algorithm selection uses the same classes; grep of `src/microsim/devices/MSRoutingEngine.cpp`
confirms `#include`s of `DijkstraRouter.h`, `AStarRouter.h`, `CHRouter.h`, `CHRouterWrapper.h`.

## Demand-assignment concept: one-shot vs. iterative DUE

- **`duarouter` itself performs one-shot, per-vehicle shortest-path routing** for a given weight snapshot:
  each vehicle/trip in the input demand is routed independently using the *current* static/loaded edge
  weights (`computeRoutes` in `duarouter_main.cpp`). It does not iterate to convergence internally.
- **Route-choice / probability calculators exist** (`RouteCostCalculator<R,E,V>` base,
  `GawronCalculator`, and — confirmed via `RODUAFrame.cpp` option registration — a `logit`/`lohse`
  `--route-choice-method`) which, given a *set* of route alternatives with prior costs/probabilities,
  update each alternative's probability and blended cost. `GawronCalculator::calculateProbabilities`
  implements exactly the logit-like pairwise update formula from Gawron (1998) "Simulation-Based Traffic
  Assignment" (docstring cites "Dynamic User Equilibria..."), see `src/utils/router/GawronCalculator.h`.
- **The actual iterative Dynamic User Equilibrium (DUE) loop is implemented outside the C++ router code**,
  in the Python script `tools/assign/duaIterate.py` (and variants `duaIterateMix.py`, `cadytsIterate.py`),
  which repeatedly: runs `duarouter`/`sumo` for one iteration, reads back the route-alternatives file
  (`.rou.alt.xml`) containing per-route costs/probabilities updated by the Gawron/logit calculator, and
  feeds it back as input for the next iteration until a convergence criterion (e.g. relative gap) is met.
  This confirms SUMO's "DUE" is a fixed-point iteration over repeated one-shot routing calls, with the
  cost-averaging/choice logic (Gawron/logit/lohse) implemented in C++ but the outer loop in Python.

Confidence: High for the C++ router/algorithm inventory (read directly). Medium — inferred for the precise
claim that no C++ code performs the outer iterate-until-convergence loop itself; based on grep results
(`tools/assign/duaIterate.py` found, no equivalent iterate loop found in `src/duarouter`) plus reading
`duarouter_main.cpp` in full (single pass: load net, load routes, `computeRoutes` once, exit).

## Inputs

- Graph: `E::getViaSuccessors(vClass[, ignoreTransient])` per edge, yielding `(nextEdge, viaEdge)` pairs
  (internal junction edges are threaded through as `via`).
- Vehicle: used for `getVClass()`, `getMaxSpeed()`, `ignoreTransientPermissions()`, `getChosenSpeedFactor()`.
- Time: `SUMOTime msTime`, the departure/query time, needed because edge cost functions are time-dependent.
- Cost `Operation` function pointer: `double(*)(const E*, const V*, double t)` — e.g.
  `ROEdge::getTravelTimeStatic`, `MSRoutingEngine::getEffort`, or emission/fuel/noise variants
  (`ROEdge::getEmissionEffort<PollutantsInterface::CO2>` etc., selected via `--weight-attribute`).
- Optional `Prohibitions` map (temporary, time-bounded edge closures) and static permissions/restrictions.

## Outputs

- `std::vector<const E*> into` — ordered edge path from `from` to `to` (inclusive), or unchanged/empty on
  failure with `compute` returning `false`.
- Cumulative `effort`/`time`/`length` recomputable afterward via `recomputeCosts`/`recomputeCostsPos`.

## State

Per router instance: `myEdgeInfos` (one `EdgeInfo` per edge index, holding `effort`, `heuristicEffort`,
`leaveTime`, `prev`, `visited`), `myFrontierList` (open set / heap), `myFound` (closed set, for reset).
`init()` performs the reset lazily by iterating only `myFrontierList`+`myFound` from the previous query,
not the whole edge array — an important performance property for large networks queried repeatedly.

`CHRouter` additionally holds a precomputed contraction order and shortcut edges (`CHBuilder`), built once
and reused across queries; `reset(vehicle)` exists specifically to invalidate CH's cached structures when
the routing context (vehicle class) changes.

## Preconditions

- `from != nullptr`; `to` may be null only for `DijkstraRouter` without a destination edge check bypass
  (see `assert(from != nullptr && (vehicle == nullptr || to != nullptr))` — `to` required whenever a
  vehicle is given, since permission checks need it).
- `from`/`to` must not be prohibited for the vehicle at the query time, else `compute` fails immediately.
- Each `E` in the graph must expose a `getNumericalID()` used as a dense array index into `myEdgeInfos`.
- `AStarRouter`'s heuristic requires `E::getDistanceTo`/`getSpeedLimit`/`getLengthGeometryFactor` to exist
  and the cost function to be (an admissible proxy for) travel time — the header comment explicitly warns
  the heuristic is invalid for arbitrary "effort" measures.
- `CHRouter` requires a single, static, permission-free weight scheme (no per-vehicle-class travel
  restrictions) since the contraction is computed once for the whole graph.

## Decision Process

1. Initialize source `EdgeInfo` (`effort=0`), push to frontier.
2. Repeatedly pop the minimum-effort (Dijkstra) / minimum-heuristic-effort (A*) edge from the frontier.
3. If it is the destination edge, reconstruct the path via `prev` pointers (`buildPathFrom`) and return.
4. Otherwise, mark visited, and for every successor edge (skipping prohibited ones and traversing via/
   internal edges by folding their cost in), relax: if the new tentative effort is lower than the
   successor's current known effort (and it hasn't been finalized), update it and re-heapify.
5. If the frontier empties without reaching `to`, report failure (unless `silent`).
6. CH/arc-flags add a pruning step (only expand along contraction-hierarchy "upward" edges past the query
   node's level; only expand edges whose arc-flag bit for the target's partition is set) to cut down the
   search space versus plain Dijkstra.

## Mathematical Model

- **Edge cost function** `c(e, v, t)`: pluggable, evaluated per edge, per vehicle, per time. In simulation
  the default is `c = max(length(e) / max(currentSpeed(e), ε), minimumTravelTime(v))`
  (`MSRoutingEngine::getEffort`, `MSRoutingEngine.cpp:184`), i.e. free-flow time floor with a live-speed
  numerator — this is the "live-traffic weighting": `currentSpeed(e)` is a (optionally moving-averaged)
  measured mean speed on the edge, not a static value.
- Offline (`duarouter`) default is `ROEdge::getTravelTimeStatic` (static edge speed/length, or values from
  a loaded weight-file time series when `--weight-files` is given); alternative measures select emission
  models (`PollutantsInterface::CO/CO2/PM_X/HC/NO_X/FUEL/ELEC`), `noise`, or an arbitrary stored effort
  attribute (`ROEdge::getStoredEffort`).
- **Via/internal-edge cost folding**: `updateViaEdgeCost` walks through internal (junction) edges between
  two "real" edges, adding each one's own effort/time/length into the running total before the successor
  relaxation — internal edges never appear as separate frontier nodes.
- **A* admissible heuristic**: `h(e) = distance(e, target) / speed`, where `speed = min(vehicle.maxSpeed,
  maxNetworkSpeed * vehicle.speedFactor)` — a straight-line-distance-at-best-possible-speed lower bound on
  remaining travel time. Landmark lookup tables (`LandmarkLookupTable`) replace the raw Euclidean estimate
  with a tighter precomputed lower bound using triangle inequality over selected landmark nodes.
- **Live-weight smoothing (simulation)**: `MSRoutingEngine::adaptEdgeEfforts` updates `myEdgeSpeeds[id]`
  either via a fixed-size moving average over `device.rerouting.adaptation-steps` samples, or an
  exponential moving average with weight `device.rerouting.adaptation-weight` — both are standard
  first-order filters over `MSEdge::getMeanSpeed()`, applied only to edges currently "delayed"
  (`MSEdge::isDelayed()`).
- **Route-choice probability model (Gawron)**: for two alternative routes R, S with costs/probabilities
  `(pR, pS)`, cost gap `delta = (costS - costR) / (costS + costR)`, and functions
  `g(a,x) = exp(a·x / (1-x²))`, `f(pR,pS,x) = pR(pR+pS)g(a,x) / (pR·g(a,x)+pS)`, the new probability of R is
  `f(pR,pS,delta)` and S gets the remainder — a smooth logit-like reallocation toward the cheaper route,
  parameterized by `gawron.beta` (cost-smoothing) and `gawron.a` (sensitivity), per
  `src/utils/router/GawronCalculator.h`.

## Constraints

- All routers operate on a **static graph topology** during a query; only edge *costs* (and time-bounded
  prohibitions) vary with time, not connectivity, within one `compute()` call.
- `CHRouter` cannot represent per-vehicle-class connectivity differences; `CHRouterWrapper` is required
  whenever permissions, driver preferences (`gRoutingPreferences`), or speed restrictions are present.
- `arcflag`/CH incur an upfront preprocessing cost (contraction / partitioning) amortized only across many
  queries on the same static network — unsuitable for networks whose weights change every query without
  a stable "weight period" (`weightPeriod`/`weight-period` option governs how often CH structures rebuild).
- A* heuristic validity is limited to the travel-time measure; using it with emission/fuel/noise "effort"
  breaks admissibility (explicitly noted in the header comment).

## State Transitions

Not a stateful process per se; but router **instances** transition between "clean" (`myAmClean=true`,
scratch state reset) and "dirty" (mid- or post-query) — `init()` performs the clean transition, and
`myBulkMode`/`myAutoBulkMode` deliberately skip the reset between calls that share the same source node to
reuse partial search trees for repeated destination queries from one origin.

## Edge Cases

- Loop routes (`from == to` with a real path required): handled by `computeLooped`, trying every
  via-successor of the start edge and choosing the cheapest closed walk.
- Disconnected graph / unreachable destination: `compute` returns `false`; the offline framework either
  errors out or (with `--ignore-errors`) logs and skips that vehicle.
- Landmark-lookup A* explicitly allows revisiting nodes (`mayRevisit`) when the lookup table is not
  metrically consistent, to preserve correctness at some performance cost.
- Zero-length or zero-cost edges at path endpoints are handled specially in `recomputeCostsPos` to avoid
  proportionally distributing cost over a zero-length denominator.

## Implementation

Source:
```
src/utils/router/SUMOAbstractRouter.h (EdgeInfo, compute/init/getEffort/prohibitions)
src/utils/router/DijkstraRouter.h (DijkstraRouter::compute)
src/utils/router/AStarRouter.h (AStarRouter::compute, heuristic_remaining)
src/utils/router/CHRouter.h, CHBuilder.h (contraction hierarchy)
src/utils/router/CHRouterWrapper.h
src/utils/router/AFRouter.h, AFBuilder.h, KDTreePartition.h (arc flags)
src/utils/router/RailwayRouter.h
src/utils/router/PedestrianRouter.h, IntermodalRouter.h, IntermodalNetwork.h, IntermodalEdge.h
src/utils/router/GawronCalculator.h, LogitCalculator.h, RouteCostCalculator.h
src/microsim/devices/MSRoutingEngine.cpp (getEffort, adaptEdgeEfforts)
src/duarouter/duarouter_main.cpp (computeRoutes: algorithm selection)
tools/assign/duaIterate.py (outer DUE iteration loop, Python, not C++)
```

## Related Classes

`RouterProvider` (bundles vehicle/pedestrian/intermodal/rail routers), `MSRouterProvider`/`MSVehicleRouter`
typedefs (`src/microsim/MSRouterDefs.h`), `RORouterProvider`/`ROIntermodalRouter` typedefs
(`src/router/RORoutable.h`), `RORouteDef`/`RORoute` (offline route + alternatives model),
`MSEdgeWeightsStorage` (time-indexed weight storage feeding the cost `Operation`).

## Related Tests

`tests/duarouter/dua`, `tests/duarouter/alternatives`, `tests/duarouter/logit`, plus
`tests/duarouter/options.duarouter.astar` and `options.duarouter.chrouter`/`chwrapper` config fixtures
select and exercise each algorithm; no dedicated `unittest/` GoogleTest target for
`src/utils/router` was located.

## Behavioural Invariants

- A returned path's first and last elements are always `from` and `to` respectively (bar the loop case,
  which prepends `from` itself before the sub-path).
- Effort is monotonically non-decreasing along the returned path (`assert(effort >= minimumInfo->effort)`
  in `DijkstraRouter::compute`) — costs are assumed non-negative, so no negative-cost edges are supported.
- Internal/via edges never appear as standalone entries in the returned edge vector; their cost is
  absorbed into the transition between the two adjacent "real" edges.

## Reimplementation Notes

- The single most important structural decision to copy is the **shared abstract router interface with a
  pluggable edge-cost function pointer** — this is what lets the same algorithm implementation serve both
  live simulation rerouting and offline batch routing without duplicating Dijkstra/A*/CH code.
- Keep the **via/internal-edge cost folding** as a router-side concern (not exposed in the path), matching
  how junctions are already atomic movements in the returned route.
- If matching SUMO's DUE behavior, do **not** try to implement convergence inside the router: replicate the
  split of "one-shot router with route-alternative probability blending (Gawron/logit)" (C++) plus an
  external iterate-and-check-convergence driver (script/orchestration layer) calling the router repeatedly.
- CH/arc-flag preprocessing only pays off with a stable, homogeneous cost model; a simulator with highly
  dynamic per-class restrictions should default to Dijkstra/A* and only add CH as an opt-in optimization,
  mirroring SUMO's own `CHRouterWrapper` fallback logic.

## Confidence

High for algorithm enumeration, cost-function formulas, and the one-shot-router-plus-external-iteration
DUE architecture (all directly read from source). Medium for some CH/arc-flag internal mechanics (read
headers/top of file but not the full contraction/partition algorithm bodies in exhaustive detail).
