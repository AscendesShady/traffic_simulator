# TraCI (Traffic Control Interface) — Socket Protocol Server

## Purpose
TraCI is SUMO's remote-control protocol: a length-prefixed, typed, binary
request/response protocol running over TCP, used by out-of-process clients
(Python `traci` module, ns-3, MATLAB, etc.) to inspect and manipulate a
running `sumo`/`sumo-gui` simulation step by step.

Source:
```
src/traci-server/TraCIServer.h
src/traci-server/TraCIServer.cpp
```

## Responsibilities
- Own a TCP server socket (`tcpip::Socket`) and accept one or more client
  connections (`num-clients` option).
- Read commands from the wire, dispatch them by numeric command ID to a
  handler, and write typed responses back.
- Translate protocol commands into calls against `libsumo::*` static methods
  (which in turn touch `MSNet`/`MSEdge`/`MSVehicle`/... directly) and encode
  the results back into the wire format.
- Manage subscriptions (periodic push of variable values without repeated
  requests) and subscription filters (spatial/context filters for vehicle
  context subscriptions).
- Manage multi-client execution ordering (`CMD_SETORDER`) and per-client
  target time bookkeeping so several TraCI clients can each step the
  simulation at their own cadence within one shared SUMO step.
- Track vehicle/transportable state changes (departed, arrived, teleport,
  parking, collision, etc.) for subscription and for the `getVehicleStateChanges`
  API used by `Simulation.getDepartedIDList()` etc.

## Inputs
- Command bytes from a `tcpip::Socket` (`myInputStorage`, a
  `tcpip::Storage` byte buffer with typed read helpers).
- Options via `OptionsCont`: `remote-port`, `num-clients`, `begin`
  (`TraCIServer::openSocket`, TraCIServer.cpp:592-607).
- Simulation events, via being registered as
  `MSNet::VehicleStateListener` / `MSNet::TransportableStateListener`
  (TraCIServer.h:59).

## Outputs
- Response bytes written to `myOutputStorage` and sent with
  `tcpip::Socket::sendExact`.
- Side effects on the live simulation: vehicles inserted/rerouted, TLS
  programs switched, edge speeds/permissions changed, POIs/polygons added,
  etc., all performed by calling into `libsumo::*` (never microsim classes
  directly from the server code — see Important Classes below).

## State
- `TraCIServer` is a process-wide singleton (`myInstance`,
  `getInstance()`), instantiated lazily the first time `openSocket` is
  called with `remote-port != 0` (TraCIServer.cpp:592-593).
- Per-client state lives in `SocketInfo` (TraCIServer.h:215-236): target
  time, "half step" flag (`executeMove`), per-client vehicle/transportable
  state-change buffers — needed because in multi-client mode each client
  may be at a different point in the same SUMO step.
- Global (`myTargetTime`, `mySockets`, `mySubscriptions`,
  `mySubscriptionCache`, `myParameterized`, `myExecutors`) declared in
  TraCIServer.h:280-343.
- Subscriptions are stored as `std::vector<libsumo::Subscription>`
  (`mySubscriptions`) — the definition of `Subscription` lives in
  `src/libsumo/Subscription.h`, shared with libsumo's own subscription
  handling (`libsumo::Helper::handleSubscriptions`).

## Dependencies
- `foreign/tcpip/socket.h`, `foreign/tcpip/storage.h` — a small bundled TCP
  socket + typed-buffer library (`src/foreign/tcpip`), used identically by
  the server and by `libtraci`/`utils/traci` clients.
- `libsumo/*` (`Helper`, `TraCIConstants`, `TraCIDefs`, `Subscription`,
  `StorageHelper`) for command constants, data structures, and the actual
  simulation-facing calls.
- `microsim/MSNet` and friends, but only reached indirectly through
  `libsumo::*` calls, plus directly for state-listener registration and
  time-step bookkeeping (`MSNet::getInstance()->getCurrentTimeStep()`).
- One `TraCIServerAPI_<Domain>.h/.cpp` file per protocol domain (Edge,
  Vehicle, Lane, InductionLoop, Person, TrafficLight, Route, POI, Polygon,
  Junction, LaneArea, MultiEntryExit, BusStop, ParkingArea,
  ChargingStation, RouteProbe, Rerouter, VariableSpeedSign, MeanData,
  OverheadWire, VehicleType, Calibrator, Simulation) — 24 domain files in
  `src/traci-server/`.

## Consumers
- Any TraCI client: Python `sumolib`/`traci` package (`tools/traci`),
  `libtraci` (in-process C++ client, still speaking this same wire
  protocol — see `libsumo.md`), `TraCIAPI` in `src/utils/traci/`
  (used, for example, by some SUMO tools and tests as a lightweight embedded C++
  client), and third-party tools such as ns-3's TraCI clients or Flow/RLlib.
