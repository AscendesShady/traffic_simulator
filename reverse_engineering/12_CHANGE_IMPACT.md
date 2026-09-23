# 12. Change-impact guide

| Proposed change | Components/source to inspect | Tests/gates | Risk |
|---|---|---|---|
| car-follow formula/parameter | concrete `MSCFModel_*`, base bounds, vehicle type parsing, vehicle plan/finalize | CF model, action-step, collision, TraCI vtype | High |
| integration/step length | `SUMOTime`, `DELTA_T`, `MSNet`, `MSVehicle`, all model conversions, flows/events/output | broad SUMO variants, ballistic/action-step/state | Critical |
| lane-change policy | LC model, lane changer, vehicle best lanes, lane containers/partial occupancy | LC, sublane, opposite, collision | High |
| route representation/reroute | `MSRoute`, `MSBaseVehicle`, `MSRoutingEngine`, router graph/API | all router suites, rerouting, stops, state | High |
| junction admission | `MSLink`, junction classes, vehicle drive items, netbuild foe logic | junction model, TLS, pedestrians, collisions | Critical |
| traffic-light timing | TL logic subclass/control, phase/link mapping, detectors, TraCI | TLS, TraCI trafficlight, state | High |
| lane/connection topology | `NBEdge/NBNode`, compute order, writers/loaders, `MSLink` | netconvert + junction/TLS/routing | Critical |
| network importer | relevant `NIImporter_*`, containers/defaults/projection | importer and round-trip cases | Medium–High |
| network generator | `NG*`, conversion to NB, RNG | netgen seeded grid/spider/random | Medium |
| insertion procedure | insertion control, edge lane selection, lane safety, counters | flows/depart modes/congestion/TraCI add | High |
| arrival/removal | vehicle/lane cleanup, vehicle control, devices/output/passengers | tripinfo, PT, state, API remove | High |
| teleport/gridlock | vehicle waiting logic, transfer, routing/permissions | congestion, teleport, parking, taxi | High |
| collision policy | lane detection geometry/actions, transfer/removal, options/output | CF/LC/junction/pedestrian collision | High |
| pedestrian model | `MSPModel*`, walking stages, link/lane conflict | pedestrian, person API, crossings | High |
| public transport dwell/boarding | stops, vehicle stop logic, transportable devices/stages | PT/person/busstop/state | High |
| new vehicle device | device factory/options, lifecycle/reminders, state/output/API | device + arrival/state | Medium–High |
| detector/output format | detector/reminders, writer, schemas/options | output exact files, intervals, empty cases | Medium |
| TraCI command/type | constants, server handler, libsumo domain, clients/bindings | protocol + complex/language tests | Critical |
| libsumo implementation | domain/helper and underlying `MS*` behavior | testlibsumo plus TraCI parity | High |
| GUI replacement | GUI load/run/control boundary, snapshots, core command APIs | headless parity + GUI lifecycle/automation | Medium if isolated; High if commands change |
| options/default | owning `*Frame`, consumers, config writer/docs | meta/config/error plus behavioral suite | Medium–High |
| state schema | every owner save/load/clear and RNG/cross-reference restoration | complete state matrix | Critical |
| multithreading/parallel lanes | edge/lane scheduler, containers, RNG, GUI locks, deterministic output | parallel variants + serial equality/race checks | Critical |
| mesoscopic model | `src/mesosim`, shared MSNet/interface/output assumptions | meso and meso GUI variants | High |
| rail signal/constraint policy | `MSRailSignal*`, `MSDriveWay`, route replacement, links, insertion and teleport | rail, TLS, state and TraCI trafficlight | Critical |
| battery/station selection | battery/station-finder devices, charging stops, routing, energy output and state | battery, stationfinder, chargingstation TraCI | High |
| overhead-wire/circuit model | electric-hybrid device, wires, substations, traction circuit, output/API | elechybrid and overheadwire TraCI | High |

## Required impact procedure

1. Locate the behavioral owner in `source_maps/`.
2. Identify state read/written in `05_STATE_MODEL.md`.
3. Check phase/order dependencies in `03_EXECUTION_FLOW.md`.
4. Revalidate every relevant invariant in `09_INVARIANTS.md`.
5. Run focused functional fixtures before broad suites; compare exact outputs
   only after understanding intentional numerical changes.
6. For public/file/protocol changes, update every adapter and schema/client.
7. Record whether change is intended compatibility change or internal redesign.

## Changes usually safe behind a contract

- container/caching implementation with identical order and invalidation;
- GUI toolkit/rendering with synchronized snapshots and same commands;
- class/file organization with stable public/file behavior;
- routing priority-queue internals with identical admissibility/cost/tie rules;
- parser implementation with identical formats/defaults/errors.

## Changes that are not local despite appearing local

- lane index/order, TL link index, time conversion, vehicle deletion, RNG call
  order, route replacement, link approach timing, and output timestamps.

## Confidence

High for major coupling; specialized feature changes require their local source
and test inventories.
