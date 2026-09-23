# Architectural class map

| Concept | Class/type | Responsibility | Source |
|---|---|---|---|
| simulation world | `MSNet` | owns clock/controllers and step order | `src/microsim/MSNet.h` |
| runtime load | `NLBuilder` | initializes options/world and loads network/scenario | `src/netload/NLBuilder.h` |
| runtime SAX dispatch | `NLHandler` | handles network/additional XML elements | `src/netload/NLHandler.h` |
| edge scheduling | `MSEdgeControl` | active lanes, plan/move/lane-change phases | `src/microsim/MSEdgeControl.h` |
| runtime edge | `MSEdge` | lanes, successors, lane changer and routing data | `src/microsim/MSEdge.h` |
| runtime lane | `MSLane` | ordered occupancy, insertion, movement, collision | `src/microsim/MSLane.h` |
| connection | `MSLink` | target/via lane, signal, foes and approaches | `src/microsim/MSLink.h` |
| junction base | `MSJunction` | junction identity/geometry contract | `src/microsim/MSJunction.h` |
| right-of-way junction | `MSRightOfWayJunction` | initializes request/foe runtime logic | `src/microsim/MSRightOfWayJunction.h` |
| internal junction | `MSInternalJunction` | internal-lane continuation conflicts | `src/microsim/MSInternalJunction.h` |
| vehicle base | `MSBaseVehicle` | route, parameters, stops, devices | `src/microsim/MSBaseVehicle.h` |
| microscopic vehicle | `MSVehicle` | motion plan/execution and detailed state | `src/microsim/MSVehicle.h` |
| vehicle type | `MSVehicleType` | physical/model/type parameters | `src/microsim/MSVehicleType.h` |
| fleet owner | `MSVehicleControl` | vehicle dictionary/lifecycle/counters | `src/microsim/MSVehicleControl.h` |
| insertion | `MSInsertionControl` | future/pending demand and flows | `src/microsim/MSInsertionControl.h` |
| off-road transfer | `MSVehicleTransfer` | teleport/parking/jump progression | `src/microsim/MSVehicleTransfer.h` |
| route | `MSRoute` | immutable runtime edge sequence/dictionary | `src/microsim/MSRoute.h` |
| runtime rerouting | `MSRoutingEngine` | weights/router workers and reroute dispatch | `src/microsim/devices/MSRoutingEngine.h` |
| CF interface | `MSCFModel` | longitudinal model contract/helpers | `src/microsim/cfmodels/MSCFModel.h` |
| default CF | `MSCFModel_Krauss` | Krauss family behavior | `src/microsim/cfmodels/MSCFModel_Krauss.h` |
| IDM CF | `MSCFModel_IDM` | IDM behavior | `src/microsim/cfmodels/MSCFModel_IDM.h` |
| LC interface | `MSAbstractLaneChangeModel` | per-vehicle LC state/contract | `src/microsim/lcmodels/MSAbstractLaneChangeModel.h` |
| default LC | `MSLCM_LC2013` | discrete strategic/cooperative LC | `src/microsim/lcmodels/MSLCM_LC2013.h` |
| sublane LC | `MSLCM_SL2015` | continuous lateral model | `src/microsim/lcmodels/MSLCM_SL2015.h` |
| LC orchestrator | `MSLaneChanger` | scans/commits discrete lane changes | `src/microsim/MSLaneChanger.h` |
| TLS owner | `MSTLLogicControl` | TLS IDs, variants, active programs/switches | `src/microsim/traffic_lights/MSTLLogicControl.h` |
| TLS base | `MSTrafficLightLogic` | mapped links and controller contract | `src/microsim/traffic_lights/MSTrafficLightLogic.h` |
| fixed TLS | `MSSimpleTrafficLightLogic` | phase sequence/time mapping | `src/microsim/traffic_lights/MSSimpleTrafficLightLogic.h` |
| actuated TLS | `MSActuatedTrafficLightLogic` | detectors and min/max/gap control | `src/microsim/traffic_lights/MSActuatedTrafficLightLogic.h` |
| event queue | `MSEventControl` | timed command execution/rescheduling | `src/microsim/MSEventControl.h` |
| device base | `MSDevice` | optional device construction/options | `src/microsim/devices/MSDevice.h` |
| vehicle device | `MSVehicleDevice` | vehicle-attached lifecycle/output | `src/microsim/devices/MSVehicleDevice.h` |
| move reminder | `MSMoveReminder` | passage/lifecycle notification interface | `src/microsim/MSMoveReminder.h` |
| detector owner | `MSDetectorControl` | detector registry, updates and interval writes | `src/microsim/output/MSDetectorControl.h` |
| mean-data aggregation | `MSMeanData` | edge/lane interval value trackers and serialization | `src/microsim/output/MSMeanData.h` |
| person | `MSPerson` | person plan and state | `src/microsim/transportables/MSPerson.h` |
| walking stage | `MSStageWalking` | walking route/progress | `src/microsim/transportables/MSStageWalking.h` |
| pedestrian interface | `MSPModel` | operational pedestrian model contract | `src/microsim/transportables/MSPModel.h` |
| transportable owner | `MSTransportableControl` | persons/containers scheduling/lifetime | `src/microsim/transportables/MSTransportableControl.h` |
| stopping place | `MSStoppingPlace` | stop geometry/access/occupancy base | `src/microsim/MSStoppingPlace.h` |
| state persistence | `MSStateHandler` | checkpoint parsing/writing coordination | `src/microsim/MSStateHandler.h` |
| meso loop | `MELoop` | mesoscopic event/segment advance | `src/mesosim/MELoop.h` |
| meso segment | `MESegment` | queue-based edge segment state | `src/mesosim/MESegment.h` |
| meso LTM segment | `MELSegment` | link-transmission headway and admission overrides | `src/mesosim/MELSegment.h` |
| meso vehicle | `MEVehicle` | route, stop, queue and event-time state | `src/mesosim/MEVehicle.h` |
| rail signal coordinator | `MSRailSignalControl` | signals, wait relations and deadlock checks | `src/microsim/traffic_lights/MSRailSignalControl.h` |
| route-dependent rail signal | `MSRailSignal` | driveways, constraints and derived link phases | `src/microsim/traffic_lights/MSRailSignal.h` |
| rail driveway | `MSDriveWay` | route occupancy, foes and flank protection | `src/microsim/traffic_lights/MSDriveWay.h` |
| vehicle battery | `MSDevice_Battery` | energy/charge state and station interaction | `src/microsim/devices/MSDevice_Battery.h` |
| charging station finder | `MSDevice_StationFinder` | charging-stop scoring, rerouting and rescue | `src/microsim/devices/MSDevice_StationFinder.h` |
| electric hybrid device | `MSDevice_ElecHybrid` | onboard/wire energy exchange | `src/microsim/devices/MSDevice_ElecHybrid.h` |
| overhead wire | `MSOverheadWire` | lane supply interval and charge output | `src/microsim/trigger/MSOverheadWire.h` |
| traction substation | `MSTractionSubstation` | shared supply, circuit ownership and limits | `src/microsim/trigger/MSOverheadWire.h` |
| traction circuit | `Circuit` | nonlinear electrical network construction and solution | `src/utils/traction_wire/Circuit.h` |
| build network | `NBNetBuilder` | owns build-time containers/compute pipeline | `src/netbuild/NBNetBuilder.h` |
| build node | `NBNode` | topology/junction geometry/logic | `src/netbuild/NBNode.h` |
| build edge | `NBEdge` | lanes/connections/geometry/permissions | `src/netbuild/NBEdge.h` |
| import dispatcher | `NILoader` | invokes configured importers | `src/netimport/NILoader.h` |
| canonical network writer | `NWWriter_SUMO` | writes runtime `.net.xml` from `NB*` state | `src/netwrite/NWWriter_SUMO.h` |
| generated graph | `NGNet` | grid/spider topology and NB conversion | `src/netgen/NGNet.h` |
| random generator | `NGRandomNetBuilder` | constrained randomized graph growth | `src/netgen/NGRandomNetBuilder.h` |
| routing network | `RONet` | offline routing graph/demand output | `src/router/RONet.h` |
| routing edge | `ROEdge` | cost, permissions and successors | `src/router/ROEdge.h` |
| route loader | `ROLoader` | network/weights/demand streaming | `src/router/ROLoader.h` |
| Dijkstra | `DijkstraRouter` | generic shortest-path router | `src/utils/router/DijkstraRouter.h` |
| A* | `AStarRouter` | heuristic shortest-path router | `src/utils/router/AStarRouter.h` |
| CH | `CHRouter` | contraction-hierarchy router | `src/utils/router/CHRouter.h` |
| options registry | `OptionsCont` | global typed option definitions/values | `src/utils/options/OptionsCont.h` |
| TraCI server | `TraCIServer` | connection, command loop, subscriptions | `src/traci-server/TraCIServer.h` |
| direct API | `libsumo::Simulation` | lifecycle/step/query domain | `src/libsumo/Simulation.h` |
| remote C++ connection | `libtraci::Connection` | socket command/reply transport | `src/libtraci/Connection.h` |
| GUI world | `GUINet` | GUI-aware `MSNet` and view updates | `src/guisim/GUINet.h` |
| GUI runner | `GUIRunThread` | pause/run/single-step scheduling | `src/gui/GUIRunThread.h` |
| GUI view | `GUIViewTraffic` | traffic rendering and interaction | `src/gui/GUIViewTraffic.h` |
| editor application | `GNEApplicationWindow` | Netedit application/file/command lifecycle | `src/netedit/GNEApplicationWindow.h` |
| editor model | `GNENet` | editable network/demand/additional/data ownership | `src/netedit/GNENet.h` |
| editor view | `GNEViewNet` | edit-mode interaction and rendering dispatch | `src/netedit/GNEViewNet.h` |
| editor history | `GNEUndoList` | grouped reversible mutations | `src/netedit/GNEUndoList.h` |

## Confidence

High — every listed path is validated by repository QA; the map prioritizes
architectural symbols rather than completeness.
