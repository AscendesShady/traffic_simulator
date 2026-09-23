# Source coverage audit

## Objective and method

This is the final completeness pass over production source. It answers a
different question from source-path QA: not merely whether cited paths exist,
but whether every source cluster has been classified and whether an important
subsystem was omitted.

The audit enumerated and read all files matching C/C++ source/header extensions
under `src/` and all Python files under `tools/`, grouped them by directory,
inspected entry points and CMake targets, extracted file/class/function families,
and compared those families with the knowledge base. It then performed focused
source reads for uncovered high-impact clusters. This is a systematic skim and
coverage classification, not a claim that every function received a semantic
line-by-line proof.

Baseline at revision `d8461b9d3306a4b26cf25b3dd49da551e459bef1`:

| Corpus | Files scanned | Lines scanned |
|---|---:|---:|
| native `*.cpp`, `*.h`, `*.c`, `*.cc` under `src/` | 2,112 | 627,037 |
| Python `*.py` under `tools/` | 619 | 231,573 |
| contributed Java under `tools/contributed/` | 207 | 26,801 |
| bundled PHEMlight C# reference code | 13 | 5,444 |
| webWizard JavaScript | 4 | 699 |
| SWIG interface definitions | 3 | 849 |
| Windows resource definitions | 2 | 2 |
| shell and batch scripts | 12 | 413 |
| contributed MATLAB | 1 | 95 |
| bundled C++ `*.hpp` headers | 3 | 19,970 |

The `*.hpp` group is bundled `nlohmann`/`zstr` code, the C# group is bundled
PHEMlight reference code, and all Java in these roots belongs to contributed
TraaS/LiSuM tools. They are dependency/contributed boundaries rather than core
SUMO architecture. The two `*.rc` files bind the SUMO and Netedit application
icons and contain no runtime logic. Generated, schema, image, test-input,
expected-output, and prose files were inventoried elsewhere but are not counted
as executable source.

## Native directory census

`Code files` includes recursive C/C++ sources and headers. Coverage levels mean:

- **deep**: dedicated subsystem plus algorithm/state/source-map treatment;
- **focused**: a dedicated document covering architecture and modification
  boundaries, without specifying every concrete model;
- **boundary**: entry points, data contracts, dependencies, and tests are mapped;
- **infrastructure**: shared or optional internals are classified where consumed;
- **external**: bundled/adapted code is treated as a dependency boundary.

| Directory | Code files | Coverage | Primary knowledge-base location |
|---|---:|---|---|
| root `src/*_main.cpp` | 4 | boundary | project overview, execution flow, tool documents |
| `src/activitygen/` | 47 | boundary | demand generation and secondary executables |
| `src/dfrouter/` | 20 | boundary | routing, tool ecosystem, secondary executables |
| `src/duarouter/` | 5 | deep | `tools/duarouter.md`, routing algorithm |
| `src/fmi/` | 5 | boundary | external interfaces and interface index |
| `src/foreign/` | 33 | external | dependency and license maps |
| `src/gui/` | 32 | deep | GUI architecture, rendering, interaction |
| `src/guinetload/` | 6 | focused | GUI architecture and runtime loading |
| `src/guisim/` | 52 | focused | GUI architecture/rendering and class map |
| `src/jtrrouter/` | 11 | boundary | routing and secondary executables |
| `src/libsumo/` | 56 | deep | `interfaces/libsumo.md` |
| `src/libtraci/` | 27 | deep | TraCI/libsumo interface documents |
| `src/marouter/` | 11 | boundary | routing and secondary executables |
| `src/mesogui/` | 6 | focused | `simulation/mesoscopic_simulation.md` and GUI integration |
| `src/mesosim/` | 16 | deep | `simulation/mesoscopic_simulation.md` and source maps |
| `src/microsim/` | 378 | deep | simulation and algorithm document sets |
| `src/netbuild/` | 67 | deep | network set and network-generation algorithm |
| `src/netedit/` | 489 | focused | `gui/netedit_architecture.md` |
| `src/netgen/` | 11 | deep | netgenerate/network-generation documents |
| `src/netimport/` | 203 | focused | netconvert and `network/import_export_formats.md` |
| `src/netload/` | 16 | deep | the data-flow, execution-flow, and runtime-network documents |
| `src/netwrite/` | 14 | focused | netconvert and import/export boundaries |
| `src/od/` | 11 | focused | demand-generation algorithm and secondary tools |
| `src/osgview/` | 9 | boundary | rendering and optional dependency maps |
| `src/polyconvert/` | 19 | focused | tool ecosystem and secondary executables |
| `src/router/` | 27 | deep | simulation/algorithm routing and duarouter |
| `src/tools/` | 6 | focused | emissions tools in secondary executables |
| `src/traci_testclient/` | 5 | boundary | interface testing and secondary executables |
| `src/traci-server/` | 48 | deep | `interfaces/traci.md` |
| `src/utils/` | 478 | infrastructure | dependency/module/interface/algorithm documents |

