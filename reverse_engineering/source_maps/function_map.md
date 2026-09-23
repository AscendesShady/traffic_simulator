# Architectural function map

| Behavior | Function/method | Source |
|---|---|---|
| headless entry | `main` | `src/sumo_main.cpp` |
| runtime initialization | `NLBuilder::init` | `src/netload/NLBuilder.cpp` |
| runtime load/build | `NLBuilder::build`, `buildNet` | `src/netload/NLBuilder.cpp` |
| run loop | `MSNet::simulate` | `src/microsim/MSNet.cpp` |
| one step | `MSNet::simulationStep` | `src/microsim/MSNet.cpp` |
| post-move/time advance | `MSNet::postMoveStep` | `src/microsim/MSNet.cpp` |
| shutdown output | `MSNet::closeSimulation` | `src/microsim/MSNet.cpp` |
| per-step output dispatch | `MSNet::writeOutput` | `src/microsim/MSNet.cpp` |
| stream routes | `MSNet::loadRoutes` | `src/microsim/MSNet.cpp` |
| lane planning dispatch | `MSEdgeControl::planMovements` | `src/microsim/MSEdgeControl.cpp` |
| movement dispatch | `MSEdgeControl::executeMovements` | `src/microsim/MSEdgeControl.cpp` |
| lane-change dispatch | `MSEdgeControl::changeLanes` | `src/microsim/MSEdgeControl.cpp` |
| lane plan | `MSLane::planMovements` | `src/microsim/MSLane.cpp` |
| approach publish | `MSLane::setJunctionApproaches` | `src/microsim/MSLane.cpp` |
| lane move execution | `MSLane::executeMovements` | `src/microsim/MSLane.cpp` |
| buffer integration | `MSLane::integrateNewVehicles` | `src/microsim/MSLane.cpp` |
| vehicle motion commit | `MSVehicle::executeMove` | `src/microsim/MSVehicle.cpp` |
| route replacement | `MSBaseVehicle::replaceRouteEdges`, `replaceRoute` | `src/microsim/MSBaseVehicle.cpp` |
| route validity | `MSBaseVehicle::hasValidRoute*` | `src/microsim/MSBaseVehicle.cpp` |
| common speed finalize | `MSCFModel::finalizeSpeed` | `src/microsim/cfmodels/MSCFModel.cpp` |
| braking distance | `MSCFModel::brakeGap` | `src/microsim/cfmodels/MSCFModel.cpp` |
| departure expansion | `MSInsertionControl::determineCandidates` | `src/microsim/MSInsertionControl.cpp` |
| emit pending | `MSInsertionControl::emitVehicles`, `tryInsert` | `src/microsim/MSInsertionControl.cpp` |
| edge lane selection | `MSEdge::insertVehicle` | `src/microsim/MSEdge.cpp` |
| lane insertion check | `MSLane::insertVehicle`, `isInsertionSuccess` | `src/microsim/MSLane.cpp` |
| insertion commit | `MSLane::incorporateVehicle` | `src/microsim/MSLane.cpp` |
| departure counters | `MSVehicleControl::vehicleDeparted` | `src/microsim/MSVehicleControl.cpp` |
| deferred removal | `MSVehicleControl::scheduleVehicleRemoval`, `removePending` | `src/microsim/MSVehicleControl.cpp` |
| final delete | `MSVehicleControl::deleteVehicle` | `src/microsim/MSVehicleControl.cpp` |
| leave network | `MSVehicle::onRemovalFromNet` | `src/microsim/MSVehicle.cpp` |
| transfer start | `MSVehicleTransfer::add` | `src/microsim/MSVehicleTransfer.cpp` |
| transfer progression | `MSVehicleTransfer::checkInsertions` | `src/microsim/MSVehicleTransfer.cpp` |
| link approach add/remove | `MSLink::setApproaching`, `removeApproaching` | `src/microsim/MSLink.cpp` |
| link admission | `MSLink::opened`, `blockedAtTime`, `blockedByFoe` | `src/microsim/MSLink.cpp` |
| conflict leaders | `MSLink::getLeaderInfo` | `src/microsim/MSLink.cpp` |
| attach vehicle devices | `MSDevice::buildVehicleDevices` | `src/microsim/devices/MSDevice.cpp` |
| update detectors | `MSDetectorControl::updateDetectors` | `src/microsim/output/MSDetectorControl.cpp` |
| write detector intervals | `MSDetectorControl::writeOutput` | `src/microsim/output/MSDetectorControl.cpp` |
| collision detect | `MSLane::detectCollisions`, `detectCollisionBetween` | `src/microsim/MSLane.cpp` |
| collision action | `MSLane::handleCollisionBetween`, `handleIntermodalCollisionBetween` | `src/microsim/MSLane.cpp` |
| TLS global switch | `MSTLLogicControl::check2Switch` | `src/microsim/traffic_lights/MSTLLogicControl.cpp` |
| fixed phase switch | `MSSimpleTrafficLightLogic::trySwitch` | `src/microsim/traffic_lights/MSSimpleTrafficLightLogic.cpp` |
| actuated decision | `MSActuatedTrafficLightLogic::trySwitch`, `decideNextPhase` | `src/microsim/traffic_lights/MSActuatedTrafficLightLogic.cpp` |
| meso due-event loop | `MELoop::simulate`, `checkCar` | `src/mesosim/MELoop.cpp` |
| meso segment transition | `MELoop::changeSegment`, `MESegment::send`, `receive` | `src/mesosim/MELoop.cpp`, `src/mesosim/MESegment.cpp` |
| meso capacity admission | `MESegment::hasSpaceFor`, `hasSpaceForInsertion` | `src/mesosim/MESegment.cpp` |
| meso teleport | `MELoop::teleportVehicle` | `src/mesosim/MELoop.cpp` |
| rail signal update | `MSRailSignalControl::updateSignals`, `MSRailSignal::updateCurrentPhase` | `src/microsim/traffic_lights/MSRailSignalControl.cpp`, `MSRailSignal.cpp` |
| rail deadlock test | `MSRailSignalControl::addWaitRelation`, `haveDeadlock` | `src/microsim/traffic_lights/MSRailSignalControl.cpp` |
| battery movement accounting | `MSDevice_Battery::notifyMoveInternal` | `src/microsim/devices/MSDevice_Battery.cpp` |
| station selection/rerouting | `MSDevice_StationFinder::findChargingStation`, `rerouteToChargingStation` | `src/microsim/devices/MSDevice_StationFinder.cpp` |
| overhead-wire energy exchange | `MSDevice_ElecHybrid::notifyMoveInternal`, `computeChargedEnergy` | `src/microsim/devices/MSDevice_ElecHybrid.cpp` |
| traction supply solution | `MSTractionSubstation::solveCircuit`, `Circuit::solve` | `src/microsim/trigger/MSOverheadWire.cpp`, `src/utils/traction_wire/Circuit.cpp` |
| build-time normalization | `NBNetBuilder::compute` | `src/netbuild/NBNetBuilder.cpp` |
| lane connections | `NBNode::computeLanes2Lanes` | `src/netbuild/NBNode.cpp` |
| junction logic | `NBNode::computeLogic`, `computeLogic2` | `src/netbuild/NBNode.cpp` |
| junction shape | `NBNode::computeNodeShape`, `computeInternalLaneShape` | `src/netbuild/NBNode.cpp` |
| import dispatch | `NILoader::load` | `src/netimport/NILoader.cpp` |
| writer dispatch | `NWFrame::writeNetwork` | `src/netwrite/NWFrame.cpp` |
| SUMO network write | `NWWriter_SUMO::writeNetwork` | `src/netwrite/NWWriter_SUMO.cpp` |
| grid generation | `NGNet::createChequerBoard` | `src/netgen/NGNet.cpp` |
| spider generation | `NGNet::createSpiderWeb` | `src/netgen/NGNet.cpp` |
| NG to NB | `NGNet::toNB` | `src/netgen/NGNet.cpp` |
| random graph | `NGRandomNetBuilder::createNet`, `createNewNode` | `src/netgen/NGRandomNetBuilder.cpp` |
| duarouter dispatch | `computeRoutes` | `src/duarouter/duarouter_main.cpp` |
| route processing | `ROLoader::processRoutes` | `src/router/ROLoader.cpp` |
| CLI/config parse | `OptionsIO::getOptions`, `loadConfiguration` | `src/utils/options/OptionsIO.cpp` |
| option metadata/value | `OptionsCont::doRegister`, `set`, typed getters | `src/utils/options/OptionsCont.cpp` |
| TraCI command loop | `TraCIServer::processCommands` | `src/traci-server/TraCIServer.cpp` |
| libsumo step | `libsumo::Simulation::step`, `executeMove` | `src/libsumo/Simulation.cpp` |
| libtraci step | `libtraci::Connection::simulationStep` | `src/libtraci/Connection.cpp` |
| GUI load | `GUILoadThread::run` | `src/gui/GUILoadThread.cpp` |
| GUI simulation step | `GUIRunThread::makeStep` | `src/gui/GUIRunThread.cpp` |
| GUI render | `GUIViewTraffic::doPaintGL` | `src/gui/GUIViewTraffic.cpp` |
| Netedit load | `GNELoadThread::run` | `src/netedit/GNELoadThread.cpp` |
| Netedit recompute | `GNENet::computeNetwork` | `src/netedit/GNENet.cpp` |
| Netedit save | `GNENet::saveNetwork` | `src/netedit/GNENet.cpp` |
| Netedit change registration | `GNEUndoList::add` | `src/netedit/GNEUndoList.cpp` |

## Confidence

High for symbol/file mapping. Overloads and subordinate helpers are omitted.
