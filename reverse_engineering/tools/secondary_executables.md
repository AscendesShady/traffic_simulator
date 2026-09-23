# Secondary native executables

## Purpose

SUMO's smaller native programs reuse the same options, XML, network, routing,
demand, emissions, and output libraries. They are not aliases for duarouter:
each changes the input interpretation or produced artifact.

## Executable inventory

| Executable | Primary job | Entry point | Core implementation | Tests |
|---|---|---|---|---|
| `jtrrouter` | expand flows/routes using junction turning ratios and sink rules | `src/jtrrouter/jtrrouter_main.cpp` | `ROJTRFrame`, `ROJTRTurnDefLoader`, `ROJTREdge` | `tests/jtrrouter/` |
| `dfrouter` | reconstruct routes/emitters from detector locations and measured flows | `src/dfrouter/dfrouter_main.cpp` | `RODFNet`, `RODFDetector`, `RODFRouteCont` | `tests/dfrouter/` |
| `marouter` | macroscopic assignment of OD demand to paths/edge loads | `src/marouter/marouter_main.cpp` | `ROMAFrame`, `ROMAAssignments`, `ODMatrix` | `tests/marouter/` |
| `od2trips` | expand OD matrices into trips or flows | `src/od2trips_main.cpp` | `ODMatrix`, `ODDistrictCont` | `tests/od2trips/` |
| `activitygen` | synthesize demand from population/activity descriptions | `src/activitygen/activitygen_main.cpp` | `AGActivityGen`, `AGCity`, `AGDataAndStatistics` | `tests/activitygen/` |
| `polyconvert` | import, project, filter, and serialize polygons/POIs | `src/polyconvert/polyconvert_main.cpp` | `PCPolyContainer`, `PCLoader*`, `PCNetProjectionLoader` | `tests/polyconvert/` |
| `emissionsDrivingCycle` | evaluate an emissions model over a speed/acceleration timeline or netstate file | `src/tools/emissionsDrivingCycle_main.cpp` | `TrajectoriesHandler`, `PollutantsInterface` | `tests/complex/emissions/` |
| `emissionsMap` | tabulate emissions over speed/acceleration/slope combinations | `src/tools/emissionsMap_main.cpp` | `PollutantsInterface`, `EnergyParams` | `tests/complex/emissionsMap/` |
| `TraCITestClient` | exercise the TraCI wire protocol | `src/traci_testclient/tracitestclient_main.cpp` | `TraCITestClient` | `tests/traci/` |
| `testlibsumo` / `testlibtraci` | execute shared API scripts against direct/remote APIs | `src/traci_testclient/testlibsumo_main.cpp`, `src/traci_testclient/testlibtraci_main.cpp` | libsumo/libtraci domains | complex API suites |

## Shared execution contract

Most production tools follow this lifecycle:

1. initialize XML and message systems;
2. register common and tool-specific options;
3. merge configuration and command-line values via `OptionsIO`;
4. validate options before allocating the main model;
5. load shared representations such as `RONet`, `ODMatrix`, geometry, or
   vehicle types;
6. execute the tool-specific transformation;
7. serialize through `OutputDevice`;
8. report `ProcessError`/standard exceptions and close common systems.

This similarity is infrastructure reuse, not behavioral equivalence. A new
implementation can share its own application shell, but each transformation
needs a distinct input/output contract and error policy.

## Routing and demand behavior

### jtrrouter

`initNet()` loads a routing graph with `ROJTREdgeBuilder`.
`loadJTRDefinitions()` reads turn definitions, sinks, and route/additional
information. `computeRoutes()` configures junction-turn routing and delegates
demand streaming to `ROLoader::processRoutes()`. The output is a selected route
consistent with local turn probabilities/permissions, not a general
least-cost assignment.

### dfrouter

The program loads a routing network, detector definitions, and detector-flow
time series. It can classify detectors, infer routes between detectors, build
detector dependencies and edge-flow maps, then write emitters, POIs, variable
speed signs, validation detectors, and end rerouters. Detector placement and
flow consistency are preconditions; ambiguity is inherent when measurements
do not uniquely determine routes.

### marouter

`marouter` loads districts and an `ODMatrix`, selects a router/cost operation,
and applies assignment logic through `ROMAAssignments`. Costs may use travel
time, stored weights, priority factors, noise, or pollutant/energy effort.
Outputs can be routes, flows, and edge-load/mean-data forms. It operates at
assignment scale and does not execute microscopic vehicle dynamics.

### od2trips and activitygen

`od2trips` validates TAZ/district references, loads an OD matrix, scales and
time-distributes demand, then writes trips or flows. `activitygen` combines a
routing network with population/activity inputs and generates mobility plans.
Both produce demand for later routing/simulation; neither guarantees that
every generated trip is routable unless the relevant checks/routing stage is
performed.

## Shape and emissions behavior

`polyconvert` creates a shared `PCPolyContainer`, establishes coordinate
projection, optionally derives a pruning boundary from a network, loads
type mappings, then invokes configured XML, OSM, DLR/Navteq, VISUM, or shape
loaders. Projection and pruning precede output and may be lossy.

The two emissions tools use the same `PollutantsInterface`/energy parameters as
simulation devices and outputs. `emissionsDrivingCycle` evaluates a timeline
and can derive acceleration from adjacent speed samples;
`emissionsMap` evaluates a configured grid. They validate model/vehicle-type
inputs but do not simulate traffic interactions.

## State and invariants

- IDs and network references must remain valid in the shared routing/network
  representation.
- Time intervals, flows, probabilities, and OD totals require explicit units
  and deterministic rounding/distribution rules.
- Projection must be initialized before geometry conversion or pruning.
- Cost-operation selection must match the requested measure; falling back from
  a missing weight/emission model must be explicit.
- Test clients are verification executables and must not be treated as public
  simulation engines.
- Output schema, ordering, exit status, warnings, and error tolerance are part
  of command-line compatibility.

## Edge Cases

- No valid districts, vehicles, or usable input is rejected by the relevant
  main/check function.
- Disconnected OD pairs and missing sink/turn information may be skipped,
  warned, or rejected according to options.
- Sparse or inconsistent detector flows can produce ambiguous reconstruction.
- Polygon pruning without a usable boundary/network is invalid.
- An unavailable emissions class or missing PHEMlight data prevents the
  requested calculation rather than silently producing valid-looking values.

## Modification Points

Change shared shortest-path mechanics in `src/utils/router/` and routing graph
semantics in `src/router/`; change only a tool's interpretation in its own
`RODF*`, `ROJTR*`, `ROMA*`, `OD*`, `AG*`, or `PC*` layer. New CLI options must
be registered, validated, consumed, and covered by meta/error plus functional
tests. Emissions changes must be checked in simulation output and both
standalone tools.

## Confidence

High for targets, entry points, principal transformations, and test roots.
Medium for numerical equivalence of detector reconstruction, activity
generation, and macro assignment; those algorithms require dedicated
case-by-case documents if included in a reimplementation profile.
