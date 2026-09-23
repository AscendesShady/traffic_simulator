# Concept-to-source-to-test reference map

| Question / concept | Start here | Then inspect | Behavioral evidence |
|---|---|---|---|
| How does time advance? | `MSNet::simulationStep/postMoveStep` | `SUMOTime.h`, `MSFrame::setMSGlobals` | action-step, state, summary tests |
| How is a vehicle updated? | `MSLane::planMovements/executeMovements` | `MSVehicle::executeMove`, CF/LC/link classes | CF/LC/junction suites |
| What controls acceleration? | `MSCFModel::finalizeSpeed` | selected `MSCFModel_*`, vehicle plan | CF gtests + functional models |
| How does lane changing interact with motion? | `MSLaneChanger*` | LC model, lane buffers/shadow state, vehicle speed patch | LC/sublane/opposite tests |
| How is a road network represented? | `NBNetBuilder`, `NBNode`, `NBEdge` | writers then `NLBuilder`, `MSEdge/MSLane/MSLink` | netconvert + simulation load tests |
| How are junctions represented? | `NBNode::computeLogic*` | serialized requests/connections, runtime junction/`MSLink` | netconvert junction + SUMO junction tests |
| How are signals represented? | `NBTrafficLight*` | `MSTLLogicControl`, logic subclasses, `MSLink` | TLS and TraCI trafficlight |
| How does routing work? | `ROLoader`/`RONet` and generic routers | `MSRoutingEngine`, vehicle route replacement | router suites/rerouting |
| How does a vehicle depart? | `MSInsertionControl` | `MSEdge::insertVehicle`, `MSLane::isInsertionSuccess` | flow/departure/TraCI add |
| How does it arrive/disappear? | vehicle arrival path | `MSVehicleControl::removePending`, devices, transfer | tripinfo/arrival/teleport/state |
| What happens at a conflict? | `MSLink::opened/blockedAtTime` | approach records, vehicle drive items | junction/TLS/pedestrian |
| What happens after a collision? | `MSLane::detect/handleCollision*` | transfer/removal/options | collision-related functional tests |
| How do persons walk/ride? | `MSPerson`, `MSStage*` | pedestrian model, stops/vehicle devices | pedestrian/person/PT tests |
| How does TraCI communicate? | `TraCIServer::processCommands` | API handlers, libsumo domains, constants, clients | protocol/complex TraCI |
| How does libsumo differ? | `libsumo::Simulation` | direct domains/helper versus TraCI transport | testlibsumo and parity cases |
| How does GUI affect simulation? | `GUIRunThread`, `GUINet` | GUI subclasses/view locks/commands | GUI and TraCI GUI tests |
| How does Netedit differ from the runtime GUI? | `GNENet`, `GNEViewNet` | `NBNetBuilder`, `GNEChange`, writers | `tests/netedit/` |
| How are devices and detectors updated? | `MSDevice::buildVehicleDevices`, `MSDetectorControl` | move reminders and `MSNet::writeOutput` | device/output/state suites |
| How does mesoscopic traffic advance? | `MELoop::simulate`, `checkCar` | `MESegment::hasSpaceFor`, `send`, `receive`; `MEVehicle` | `tests/sumo/meso/` |
| How are rail conflicts reserved? | `MSRailSignalControl::updateSignals` | `MSRailSignal`, `MSDriveWay`, constraints and wait relations | `tests/sumo/rail/` |
| How is electric energy supplied? | battery/electric-hybrid movement callbacks | charging stations, station finder, overhead wire and traction circuit | battery/elechybrid/stationfinder and TraCI suites |
| How are external networks normalized? | `NILoader::load` | importer, `NBNetBuilder::compute`, writer | netconvert import/export/round-trip suites |
| How are checkpoints restored? | `MSStateHandler` | each owner's save/load/clear methods, RNG | `tests/complex/state/` |
| Where do options come from? | owning `*Frame::fillOptions` | `OptionsIO`, `OptionsCont`, consumer | meta/write_config/errors |
| What can be replaced? | `14_REIMPLEMENTATION_SPEC.md` | invariants/change impact and relevant subsystem | acceptance matrix in testing doc |

## Navigation rule

For any change, follow `concept -> orchestration function -> state owner ->
format/API boundary -> focused tests`. A class name or filename alone is not
enough evidence for behavioral purpose.

## Confidence

High.
