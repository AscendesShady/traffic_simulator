# 10. Edge cases and failure behavior

Only cases with source/test support are included. Exact diagnostics may vary by
tool and option.

| Case | Expected handling / decision point | Evidence |
|---|---|---|
| no CLI/config input | meta/help or option validation exits/errors rather than starting an empty simulation | `OptionsCont::processMetaOptions`, `MSFrame::checkOptions`, tool `meta/errors` tests |
| malformed XML | Xerces/SAX handler reports error; build/run aborts unless a specific ignore policy applies | `OptionsIO`, `XMLSubSys`, functional error suites |
| empty/zero-traffic simulation | engine can step/end based on end-time/no-further-vehicles policy; outputs may contain empty intervals | `MSNet::simulationState`, `tests/sumo/basic`/output cases |
| empty/disconnected network | network tool may reject/remove components or serialize according to options; routes can remain unreachable | `NBNodeCont::removeComponents`, netconvert/router error tests |
| invalid/disconnected route | route validity reports missing/forbidden transition; may error, discard or teleport under configured policy | `MSBaseVehicle::hasValidRoute*`, routing and SUMO error tests |
| route valid by edges but no usable lane | insertion/movement fails because lane permission/connection is absent | `MSEdge::allowedLanes/insertVehicle`, `MSLane::succLinkSec` |
| vehicle with no leader | free-speed/lane/link/stop constraints remain; no synthetic physical leader required | CF/lane leader queries and CF tests |
| red signal | upcoming link imposes stop speed; stranded/drive-after-red options cover abnormal cases | `MSLink`, `MSVehicle` drive items, `tests/sumo/junction_model/strandedOnRed` |
| downstream spillback | keep-clear/link logic can block entry despite permissive signal | junction model `avoid_spill_back`, `ignoreKeepClear` tests |
| unavailable departure lane | vehicle remains pending unless max delay, vaporization, abort or invalid start makes failure terminal | `MSInsertionControl::tryInsert` |
| explicit unsafe departure speed/position | lane insertion checks patch or reject according to procedure/check flags | `MSLane::isInsertionSuccess/checkFailure` |
| multiple simultaneous departures | deterministic pending order and current lane state mean earlier commits affect later attempts | `MSInsertionControl::emitVehicles` |
| max vehicle count reached | otherwise valid departure is retried | `MSInsertionControl::tryInsert` |
| flow scale/probability edge values | quota/rounding or per-step RNG determines concrete vehicles; zero scale yields none | `determineCandidates` |
| vehicle arrival during lane iteration | lane/network cleanup schedules deferred removal | `MSVehicleControl::scheduleVehicleRemoval/removePending` |
| parking egress blocked | vehicle remains off-stream/idling and retries safe insertion | `MSVehicleTransfer::checkInsertions` |
| gridlock teleport | vehicle leaves normal occupancy and advances/reinserts through transfer; can remove instead by option | time-to-teleport options, `MSVehicleTransfer` |
| teleport beyond final edge | schedules arrival/removal; taxi branch avoids going past its route | `MSVehicleTransfer::checkInsertions` |
| vehicle collision | action none/warn/teleport/remove, optionally after collision stop | `MSLane::detect/handleCollision*`, collision options |
| pedestrian collision | separate intermodal action/stop options and crossing/walking-area classification | `MSLane::handleIntermodalCollisionBetween` |
| junction collision checks off | normal link arbitration still runs; geometric post-check is not universally enabled | `collision.check-junctions`, `MSLink::opened` |
| simultaneous/continuous lane changes | shadow/reservation/lateral overlap prevent unsafe double occupancy; collision filter handles synchronized case | SL2015/lane changer, `MSLane::detectCollisionBetween` |
| opposite/bidirectional driving | coordinate/leader/collision calculations use opposite-state branches | `MSLane` opposite queries and opposite-direction tests |
| transient lane closure/permission | route/insertion may become invalid; current/transfer vehicles may be stranded or rerouted | `MSLane::setPermissions`, rerouter/TraCI tests |
| missing/invalid TLS program | load/control rejects unknown mapping or uses explicit off/online behavior | `MSTLLogicControl`, TLS error/TraCI tests |
| mesoscopic destination queue full | vehicle leader is rescheduled from segment/block/recheck timing; configured gridlock may teleport/remove it | `MELoop::checkCar`, `MESegment::hasSpaceFor`, meso tests |
| mesoscopic unsupported join stop | warning is emitted and join behavior is not treated as implemented parity | `MEVehicle::checkStop`, `tests/sumo/meso/` |
| cyclic rail wait relation | rail control detects a deadlock cycle; configured rail-deadlock threshold participates in teleport handling | `MSRailSignalControl::haveDeadlock`, `MSLane`, rail tests |
| rail route changes near signal update | begin-step reminder activation refreshes route-dependent driveways before signal evaluation | `MSBaseVehicle::activateRemindersOnReroute`, `MSRailSignalControl::updateSignals` |
| depleted battery or no acceptable station | warning, continued policy, station search, or configured rescue action depends on device settings | `MSDevice_Battery`, `MSDevice_StationFinder`, device tests |
| adjacent charging/wire intervals | previous provider is disconnected and current provider accounts energy without a double charge | battery/electric-hybrid movement callbacks |
| traction demand exceeds supply | coupled circuit limits delivered current/voltage/energy and records the limiting condition | `MSTractionSubstation`, `Circuit`, elechybrid tests |
| state load with time offset | restored times are shifted; static network still must resolve references | `MSStateHandler`, `load-state.offset`, state tests |
| unusual step/action step | formulas/integration and probabilistic flow rate scale with time; dedicated action-step variants test behavior | action-step/ballistic tests |
| remote control causes overlap | remote post-processing has its own staged collision check; some teleport collision action is suppressed for remote-affected vehicles | `MSNet::postMoveStep`, `MSLane::detectCollisionBetween` |
| object disappears while GUI tracks it | GUI must resolve lifecycle asynchronously and stop/update tracking safely | GUI run/view/object registry code |
| optional dependency absent | feature is disabled or adapter unavailable; base headless build can remain usable depending on dependency | top-level CMake feature checks |

## Extreme congestion

Congestion couples insertion retry, lane ordering, junction keep-clear, waiting
time, teleport and routing weights. It must be tested as a multi-step scenario;
isolated formula tests cannot establish correct gridlock behavior.

## Confidence

High for listed decision points; user-visible diagnostic text should be verified
against the exact functional test/configuration when compatibility requires it.
