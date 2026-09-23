# 05. State model

## State ownership principles

- `MSNet` owns the simulation clock and major controls.
- `MSVehicleControl` owns vehicle object lifetime; lanes reference active
  vehicles and impose spatial order.
- Static network topology is effectively immutable after load, apart from
  explicit runtime controls such as permissions/speed/closures.
- Derived caches are not independent truth; they must be invalidated/rebuilt
  after the state they summarize changes.
- GUI/client subscription state is not authoritative traffic state.

## Core state contract

| Name | Owner / type | Meaning and units | Initial state / update | Readers | Lifetime / constraints / side effects |
|---|---|---|---|---|---|
| current step `myStep` | `MSNet`, `SUMOTime` | simulation clock, integer time steps (normally ms representation) | set to begin/restored time; incremented by `DELTA_T` after complete step | all scheduled/runtime systems | simulation lifetime; monotonic except explicit reload/state initialization |
| step length `DELTA_T` | global time config | fixed integration step | set from options by `MSFrame::setMSGlobals()` | motion, flows, events, outputs | run-wide; changing it changes numerical behavior |
| step completion flag | `MSNet`, bool | whether move-only step awaits post processing | false; set by `simulationStep(true)`, cleared by completion | libsumo/TraCI stepping | never advance time/output twice |
| event queues | `MSNet`/`MSEventControl` | begin, insertion and end commands keyed by time | built during load; execute/reschedule by queue | engine/subsystems | queue placement is ordering contract |
| vehicle dictionary | `MSVehicleControl`, ID→vehicle | all loaded/live/transfer/kept vehicles | add on build, erase on delete | API, insertion, controls | IDs unique; owns lifetime |
| vehicle lifecycle counters | `MSVehicleControl` | loaded, running, ended, discarded, teleport and aggregate stats | changed at build/depart/removal | termination, summary, API/output | each transition counted once |
| vehicle motion state | `MSVehicle::State` | position (m), speed (m/s), plus prior state | initialized at insertion/state load; updated in `executeMove()` | CF/LC/output/API/devices | consistent with active lane and completed timestep |
| acceleration | `MSVehicle`/derived | speed change rate, m/s² | updated from current/previous speed/integration | output, emissions, devices | agrees with integration mode/step |
| route pointer/index | `MSBaseVehicle` | route edge sequence and current edge offset | set at build; advances on crossing; replaced on reroute | lane/link selection, stop logic, API | current lane edge matches route position except explicit teleport/opposite cases |
| planned drive items | `MSVehicle` vector | upcoming links, distances, speeds/times | recomputed during plan movement | approach registration, execution | predictions removed/rebuilt when route/motion changes |
| vehicle lane membership | `MSVehicle` + `MSLane` refs | current lane and optional further/shadow lanes | commit insertion, movement/LC, removal | all motion/GUI/API | bidirectionally consistent with lane containers |
| lane vehicle order | `MSLane` container | vehicles ordered by longitudinal position | insertion/buffer integration/LC/removal | leaders, movement, collision, output | sorted; no duplicate full membership |
| partial/reservation state | `MSLane` + LC model | overlap/shadow/target occupancy | continuous LC and long vehicle crossings | leader/collision/GUI | cleared on maneuver completion/removal |
| lane permissions/speed/friction | `MSLane` | current legal classes and physical/regulatory limits | loaded, optionally changed by triggers/TraCI | insertion/routing/motion | changes invalidate relevant caches and may strand vehicles |
| edge active state | `MSEdgeControl`/`MSEdge` | whether lanes require per-step work, plus caches | patched each step and on insert/movement | step scheduler | active set must include all lanes with work |
| link state | `MSLink` | signal/priority aspect for a connection | initialized from network, set by TLS | vehicle/junction/GUI/API | link index mapping invariant |
| link approaches | `MSLink` map | predicted arrival/leave of vehicles/persons | installed in planning, removed on pass/replan/removal | foe arbitration | only live/current plans; no stale pointers |
| TLS active program/phase | `MSTLLogicControl` + logic | program ID, phase index, last/next switch, detector/controller state | loaded and switched before movement | links/API/GUI/state | phase state length matches mapped links |
| mesoscopic due events | `MELoop`, priority queues | queue leaders keyed by next event time and stable tie order | added/invalidated by segment transitions; drained through current step | mesoscopic loop, links | nondecreasing execution; invalidated copies never execute |
| mesoscopic segment queues | `MESegment`/`Queue` | permitted ordered vehicles, occupied length, speed, capacity and entry/block times | built with network; updated by initialise/send/receive/removal/state load | meso admission, detectors, GUI/API | one normal queue per vehicle; occupancy and membership agree |
| rail driveway/reservation | `MSRailSignal`/`MSDriveWay` | route-specific protected path, occupancy, foes/flank state and derived link phase | built/refreshed from rail routes and reminders; updated before road TLS switching | rail link admission, GUI/API | current route/reminders and protected path remain consistent |
| rail wait/constraint state | `MSRailSignalControl`/`MSRailSignalConstraint` | per-step wait graph, deadlock checks, trip precedence and passed trackers | signal/insertion evaluation; reset or persisted according to owner | admission, rail teleport, TraCI, state | stale wait edges do not cross their defined step boundary |
| future/pending departures | `MSInsertionControl` | scheduled vehicles, retries, flows/counters/RNG | route load/flow expansion/emission | insertion/termination/API | on-road vehicle no longer pending |
| transfer list | `MSVehicleTransfer` | off-road teleport/parking/jump vehicle and next attempt/proceed time | add on transfer; remove on reinsertion/removal | per-step transfer phase/state | vehicle remains globally owned and route-valid |
| person/container active stage | object + `MSTransportableControl` | exactly one current plan stage and location | depart, stage transitions, arrival | pedestrian/PT/API | ownership/location transitions atomic |
| detector aggregates | detector + `MSDetectorControl` | passage/state samples over interval | reminders/step updates, reset on interval close | TLS/output/API | interval bounds and vehicle notifications consistent |
| battery/charging state | `MSDevice_Battery`, `MSChargingStation`, `MSDevice_StationFinder` | stored/consumed/regenerated/delivered energy, charge timer/rate/type, chosen station/strategy | movement callbacks, station interval, rerouting/rescue, state load | propulsion accounting, tripinfo, charging output/API | bounded capacity; each interval accounted once; station compatibility enforced |
| overhead-wire circuit state | `MSDevice_ElecHybrid`, `MSOverheadWire`, `MSTractionSubstation`, `Circuit` | vehicle demand, active wire, current, voltage, delivered energy and limit reason | vehicle movement and coupled supply solution | battery, wire/substation output/API | current connection matches position; shared supply limits couple vehicles |
| RNG streams | `RandHelper`, lane/flow/parser owners | pseudorandom state | seed/start or state restore; advanced by draws | demand/models/generation | call order and stream selection affect reproducibility |
| router weights/cache | routing engine/storage | time/effort per edge and preprocessed paths | load/update/reroute | vehicles/persons/routing APIs | invalidate preprocessing when unsupported weights change |
| output-device state | `OutputDevice`/writers/devices | open XML tags, intervals, pending summaries | init/write/close | files | close exactly once; unfinished entities handled at shutdown |
| GUI run/view state | GUI window/run thread/views | pause/run delay, camera, selection, snapshots | user/events | renderer | separate from physics; synchronized access required |

