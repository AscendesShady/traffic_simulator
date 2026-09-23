# libsumo — In-Process Direct Simulation API

## Purpose
`libsumo` is a C++ library that exposes the same conceptual API as TraCI
(same domains: Edge, Lane, Vehicle, Junction, Person, TrafficLight, ...;
same command/variable constants) but as **direct static C++ method calls
into the running simulation's data structures**, with no socket, no byte
serialization, and no separate server process. It is compiled into
language bindings via SWIG.

Source:
```
src/libsumo/Helper.h, Helper.cpp
src/libsumo/Edge.h, Edge.cpp
src/libsumo/CMakeLists.txt
src/libsumo/libsumo.i, libsumo_typemap.i
```

## Responsibilities
- Provide `getIDList`, `getXxx`, `setXxx`, `subscribe` static methods per
  domain (`libsumo::Edge::getTraveltime`, `libsumo::Vehicle::setSpeed`,
  etc.) that read/write `MSEdge`, `MSVehicle`, `MSLane`, `MSNet`, etc.
  directly (Edge.cpp:49-117 shows `getIDList` calling `MSEdge::insertIDs`
  directly, and `getEdge` calling `MSEdge::dictionary`).
- Provide `Helper` (`src/libsumo/Helper.h/.cpp`) as shared plumbing used by
  every domain class: coordinate conversions (`makeTraCIPosition`,
  `makePositionVector`), road-network lookups (`getEdge`,
  `getLaneChecking`, `convertCartesianToRoadMap`), vehicle/person lookup
  (`getVehicle`, `getPerson`, `getTrafficObject`), subscription filter
  evaluation (`applySubscriptionFilterLanes`, `...Turn`,
  `...LateralDistance`, `...FieldOfVision`), and the `moveToXY` mapping
  algorithm (`moveToXYMap`, `findCloserLane`).
- Own the process-wide subscription state for the embedded/in-process
  case: `Helper::mySubscriptions`, `Helper::handleSubscriptions`,
  `Helper::SubscriptionWrapper` (a `VariableWrapper` implementation that
  writes results into `SubscriptionResults`/`ContextSubscriptionResults`
  maps instead of a wire buffer — contrast with `TraCIServer`'s own
  `wrap*` methods that serialize to bytes, TraCIServer.cpp:139-440).
- Register itself as `MSNet::VehicleStateListener` /
  `TransportableStateListener` (`Helper::registerStateListener`,
  `Helper::VehicleStateListener`/`TransportableStateListener` nested
  classes, Helper.h:265-277) so `Simulation.getDepartedIDList()`-style
  queries work without TraCI.

## Inputs
Direct C++ (or SWIG-wrapped) function calls from an embedding host
process/language, e.g. `libsumo.vehicle.setSpeed("veh0", 10.0)` from
Python.

## Outputs
Direct return values (no serialization) and direct mutation of `MSNet`'s
object graph. No network I/O.

## State
- Everything is static (`Edge::mySubscriptionResults`,
  `Helper::mySubscriptions`, `Helper::myLaneTree`,
  `Helper::myRemoteControlledVehicles/Persons`) — libsumo assumes exactly
  one embedded simulation per process, consistent with `MSNet` itself
  being effectively a singleton (`MSNet::getInstance()`).
- `Helper::myWrapper` is a `std::map<int, std::shared_ptr<VariableWrapper>>`
  keyed by command ID, letting `handleSubscriptions` reuse the same
  domain-dispatch idea as `TraCIServer::myExecutors`, but targeting
  in-memory results instead of a socket.

## Dependencies
- `microsim/*` directly (`MSNet`, `MSEdge`, `MSLane`, `MSVehicle`,
  `MSEdgeWeightsStorage`, `MSInsertionControl`, transportables, traffic
  lights).
- `libsumo/TraCIDefs.h`, `TraCIConstants.h`, `Subscription.h`,
  `StorageHelper.h` — shared data types and constants also used by the
  TraCI server and by libtraci; this is the "shared vocabulary" layer
  referenced in `traci.md`.
- No dependency on `foreign/tcpip` (no socket code) — confirmed by
  `Edge.cpp`'s include list (no `tcpip` headers), unlike
  `src/libtraci/Edge.cpp` which includes `Connection.h`/`Domain.h`.