All native source directories now have an explicit destination in the corpus.
File-stem mention counts were used only to find gaps: requiring every concrete
class name in Markdown would violate the task's instruction not to reproduce
the codebase symbol by symbol.

## Microscopic subdirectory coverage

| Subdirectory | Role found in source | Coverage decision |
|---|---|---|
| `src/microsim/actions/` | scheduled save/TLS/control command objects | event/output boundaries; no separate subsystem needed |
| `src/microsim/cfmodels/` | concrete longitudinal models | deep algorithm/subsystem coverage |
| `src/microsim/devices/` | attached behavior and output devices | dedicated devices/detectors/outputs document |
| `src/microsim/engine/` | engine/energy helper models | optional vehicle-device boundary |
| `src/microsim/lcmodels/` | discrete and sublane lane-change models | deep algorithm/subsystem coverage |
| `src/microsim/logging/` | optional runtime event logging | output/infrastructure boundary |
| `src/microsim/output/` | detectors, aggregation, exporters | dedicated devices/detectors/outputs document |
| `src/microsim/traffic_lights/` | road TLS plus rail driveways, constraints, crossings and deadlock control | deep TLS and focused rail-system coverage |
| `src/microsim/transportables/` | persons, containers, stages, pedestrian models | deep pedestrian/PT coverage |
| `src/microsim/trigger/` | rerouters, calibrators, charging/wire/speed triggers | focused in routing, devices/output, other behavior, and rail/electric systems |

The skim identified concrete device families not previously named—battery,
electric hybrid, emissions, SSM, ToC, driver state, blue-light, Bluetooth,
GLOSA, friction, FCD replay, taxi dispatch, and station finding. Their common
lifecycle is now specified; their domain equations remain optional focused
work rather than being guessed.

## Utility coverage

`src/utils/` is not one subsystem. Its directories were classified as follows:

| Utility family | Architectural consumers / documentation |
|---|---|
| common time, IDs, RNG, messages | state, execution, invariants, testing |
| distribution | demand, vehicle/type parameter sampling |
| emissions and vehicle parameters | devices/output and secondary emissions tools |
| FOX/OpenGL GUI utilities | GUI architecture/rendering/interaction |
| geometry and shapes | network generation/import, GUI, pedestrian behavior |
| handlers/import I/O/XML | CLI, data flow, loaders and format boundaries |
| output devices | interfaces and devices/output |
| options | command-line/configuration interface |
| generic routers/thread pool | routing and dependency maps |
| TraCI constants/storage | TraCI/libsumo interfaces |
| traction wire | charging/electric optional device boundary |
| utility self-tests | testing architecture |

These are implementation services. A reimplementation should reproduce a
service's observable contract where required, not its directory structure.

## Python tool coverage

