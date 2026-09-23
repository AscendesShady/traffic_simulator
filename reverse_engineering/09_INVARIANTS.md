# 09. Behavioral and structural invariants

These invariants are derived from ownership checks, assertions, phase order,
parser/build validation, and regression structure. They are requirements for a
compatible implementation unless a documented feature is deliberately omitted.

## Time and phase

1. A completed step advances current time exactly once by the configured fixed
   step length.
2. A move-only external step is incomplete until post-move processing runs;
   output and time must not advance twice.
3. Scheduled commands execute in their owning begin/insertion/end phase and in
   deterministic time/order semantics.
4. TLS state used for vehicle planning is the state selected before the
   movement phase of that step.
5. Detector/output observations labeled for a step correspond to the defined
   completed-state boundary.

Evidence: `MSNet::simulationStep()`, `postMoveStep()`, `MSEventControl`.

## Network topology

1. IDs are unique within node, edge, lane, route, vehicle/type, TLS and other
   named-object domains as enforced by their registries.
2. Every edge references valid endpoint junctions and owns at least the valid
   lane set required by its function.
3. Every connection references valid source/destination lane indices and a
   topologically consistent destination/via lane.
4. A route edge transition is usable only when a permitted lane-level
   connection exists for that vehicle class.
5. Connection order, junction request/response bits, internal-lane mapping, and
   TLS link indices describe the same movement ordering.
6. Runtime topology is fully computed/loaded before simulation; structural
   edits require recomputation/reload rather than ad-hoc pointer changes.

Evidence: `NBNetBuilder::compute()`, `NBNode::computeLogic*`,
`NWWriter_SUMO`, `NLEdgeControlBuilder`, `MSLink`.

## Vehicle and lane state

1. `MSVehicleControl` is the lifetime owner for every registered vehicle; lane,
   link, device, GUI and API structures hold non-owning references unless
   explicitly documented.
2. An on-road vehicle has a valid current lane and appears exactly once as a
   full vehicle in that lane's ordered container.
3. The lane vehicle container is ordered consistently with longitudinal
   position before leader/collision queries.
4. Partial/shadow/further-lane memberships accurately represent physical
   overlap and are cleared on maneuver completion/removal.
5. Vehicle current lane edge and route position agree, except during explicit
   modeled transfer/opposite/internal transitions whose state carries the
   mapping.
6. Approach records, movement reminders, parking references and lane-change
   reservations never outlive the traffic object.
7. A vehicle is not simultaneously pending departure and committed on-road.

Evidence: `MSLane::incorporateVehicle`, `integrateNewVehicles`,
`MSVehicle::executeMove/onRemovalFromNet`, `MSVehicleControl`.

## Motion and safety

1. Planning precedes execution; vehicle positions are not incrementally changed
   while gathering same-phase lane plans.
2. Committed speed is the result of applicable car-follow, stop, lane/link,
   lane-change, physical/type/lane and external-control constraints in their
   defined precedence.
3. Units remain metres, seconds-derived m/s and m/s² at public/numeric model
   boundaries, with explicit `SUMOTime` conversion.
4. A vehicle cannot cross a closed/red/blocked link merely because free-road
   speed permits it.
5. A lane change commits only with route/permission compatibility and required
   leader/follower safety, or with an explicit external mode that defines which
   checks may be ignored.
6. Collision recovery does not retroactively validate unsafe preventive logic.

Evidence: `MSVehicle::planMove/executeMove`, `MSCFModel`, `MSLink::opened`,
lane-change models, `MSLane::detectCollisions`.

## Insertion, lifecycle and demand

1. Generated vehicle IDs are unique; flow repetition index/counter updates are
   deterministic for a seed and step length.
2. Successful insertion changes lane membership and departure counters/listener
   state exactly once.
3. Retryable failed insertion retains object ownership and is reconsidered;
   configured terminal failures increment discard/end semantics consistently.
4. Running count increments on actual departure and decrements once during
   finalized removal.
5. Removal during lane iteration is deferred; final devices/output run before
   object destruction.
6. Teleport/parking/jump vehicles absent from normal lane traffic remain owned,
   route-aware and available for safe reinsertion or terminal removal.

Evidence: `MSInsertionControl`, `MSVehicleControl`, `MSVehicleTransfer`.

## Junctions and traffic lights

1. A link's state character maps to the intended controlled movement.
2. Approach records represent current live plans and are removed/replaced after
   pass, reroute, remote change or removal.
3. Traffic-light permission and right-of-way/foe safety are separate layers;
   green is not an unconditional guarantee of space or no conflict.
4. An active TLS program has a valid phase and next-switch state; state restore
   preserves its timing/controller-specific state.

## Persons and public transport

1. Each person/container has exactly one active plan stage and a consistent
   physical owner/location.
2. Onboard transportables refer to a live vehicle and capacity accounting is
   consistent.
3. Stop sequence and positions remain compatible with the vehicle route after
   route/stop mutation.
4. Walking position lies on the current stage geometry and crossings are seen
   consistently by pedestrian and vehicle conflict logic.

## Mesoscopic, rail, and electric systems

1. A mesoscopic vehicle belongs to at most one normal segment queue, whose
   occupancy and leader event agree with membership.
2. Mesoscopic leader events execute in nondecreasing time/tie order; invalidated
   events cannot move a vehicle twice.
3. Rail driveway state is derived from the current route and occupancy before
   its signal state is used for movement.
4. Rail wait relations are reset at their defined step boundary and constraints
   retain valid trip/vehicle references across insertion and state load.
5. Battery energy is bounded by capacity and propulsion, recuperation, and
   charging are each accounted once per movement interval.
6. An electric-hybrid vehicle's active wire matches its lane position, while
   shared substation/circuit limits are applied consistently to all connected
   vehicles.

Evidence: `MELoop`, `MESegment`, `MSRailSignalControl`, `MSDriveWay`,
`MSDevice_Battery`, `MSDevice_ElecHybrid`, `MSOverheadWire`.

## State save/load and randomness

1. State load uses a compatible static network and supported configuration.
2. Restored cross-references (vehicle-lane-route-stop-device/TLS) resolve before
   stepping resumes.
3. RNG stream state and ownership are restored for deterministic continuation.
4. Cached leader/router/geometry/output state is rebuilt or restored from its
   authoritative inputs, never accepted stale.

## Interface invariants

1. TraCI server, libtraci/Python clients, libsumo and constants agree on command
   IDs, types and compound-field ordering.
2. A TraCI simulation-step response is sent only at the protocol-defined
   completion point and includes current subscription results.
3. XML writers emit references and units accepted by the corresponding parser;
   schemas alone do not override parser behavior.
4. GUI rendering reads synchronized state and cannot alter physics merely by
   changing frame rate or visibility.

## Known non-requirement

The apparent non-FOX inversion in `MSVehicleControl::isPendingRemoval()` is a
source anomaly requiring a test, not an invariant to reproduce.

## Confidence

High for core invariants; specialized devices/controllers may impose additional
contracts.