## Consumers
- `src/traci-server/TraCIServerAPI_*.cpp` — every domain handler in the
  TraCI server calls into `libsumo::<Domain>::*` to do the actual work
  (e.g. `TraCIServerAPI_Edge.cpp:71,77,88,92,108,112,123,129,137` call
  `libsumo::Edge::setAllowed/setDisallowed/adaptTraveltime/setEffort/
  setMaxSpeed/setFriction/setParameter`). This means **libsumo is not just
  an alternate API — it is the actual implementation the TraCI server
  itself is built on**.
- SWIG-generated bindings, per `src/libsumo/CMakeLists.txt`:
  - **Python** (`ENABLE_PYTHON_BINDINGS`, `SWIG_ADD_LIBRARY(libsumo LANGUAGE python ...)`,
    output to `tools/libsumo`) — this is what Python users import as
    `import libsumo`.
  - **Java** (`ENABLE_JAVA_BINDINGS`, package
    `org.eclipse.sumo.libsumo`, built into a `.jar` via Maven).
  - **C#** (`ENABLE_CS_BINDINGS`, namespace `Eclipse.Sumo.Libsumo`).
  - **C** (`ENABLE_C_BINDINGS`, flat C API, `-nocxx`).
  - A pure C++ shared library `libsumocpp` is always built when SWIG is
    available, independent of which language bindings are enabled
    (CMakeLists.txt:83-93), for direct C++ embedding.
- `src/fmi/` (Functional Mock-up Interface) links against libsumo via a
  C-callable bridge (`libsumocpp2c.cpp/.h`, `sumo2fmi_bridge.c`) — see
  `external_interfaces.md`.

## Execution
There is no protocol loop: each call is synchronous and immediate. A
`simulationStep()` equivalent is `libsumo::Simulation::step(...)`
(verified in `src/libsumo/Simulation.cpp:168`) which calls
`MSNet::simulationStep()` once for time zero or repeatedly until the requested
target time.
Subscriptions are evaluated on demand via `Helper::handleSubscriptions(t)`,
called at the end of `Simulation::step()` with the resulting `SIMSTEP`.

GUI support: when built with FOX (`FOX_FOUND`), a second static library
`libsumoguistatic` and `GUI.cpp/.h` are compiled with
`HAVE_LIBSUMOGUI` defined, adding GUI-specific variables/behavior; the
plain `sumo`-linked `libsumostatic` excludes `GUI.cpp` entirely
(CMakeLists.txt:56-86).

## Important Classes
- `libsumo::Helper` (`Helper.h/.cpp`) — shared lookup/subscription/geometry
  utilities used by every domain class.
- `libsumo::Helper::SubscriptionWrapper` — in-memory analogue of
  `TraCIServer`'s `wrap*` methods.
- One class per domain (`Edge`, `Lane`, `Vehicle`, `VehicleType`,
  `Junction`, `Person`, `TrafficLight`, `Route`, `POI`, `Polygon`,
  `BusStop`, `Calibrator`, `ParkingArea`, `ChargingStation`, `RouteProbe`,
  `Rerouter`, `VariableSpeedSign`, `MeanData`, `OverheadWire`,
  `LaneArea`, `MultiEntryExit`, `Simulation`, `GUI`) mirroring the TraCI
  domain list one-to-one (compare `src/libsumo/*.h` against
  `src/traci-server/TraCIServerAPI_*.h`).
- types from `src/libsumo/TraCIDefs.h` (`TraCIPosition`, `TraCIColor`,
  `TraCIStage`, `TraCIException`, `VariableWrapper` interface) shared
  across libsumo, the TraCI server, and libtraci.

## Important Functions
- `Helper::getEdge/getVehicle/getPerson/getTrafficObject/getVehicleType/
  getTLS/getStoppingPlace` — the canonical "resolve string ID to live
  simulation object, or throw `TraCIException`" pattern (e.g.
  `Edge::getEdge`, Edge.cpp:92-99, throws
  `TraCIException("Edge '" + edgeID + "' is not known")`).
- `Helper::subscribe` / `handleSubscriptions` / `clearSubscriptions` —
  subscription lifecycle shared conceptually with `TraCIServer`'s own
  subscription bookkeeping but operating on `libsumo::Subscription`
  objects directly rather than via the wire protocol.
- `Helper::moveToXYMap*`, `findCloserLane`, `patchShapeDistance` — the
  `moveToXY`/`convertCartesianToRoadMap` algorithms backing TraCI's
  freeform vehicle positioning commands.