## Static versus dynamic topology

`MSEdge`, `MSLane`, `MSLink` and junction relationships originate in network
XML and are normally stable. Dynamic speed, permissions, rerouters, signal
states and closures modify behavior without rebuilding topology. Structural
edits belong in netbuild/netedit followed by reload.

## Checkpoint requirements

A resumable snapshot must restore current time, vehicle/person/container state,
lane placement/order, routes/stops, insertion flows/pending demand, transfer
state, TLS programs/timing, devices/detectors where supported, and RNG streams.
It also requires the compatible static network and option semantics.

## State mutation boundaries

| Mutation | Commit point |
|---|---|
| departure | `MSLane::incorporateVehicle` plus `vehicleDeparted` |
| longitudinal move | `MSVehicle::executeMove` and lane buffer integration |
| lane change | `MSLaneChanger*` plus lane/shadow updates |
| link plan | `MSLink::setApproaching` |
| arrival | schedule, then `MSVehicleControl::removePending` |
| signal change | logic switch then mapped `MSLink::setTLState` |
| mesoscopic segment move | paired `MESegment::send`/`receive`, then next leader event |
| rail route change | `MSBaseVehicle::replaceRoute*`, then begin-step reminder/driveway refresh |
| battery/wire energy | device movement callback plus station or circuit accounting |
| reroute | `MSBaseVehicle::replaceRoute*` after validity checks |

## Confidence

High for listed core state and ownership boundaries. Numerical internals of
individual energy/device models remain delegated to focused source/docs.
