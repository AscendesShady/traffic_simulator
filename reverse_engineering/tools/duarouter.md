# duarouter

## Purpose

`duarouter` turns trips, flows, and incomplete/alternative route definitions
into routes over a loaded network. It can route vehicles and intermodal persons,
use several shortest-path engines, consume time-dependent weights, and emit
route alternatives.

Primary implementation:

- `src/duarouter/duarouter_main.cpp`
- `src/duarouter/RODUAFrame.*`, `RODUAEdgeBuilder.*`
- `src/router/ROLoader.*`, `RONet.*`, `RORouteDef.*`, `ROVehicle.*`
- `src/utils/router/DijkstraRouter.h`, `AStarRouter.h`, `CHRouter.h`,
  `CHRouterWrapper.h`

Related tests: `tests/duarouter/` (`trips`, `flows`, `alternatives`, `dua`,
`closing`, `vclasses`, `person`, `errors`, and algorithm variants).

## Execution flow

1. `RODUAFrame::fillOptions()` composes common routing/import/output options.
2. `ROLoader::loadNet()` loads the routing graph as `RONet`/`ROEdge` objects.
3. Weight files and route inputs are opened.
4. `computeRoutes()` selects a router and wraps it in a provider.
5. `ROLoader::processRoutes()` parses demand in time intervals and asks route
   definitions/person trips to compute paths.
6. `RONet`/route definitions write routes, alternatives, and optional
   intermodal outputs.

## Router selection

The `routing-algorithm` option accepts `dijkstra`, `astar`, `CH`, and
`CHWrapper`; this checkout also has an `arcflag` branch in
`duarouter_main.cpp::computeRoutes()`. The code falls back or rejects combinations
when permissions, restrictions, bulk routing, or non-travel-time weights are
unsupported. `RODUAFrame::checkOptions()` is part of the contract, not merely UI
validation.

Cost normally represents travel time, but `ROEdge` can derive other weight
attributes, including emissions. Time-dependent weights are loaded through
`ROLoader` timeline retrievers. Vehicle class permissions and restriction
parameters affect edge admissibility.

## DUA distinction

`duarouter` performs a routing pass. Iterative dynamic user assignment is
orchestrated by Python tooling such as `tools/assign/duaIterate.py`, which calls
router and simulator repeatedly and updates weights. Do not describe the
single C++ executable as an equilibrium solver by itself.

## Invariants and edge cases

- Input edge IDs must exist and the selected vehicle/person mode must have an
  admissible connected path.
- Routing graph costs must be nonnegative for the selected shortest-path engine.
- A route's mandatory via edges must appear in order.
- Closed edges/lanes, permissions, TAZ connectors, and time intervals may make
  a graph structurally connected but unusable for a specific request.
- Algorithm caches/preprocessing become invalid when weights or permissions
  change outside their supported model.

## Modification points

Change graph semantics in `ROEdge`/`RONet`, path algorithms under
`src/utils/router`, demand parsing in `ROLoader`/handlers, and CLI compatibility
in `RODUAFrame`. Validate each supported algorithm, permissions, time-dependent
weights, and disconnected routes.

## Confidence

High for execution and router dispatch; medium for every cost mode because
mode-specific logic is distributed across `ROEdge`, vehicle classes, and
intermodal routing.