- `Helper::applySubscriptionFilters*` — the actual filter algorithms that
  `TraCIServer::addSubscriptionFilter*` (server-side) merely configures.

## Behaviour
libsumo functions raise `libsumo::TraCIException` (or `FatalTraCIError`)
on invalid input instead of returning an error code, which the TraCI
server layer (`TraCIServerAPI_*`) catches and converts into an
`RTYPE_ERR` status message — libsumo itself has no concept of a
wire-level status/response code.

## Edge Cases
- Because state is static/global, only one simulation can be embedded per
  process at a time (Confidence: Medium — inferred from static member
  design; not verified against any explicit multi-instance guard).
- `libsumoguistatic`/`libsumocpp` built `WITH` GUI recompile the entire
  source list a second/third time rather than linking a shared object,
  because `HAVE_LIBSUMOGUI` changes behavior inside `Simulation.cpp` and
  `Helper.cpp` (comment, CMakeLists.txt:69).

## Tests

`tests/complex/unit_tests/testlibsumo/` drives the native test executable.
Domain semantics are also exercised through the shared API implementation by
the corresponding TraCI domain suites; exact direct-versus-remote parity must
still be checked per domain rather than assumed from a shared method name.

## Modification Points
- Adding a variable/method to a domain: implement it in the
  `libsumo::<Domain>` class (this is the single source of truth), then
  optionally expose it through `TraCIServerAPI_<Domain>` for TraCI/libtraci
  access — libsumo consumers (Python/Java/C#/C bindings) get it
  automatically once the SWIG `.i` interface files pick up the new public
  method (`libsumo.i`, `libsumo_typemap.i` control what SWIG exposes and
  how types are mapped).
- Enabling a new binding language is a CMake concern
  (`ENABLE_JAVA_BINDINGS`, `ENABLE_CS_BINDINGS`, `ENABLE_C_BINDINGS`,
  `ENABLE_PYTHON_BINDINGS` in `src/libsumo/CMakeLists.txt`), not a
  per-domain code change.

## Relationship to TraCI and libtraci (verified)
- **TraCI** (`src/traci-server`): out-of-process, language-agnostic,
  socket wire protocol; `TraCIServerAPI_*` handlers are thin adapters that
  parse bytes and call `libsumo::*`.
- **libsumo** (this doc): in-process, direct function calls, no
  serialization; is the actual implementation both `sumo`'s embedded
  TraCI server and SWIG-based bindings (Python/Java/C#/C) build on.
- **libtraci** (`src/libtraci`, see its files) is a *different* thing from
  libsumo despite similar naming: reading `src/libtraci/Edge.cpp` shows
  its `Edge::getTraveltime` calls `Dom::getDouble(libsumo::VAR_EDGE_TRAVELTIME, edgeID, ...)`
  where `Dom` is `Domain<CMD_GET_EDGE_VARIABLE, CMD_SET_EDGE_VARIABLE>`
  (`libtraci/Domain.h`) built on `libtraci::Connection`, which owns a
  `tcpip::Socket` (`Connection.h:180`) and does
  `mySocket`-based `doCommand`/`createCommand`. So **libtraci is a
  lightweight, header-light, pure-C++ TraCI *client* that still speaks the
  identical wire protocol to a separate/embedded server process** — it is
  architecturally on the TraCI side of the fence, not the libsumo side,
  even though its per-file API surface (`Edge.h`, `Vehicle.h`, ...)
  intentionally mirrors libsumo's so client code can switch between the
  two by changing which library it links (confirmed by
  `src/libsumo/libtraci.h` and the `#define LIBTRACI 1` guard visible in
  `src/libtraci/Edge.cpp:22`, which lets shared headers branch on which
  backend is compiled in).
- All three share the same command/variable ID constants
  (`src/libsumo/TraCIConstants.h`) and the same exception/data types
  (`src/libsumo/TraCIDefs.h`), which is why the API "reads the same" across
  all three despite very different transports.

## Confidence
High for the direct-call architecture, step/subscription behavior, SWIG binding
matrix, and libtraci distinction, traced through `Simulation.cpp`,
`Helper.cpp`, the domain implementations, and CMake. Medium for the "one
simulation per process" conclusion and exhaustive direct-versus-remote domain
parity.
