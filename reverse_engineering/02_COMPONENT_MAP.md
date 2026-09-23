# 02. Component map

| Component | Purpose / state owned | Primary source | Main consumers | Tests |
|---|---|---|---|---|
| common options/XML/time | typed configuration, parsing, time and shared primitives | `src/utils/options/`, `src/utils/xml/`, `src/utils/common/` | all executables | meta/error suites; utility gtests |
| netimport | convert source formats into `NB*` | `src/netimport/` | netconvert | `tests/netconvert/` |
| netgen | grid/spider/random temporary `NG*` graph | `src/netgen/` | netgenerate | `tests/netgen/` |
| netbuild | mutable normalized build-time graph/control definitions | `src/netbuild/` | netconvert/netgenerate/netedit/writers | netconvert; limited gtests |
| netwrite | serialize SUMO and other network formats | `src/netwrite/` | network tools | netconvert/netgen expected files |
| netload | parse simulation inputs and build runtime controls | `src/netload/` | sumo, GUI, libsumo | all SUMO startup/error tests |
| runtime coordinator | clock, step order, global controls | `src/microsim/MSNet.*` | sumo, GUI, APIs | `tests/sumo/`, state/TraCI |
| lanes/edges | topology, occupancy, movement orchestration | `src/microsim/MSLane.*`, `MSEdge.*`, `MSEdgeControl.*` | vehicles, detectors, APIs | movement/lane/junction suites |
| vehicles/types | per-vehicle route/motion/stop/device state and fleet ownership | `MSBaseVehicle.*`, `MSVehicle.*`, `MSVehicleType.*`, `MSVehicleControl.*` | lanes, routing, interfaces | broad SUMO/TraCI tests |
| car-following | longitudinal safe/desired speeds | `src/microsim/cfmodels/` | vehicle planning | `tests/sumo/cf_model/`, 2 gtests |
| lane-changing | lateral decision and commit | `src/microsim/lcmodels/`, `MSLaneChanger*` | edge/lane movement | LC/sublane/opposite suites |
| junction/link | movement conflicts and link admission | `MSLink.*`, `MS*Junction.*` | vehicles/TLS/pedestrians | junction model/TLS tests |
| traffic lights | road programs, phases and detector-based switching | `src/microsim/traffic_lights/` | links, API, GUI | TLS/TraCI tests |
| rail control | route-dependent driveways, rail signals/crossings, constraints and deadlock relations | `src/microsim/traffic_lights/MSRail*`, `MSDriveWay.*` | rail vehicles, links, API, GUI | `tests/sumo/rail/` |
| insertion | future/pending demand and safe departure | `MSInsertionControl.*` | MSNet, route loader | flow/departure/TraCI tests |
| transfer/removal | deferred deletion, teleport, parking reinsertion | `MSVehicleControl.*`, `MSVehicleTransfer.*` | MSNet, outputs | arrival/teleport/state tests |
| persons/containers | staged multimodal plans | `src/microsim/transportables/` | PT, pedestrian, API | pedestrian/person/PT tests |
| devices | optional vehicle behavior/measurement | `src/microsim/devices/` | vehicles, output, routing | `tests/sumo/devices/` |
| detectors/output | measurement and serialization | `src/microsim/output/` | files, TLS, API | output/detector suites |
| mesoscopic | event-driven segment/queue traffic resolution | `src/mesosim/` | MSNet alternate path, mesogui | `tests/sumo/meso/` |
| electric supply | batteries, station selection/charging, overhead wires and traction circuits | `src/microsim/devices/MSDevice_{Battery,ElecHybrid,StationFinder}.*`, `src/microsim/trigger/MS{ChargingStation,OverheadWire}.*` | vehicles, routing, output, API | battery/elechybrid/stationfinder suites |
| routing domain | routing graph, demand parsing/output | `src/router/` | router executables | router suites |
| generic routers | Dijkstra/A*/CH/intermodal templates | `src/utils/router/` | routing tools and runtime rerouting | router/rerouting functional tests |
| TraCI server | wire protocol and stepping gate | `src/traci-server/` | remote clients | `tests/traci/`, complex TraCI |
| libsumo | direct in-process domain API | `src/libsumo/` | bindings, FMI, applications | libsumo/TraCI parity tests |
| libtraci/Python TraCI | remote clients | `src/libtraci/`, `tools/traci/` | external applications | protocol/language suites |
| GUI | load/run lifecycle and interaction | `src/gui/`, `src/guisim/`, `src/guinetload/` | sumo-gui users | GUI/TraCI GUI tests |
| netedit | GUI editing over netbuild/demand/additional models | `src/netedit/` | network authors | `tests/netedit/` |
| Python ecosystem | analysis, conversion, orchestration | `tools/`, especially `tools/sumolib/` | users, tests, and workflows | `tests/tools/` plus scenarios under `tests/complex/` |

## Interaction hotspots

| Hotspot | Components that meet | Why risky |
|---|---|---|
| `MSNet::simulationStep()` | every runtime control | ordering changes alter results |
| `MSVehicle::planMove/executeMove` | CF, LC, links, stops, devices, route | most motion semantics converge here |
| `MSLink` | network topology, junctions, TLS, pedestrians, vehicle planning | indices/timing/conflicts must agree |
| `MSLane` containers | insertion, movement, LC, collision, GUI, detectors | ownership/order/concurrency |
| `NBNetBuilder::compute()` | import, topology, geometry, TLS, writers | pass-order and index invalidation |
| libsumo domain classes | engine, TraCI handlers, SWIG bindings | public compatibility surface |
| XML constants/schemas | parsers, writers, tools, clients | distributed protocol/file contract |

## Confidence

High at component level. Refer to detailed pages for algorithm-specific
confidence.