- `sumo-gui` embeds the same `TraCIServer`; `TraCIServerAPI_GUI` variables
  are rejected by plain `sumo` with `RTYPE_NOTIMPLEMENTED`
  ("GUI is not running...", TraCIServer.cpp:1132-1134, 1283-1285).

## Execution — protocol handshake / step loop
1. **Startup**: `TraCIServer::openSocket` is called from
   `NLBuilder::init` right after `MSNet` construction and before route
   loading (`src/netload/NLBuilder.cpp:372-374`), specifically so that
   `VehicleState::BUILT` events during initial route parsing are already
   observed by the server. If `remote-port` is 0, no server is created and
   TraCI is inert.
2. **Connection**: the constructor blocks in a loop accepting
   `num-clients` connections (TraCIServer.cpp:541-569, `serverSocket.accept(true)`).
   With `num-clients > 1`, `checkClientOrdering()` (TraCIServer.cpp:653-709)
   is run once: each client's first command must be `CMD_GETVERSION`
   and/or `CMD_SETORDER`; other commands during this init phase throw
   `ProcessError`.
3. **Per-SUMO-step loop**: `TraCIServer::processCommands(step, afterMove)`
   (TraCIServer.cpp:802 onward) is the main entry point, called by the
   simulation loop (`MSNet::simulate`) once per simulation step:
   - Applies pending `CMD_SETORDER` reordering requests
     (`processReorderingRequests`).
   - On steps after the first, sends queued subscription results out to
     all clients that are due to act (`postProcessSimulationStep`,
     `sendOutputToAll`).
   - Computes `myTargetTime = nextTargetTime()` = min over clients'
     requested target times; if the next SUMO step is still before that,
     returns immediately (simulation can advance without TraCI
     involvement).
   - Otherwise loops over clients whose target time has been reached,
     calling `dispatchCommand()` repeatedly until a "stepping" command
     (`CMD_SIMSTEP`, `CMD_LOAD`, `CMD_EXECUTEMOVE`, `CMD_CLOSE`) is seen for
     that client, then moves to the next client — this is how multiple
     clients interleave within one SUMO timestep.
4. **Command framing**: each command is
   `[length:ubyte-or-(0,int)] [commandId:ubyte] [payload...]`
   (`readCommandID`, TraCIServer.cpp:1100-1115). `dispatchCommand`
   (TraCIServer.cpp:1118-1304) reads the ID, looks it up in `myExecutors`
   (a `std::map<int, CmdExecutor>` populated in the constructor for `SET_*`
   commands, TraCIServer.cpp:469-492) or in the `CMD_GET_*` range
   (dispatched to `processGet`), or falls into a `switch` for
   server-internal commands (`CMD_GETVERSION`, `CMD_LOAD`,
   `CMD_EXECUTEMOVE`, `CMD_SIMSTEP`, `CMD_CLOSE`, `CMD_SETORDER`,
   subscribe/context-subscribe commands, `CMD_ADD_SUBSCRIPTION_FILTER`).
   After dispatch it verifies the input cursor landed exactly at
   `commandStart + commandLength`; a mismatch is treated as a protocol
   error and closes the connection (TraCIServer.cpp:1295-1302).
5. **CMD_SIMSTEP**: increments the calling client's `targetTime` by
   `DELTA_T` (or to an explicit time), clears that client's buffered state
   changes, and returns control to `processCommands`'s outer loop, which
   will actually advance `MSNet` once all interleaved clients have reached
   their target (the outer stepping happens in `MSNet::simulate`, not
   shown here — inferred from the collaboration, Confidence: Medium).
6. **CMD_LOAD**: reads a string-list of new command-line args into
   `myLoadArgs`; the caller of `processCommands` (`MSNet`/`NLBuilder`) is
   expected to notice `myLoadArgs` is non-empty and reload
   (Confidence: Medium — inferred from the early `break` in
   `processCommands` at TraCIServer.cpp:883-887).
7. **Shutdown**: `CMD_CLOSE` from the last remaining client sets
   `myDoCloseConnection`; `TraCIServer::close()` deletes the singleton
   instance (called unconditionally at the end of `main()` in
   `sumo_main.cpp:130`).

## Important Classes
- `TraCIServer` (`src/traci-server/TraCIServer.h/.cpp`) — the server
  itself; also implements `libsumo::VariableWrapper` so libsumo getters can
  write directly into the server's wire-format output buffer without an
  intermediate generic-value type (see `wrapInt`, `wrapDouble`,
  `wrapPositionVector`, etc., TraCIServer.cpp:139-440).
- `TraCIServer::SocketInfo` — per-client connection state.
- `TraCIServerAPI_<Domain>` (24 files) — one static-method class per
  protocol domain, each with `processGet`/`processSet` that parse the
  domain-specific variable IDs and forward to `libsumo::<Domain>::*`.
