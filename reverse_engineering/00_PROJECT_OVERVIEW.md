# 00. Project overview

## Objective of this knowledge base

This directory describes the architecture, observable behavior, algorithms,
state contracts, interfaces, tests, and safe change boundaries of Eclipse SUMO.
It is a guide to reimplementation and investigation, not a line-by-line source
translation. Statements apply to repository revision
`d8461b9d3306a4b26cf25b3dd49da551e459bef1` unless noted.

## What SUMO is in this repository

SUMO is an ecosystem of executables and libraries around a shared set of
network, demand, routing, simulation, XML, and utility models:

- `sumo` — headless microscopic/multimodal or mesoscopic simulator.
- `sumo-gui` — GUI shell over the same core engine.
- `netconvert`, `netgenerate`, `netedit` — network import/generation/editing.
- `duarouter`, `jtrrouter`, `dfrouter`, `marouter`, `od2trips`, `activitygen` —
  demand/routing/assignment tools.
- TraCI server and clients — process-separated runtime control.
- libsumo — in-process runtime control; libtraci — C++ socket client.
- `tools/` — Python libraries and workflow programs including `sumolib`,
  `traci`, assignment, import/export, TLS, detector, visualization, and demand
  tooling.

Build evidence: targets and link groups in `src/CMakeLists.txt` and subsystem
`CMakeLists.txt` files.

## Repository inventory

| Path | Role |
|---|---|
| `src/` | C/C++ executables, engine, libraries, GUI, import/build/write stacks |
| `tools/` | Python clients, libraries, converters, scenario/workflow tools |
| `tests/` | TextTest-driven functional/regression scenarios and expected outputs |
| `unittest/` | Google Test unit targets |
| `data/` | XML schemas, fonts/images, object types, localization and shared assets |
| `docs/` | existing SUMO documentation, Doxygen material, this knowledge base |
| `build_config/` | CMake find modules, packaging, CI/platform build support |
| `bin/` | executable launchers/artifacts supplied by the checkout |
| `.github/`, `.jenkins/` | automation and CI configuration |
| `LICENSE`, `NOTICE.md` | project and third-party notices |

At reconnaissance time `rg --files` reported 226,264 paths excluding this
knowledge-base subtree. That number includes the very large scenario corpus and
is an inventory indicator, not a source-line metric.

## Architectural split that must remain clear

### Build-time network versus runtime network

`netbuild` uses mutable `NBNode`, `NBEdge`, lane/connection records and control
containers. It computes a `.net.xml`. `sumo` later parses that file into
`MSNet`, `MSEdge`, `MSLane`, `MSLink`, and junction/traffic-light objects.
They are not the same live object graph.

### Model versus interfaces

`MS*` owns simulation semantics. TraCI/libsumo expose those semantics; GUI
subclasses render them. A reimplementation may replace wire/UI internals but
must not allow those adapters to become competing traffic models.

### Preventive behavior versus recovery

Car following, lane changing, link right-of-way and insertion attempt to avoid
invalid states. Collision detection and teleport/removal recover from states
that still occur. Recovery settings are not the nominal motion model.

## Build and external dependencies

CMake is the principal native build. Xerces-C++ is required. Configuration can
enable PROJ, FOX/OpenGL, FreeType, zlib, gettext, SWIG bindings, Arrow/Parquet,
Eigen, GDAL, FFMPEG, OpenSceneGraph, GL2PS, JuPedSim/GEOS, Boost, fmt, gtest,
and tcmalloc. Exact availability is platform/configuration dependent.

Source: top-level `CMakeLists.txt`, `src/CMakeLists.txt`.

## How to use this corpus

1. For a practical first run, follow `17_SUMO_QUICK_TUTORIAL.md`.
2. Start the architectural material with `01_SYSTEM_ARCHITECTURE.md` and
   `02_COMPONENT_MAP.md`.
3. For a behavioral change, consult `07_ALGORITHM_INDEX.md`, the detailed
   algorithm/subsystem page, `09_INVARIANTS.md`, and `12_CHANGE_IMPACT.md`.
4. Follow concept-to-source/test links in `source_maps/`.
5. For a new implementation, treat `14_REIMPLEMENTATION_SPEC.md` as the
   requirement boundary and detailed pages as evidence/explanation.
6. Review `DOCUMENTATION_QA.md` before relying on low-confidence or incomplete
   areas.
7. Use `16_SOURCE_COVERAGE_AUDIT.md` to see how every production source
   directory was classified and which optional families remain below deep
   specification level.

## Documentation versus implementation discrepancies

This corpus records observed conflicts instead of resolving them silently.
Known source anomaly: the non-FOX implementation of
`MSVehicleControl::isPendingRemoval()` appears inverted relative to the FOX
branch and caller; see `simulation/vehicle_removal.md`. It is not promoted to a
required behavior.

## Confidence

High for the major ecosystem/component boundaries; depth varies by subsystem
and is stated in each document.