All 619 Python files and the smaller non-Python script groups were grouped into
the immediate tool families:
`assign`, `build_config`, `contributed`, `detector`, `devel`, `district`, `drt`,
`emissions`, `game`, `import`, `lib`, `libsumo`, `libtraci`, `net`,
`neteditTestFunctions`, `output`, `purgatory`, `route`, `shapes`, `simpla`,
`sumolib`, `tls`, `traci`, `trigger`, `turn-defs`, `visualization`, `webWizard`,
and `xml`, plus top-level scripts such as `randomTrips.py` and
`duaIterate.py`.

The contributed Java, MATLAB, and bundled PHEMlight C# sources were checked as
separate license/maintenance boundaries. SWIG interface files under
`src/libsumo/` and `src/libtraci/` feed generated language bindings. JavaScript,
HTML/CSS, and related assets under `tools/webWizard/` implement its browser
front end; they do not participate in the native simulation loop.

Detailed reusable-library coverage remains concentrated on `sumolib` and
TraCI. The tool ecosystem document maps the workflow families. Build/developer,
test-driving, purgatory, game/web UI, and contributed scripts are deliberately
not promoted to core simulator requirements. Contributed tools require
individual maintenance and license review.

## Executable target reconciliation

The CMake scan found the expected production targets: `sumo`, `sumo-gui`,
`netconvert`, `netgenerate`, `netedit`, `duarouter`, `jtrrouter`, `dfrouter`,
`marouter`, `od2trips`, `activitygen`, `polyconvert`, `emissionsDrivingCycle`,
and `emissionsMap`, plus TraCI/libsumo test clients. Every target now appears in
the project/module/tool/interface documents.

## Gaps found and resolved in this pass

1. **Netedit was only indexed.** Added `gui/netedit_architecture.md` to describe
   its editable models, load thread, view modes, change/undo model,
   recomputation, persistence, invariants, and test surface.
2. **Devices/detectors/output were compressed into one short section.** Added
   `simulation/devices_detectors_outputs.md` with construction order, callbacks,
   interval state, output ordering, lifecycle invariants, and modification
   points.
3. **Secondary native executables lacked implementation contracts.** Added
   `tools/secondary_executables.md` for JTR/DF/macro/OD/activity routing and
   demand, polyconvert, emissions utilities, and test clients.
4. **Format-specific network code was represented only as netconvert detail.**
   Added `network/import_export_formats.md` to make the normalized `NB*`
   boundary, importer order, projection, loss, and writer contracts explicit.
5. **No durable all-source census existed.** Added this document and linked it
   from the project overview/module index/QA.
6. **Mesoscopic execution was only summarized.** Added
   `simulation/mesoscopic_simulation.md` after tracing the event queue, segment
   admission and transitions, mesoscopic vehicle/control, detectors,
   calibrator, teleport, state, GUI, and outer-step integration.
7. **Rail and electric state was scattered across TLS/device mentions.** Added
   `simulation/rail_and_electric_systems.md` for rail driveways, constraints and
   deadlock relations, plus battery/station-finder/charging and coupled
   overhead-wire/traction-circuit behavior.

## Remaining intentional depth limits

No major production directory is now missing from the architecture map. The
following remain intentionally below algorithm-specification depth:

- individual Netedit element, frame, dialog, and change subclasses;
- every external-format field mapping in `netimport` and `netwrite`;
- each concrete emissions/energy model's internal equations and most optional
  vehicle devices beyond their shared lifecycle;
- numerical equivalence for every mesoscopic parameter combination and detailed
  mesoscopic GUI presentation behavior;
- every rail constraint subtype and electric circuit equation;
- OpenSceneGraph 3D visualization;
- activity generation, detector-flow reconstruction, and macroscopic
  assignment mathematics;
- build/developer/contributed/purgatory/game/webWizard scripts;
- bundled third-party implementation internals.

These are recorded gaps, not invisible omissions. Promote one to a dedicated
specification only if the new simulator's requested compatibility profile
includes it.

## Confidence

High for source-directory, file, line, target, entry-point, and ownership
coverage. High that no production source cluster is absent from the component
map after the additions above. Medium for semantic completeness within optional
model families, because a repository-wide skim cannot replace focused
behavioral tracing and execution of every associated test.