- `libsumo::Subscription` (`src/libsumo/Subscription.h`) — shared
  subscription record type used by both the server and libsumo's own
  in-process subscription handling.

## Important Functions
- `TraCIServer::openSocket(execs)` — lazy singleton construction, add
  vehicle/transportable listeners (TraCIServer.cpp:592-607).
- `TraCIServer::processCommands(step, afterMove)` — the per-step command
  pump (TraCIServer.cpp:802-...).
- `TraCIServer::dispatchCommand()` — single command dispatch
  (TraCIServer.cpp:1118-1304).
- `TraCIServer::processGet(commandID, in, out)` — routes `CMD_GET_*`
  commands to the matching `TraCIServerAPI_<Domain>::processGet`
  (declared TraCIServer.h:103).
- `TraCIServer::postProcessSimulationStep()` — evaluates and serializes
  all active subscriptions into `mySubscriptionCache` once per step
  (TraCIServer.cpp:1326-1381+), pruning expired ones (`endTime < t`) and
  vehicles/persons that have since arrived.
- `TraCIServer::checkClientOrdering()` — enforces `CMD_SETORDER` handshake
  for multi-client setups.
- `addObjectVariableSubscription`, `addSubscriptionFilter*` — subscription
  and geometric/road-network filter setup (lanes, turn, vClass, vType,
  field of vision, lateral distance, up/downstream distance,
  no-opposite, lead/follow) — declared TraCIServer.h:345-374.

## Behaviour
- Command IDs and variable IDs are shared constants defined in
  `src/libsumo/TraCIConstants.h` — the same header used by the server, by
  libsumo, and by libtraci, which is why all three speak semantically
  identical vocabularies even though only two of them use the wire format.
- Unrecognized/unimplemented commands get `RTYPE_NOTIMPLEMENTED`
  (TraCIServer.cpp:1287, 1133).
- Any `libsumo::TraCIException` thrown by a domain call is caught at the
  `TraCIServerAPI_*::processSet/processGet` layer and converted into an
  `RTYPE_ERR` status response (see `TraCIServerAPI_Edge.cpp:143-145` for
  the pattern) — the server itself does not abort the connection for
  application-level errors, only for protocol framing errors.
- GUI-only variables (`CMD_GET_GUI_VARIABLE`, `CMD_SET_GUI_VARIABLE`)
  are explicitly special-cased to fail gracefully under headless `sumo`.

## Edge Cases
- Multi-client ordering: clients must all send `CMD_SETORDER` (optionally
  preceded by `CMD_GETVERSION`) before the first real step; any other
  first command throws `ProcessError` and aborts startup
  (TraCIServer.cpp:700).
- A framing mismatch after dispatch (wrong number of bytes consumed) is
  treated as fatal for the connection (`myDoCloseConnection = true`,
  TraCIServer.cpp:1301) rather than merely an error response.
- Subscriptions to vehicles/persons that arrive/leave the simulation are
  pruned automatically each step (TraCIServer.cpp:1336-1343).
- `CMD_LOAD` only captures the reload args for "the client that issued the
  load command" — multi-client load is explicitly noted as unimplemented
  (comment at TraCIServer.cpp:1152-1153, referencing ticket #3146).

## Tests

The test tree was enumerated in the final pass. Protocol-level suites under
`tests/traci/` cover get/set, variable subscriptions, context subscriptions,
and general API behavior. Domain suites under `tests/complex/traci/` include
edge, lane, vehicle, person, junction, traffic light, GUI, detectors,
stopping places, rerouters, route probes, overhead wire, mean data, and
subscription-filter cases. Directory presence establishes breadth; exact
assertion strength remains case-specific.

## Modification Points
- Adding a new gettable/settable domain variable: add the constant to
  `libsumo/TraCIConstants.h`, extend the relevant
  `libsumo::<Domain>` class with the actual logic, then extend the
  matching `TraCIServerAPI_<Domain>::processGet/processSet` switch to wire
  the new variable ID to it (the pattern demonstrated by
  `TraCIServerAPI_Edge.cpp`).
- Adding an entirely new domain requires: a `libsumo::<Domain>` class, a
  `TraCIServerAPI_<Domain>` translation class, registering its
  `CMD_SET_*`/subscribe IDs in the `TraCIServer` constructor's
  `myExecutors`/switch tables, and mirroring it in `libtraci` if an
  in-process-but-wire-protocol C++ client binding is desired.
- Multi-client behavior and subscription filter logic are concentrated in
  `TraCIServer.cpp`; changes there affect all domains simultaneously.

## Confidence
High for protocol framing, command dispatch, step integration, and test-suite
breadth, traced through `TraCIServer.cpp`, `MSNet.cpp`, and the enumerated test
roots. Medium for exact multi-client timing in every close/load/error path.
