# 06. Module index

## Native source modules

| Module | Responsibility | Key entry points |
|---|---|---|
| `src/microsim` | microscopic runtime core | `MSNet`, `MSVehicle`, `MSLane`, `MSEdge`, `MSLink` |
| `src/microsim/cfmodels` | car-following models | `MSCFModel`, `MSCFModel_*` |
| `src/microsim/lcmodels` | lane-change decisions | `MSAbstractLaneChangeModel`, `MSLCM_*` |
| `src/microsim/traffic_lights` | runtime TLS/rail signal controllers | `MSTLLogicControl`, `MSTrafficLightLogic` |
| `src/microsim/transportables` | persons, containers and plan stages | `MSTransportableControl`, `MSPerson`, `MSStage*`, `MSPModel*` |
| `src/microsim/devices` | optional attached vehicle behavior/output | `MSDevice`, `MSVehicleDevice`, `MSDevice_*` |
| `src/microsim/output` | detectors and simulation exporters | `MSDetectorControl`, `MSMeanData*`, `MSFCDExport`, etc. |
| `src/microsim/trigger` | rerouters, calibrators, stops/charging/wires | `MSTriggeredRerouter`, `MSLaneSpeedTrigger`, etc. |
| `src/microsim/actions` | scheduled output/control commands | `Command_Save*` |
| `src/microsim/engine` | engine models/utilities | engine-specific classes |
| `src/microsim/logging` | function-binding helpers used by runtime logging | `FunctionBinding`, `CastingFunctionBinding` |
| `src/mesosim` | mesoscopic segment/queue model | `MELoop`, `MESegment`, `MEVehicle` |
| `src/mesogui` | GUI representations for mesoscopic state | `GUIMEVehicle`, `GUIMEVehicleControl`, `GUIMEInductLoop` |
| `src/netload` | runtime XML loading/builders | `NLBuilder`, `NLHandler`, `NLEdgeControlBuilder` |
| `src/netbuild` | normalized build-time network | `NBNetBuilder`, `NBNode`, `NBEdge`, containers |
| `src/netimport` | external/plain network import | `NILoader`, `NIImporter_*` |
| `src/netwrite` | network serialization/export | `NWFrame`, `NWWriter_*` |
| `src/netgen` | synthetic network creation | `NGNet`, `NGRandomNetBuilder` |
| `src/router` | routing graph/demand common layer | `RONet`, `ROEdge`, `ROLoader`, `RORouteDef` |
| `src/duarouter` | shortest-path route assignment executable | `duarouter_main.cpp`, `RODUAFrame` |
| `src/jtrrouter` | junction-turn-ratio routing | `jtrrouter_main.cpp`, `ROJTR*` |
| `src/dfrouter` | detector-flow route reconstruction | `dfrouter_main.cpp`, `RODF*` |
| `src/marouter` | macro assignment | `marouter_main.cpp`, `ROMA*` |
| `src/od` | OD matrix loading/conversion | `ODMatrix`, `ODDistrict*` |
| `src/activitygen` | activity/population demand generation | city/activity submodules |
| `src/polyconvert` | polygon/POI import conversion | `polyconvert_main.cpp`, `PCLoader*` |
| `src/traci-server` | TraCI command server/domains | `TraCIServer`, `TraCIServerAPI_*` |
| `src/libsumo` | direct domain API | `Simulation`, `Vehicle`, `Lane`, etc. |
| `src/libtraci` | C++ remote client and bindings | `Connection`, domain mirrors |
| `src/fmi` | FMI co-simulation adapter | `libsumofmi2` sources |
| `src/gui` | application/views/run-load threads | `GUIApplicationWindow`, `GUIRunThread`, `GUIViewTraffic` |
| `src/guisim` | GUI-aware runtime subclasses | `GUINet`, `GUIVehicle`, `GUILane`, `GUIEdge` |
| `src/guinetload` | GUI runtime builders | `GUIEdgeControlBuilder`, junction builder |
| `src/netedit` | network/demand/additional-data editor | `GNE*` model/view/undo elements |
| `src/osgview` | optional OpenSceneGraph 3D view | OSG scene/view helpers |
| `src/tools` | native emissions analysis executables | `emissionsDrivingCycle`, `emissionsMap` |
| `src/traci_testclient` | protocol/direct-API verification executables | `TraCITestClient`, `testlibsumo`, `testlibtraci` |
| `src/utils` | shared options/XML/time/geometry/router/vehicle/output/GUI infrastructure | module-specific classes |
| `src/foreign` | bundled third-party/adapted sources | tcpip, rtree, PHEMlight, etc. |

## Python modules

| Path | Role |
|---|---|
| `tools/sumolib` | parse/manipulate networks, XML, routes, shapes and options |
| `tools/traci` | pure-Python TraCI client/domain API |
| `tools/libsumo`, `tools/libtraci` | generated/native Python bindings/packaging locations |
| `tools/assign` | iterative assignment workflows |
| `tools/import`, `tools/net`, `tools/route` | data conversion and network/route manipulation |
| `tools/randomTrips.py` | sampled trip/flow generation |
| `tools/tls`, `tools/detector`, `tools/output` | domain-specific analysis/generation |
| `tools/visualization` | plotting and rendering helpers |
| `tools/webWizard` | browser-guided scenario creation |
| `tools/contributed` | externally contributed tools; inspect license/maintenance individually |

## Detailed coverage additions

- `gui/netedit_architecture.md` covers the editor's model/change/recompute
  contracts.
- `simulation/devices_detectors_outputs.md` covers the observation and attached
  behavior pipeline.
- `simulation/mesoscopic_simulation.md` covers the alternate event-driven
  segment/queue execution engine.
- `simulation/rail_and_electric_systems.md` covers rail driveways/constraints
  and coupled battery, charging-station, overhead-wire, and traction state.
- `network/import_export_formats.md` covers format translation boundaries.
- `tools/secondary_executables.md` covers the remaining native command-line
  targets.
- `16_SOURCE_COVERAGE_AUDIT.md` reconciles every production source directory.

## Executable composition

`src/CMakeLists.txt` defines common library groups and executable links. GUI
targets are conditional on FOX; optional modules/bindings follow top-level CMake
feature flags. Consult CMake rather than assuming a source directory is linked
into every executable.

## Confidence

High at directory/target level; this index intentionally omits trivial files.
