# 08. Interface index

| Interface | Inputs / outputs | Implementation | Contract tests |
|---|---|---|---|
| command line | argv, help/version, exit status | `utils/options`, `SystemFrame`, `*Frame` | per-tool meta/errors |
| configuration XML | typed options and relative file references | `OptionsIO`, `OptionsLoader`, `OptionsCont` | write_config/template and runs |
| network XML | topology/geometry/connections/TLS | `NWWriter_SUMO` -> `NLHandler` | netconvert + all simulation tests |
| routes/demand XML | types, routes, trips, flows, persons | route handlers/loaders | router/SUMO demand suites |
| additional XML | stops, detectors, rerouters, shapes/controllers | `NLHandler` + specialized handlers | subsystem suites |
| state XML | checkpoint/restore | `MSStateHandler` + owner state methods | `tests/complex/state/` |
| simulation outputs | XML/text/CSV/Parquet observations | `microsim/output`, devices, `OutputDevice` | output expected files |
| TraCI | typed binary request/reply/subscription over TCP | `traci-server`, constants, APIs | `tests/traci`, complex TraCI |
| libsumo | in-process static domain API | `src/libsumo/` | testlibsumo/API parity |
| libtraci | C++ TraCI client with libsumo-shaped domains | `src/libtraci/` | testlibtraci, language tests |
| Python TraCI | process launch/connections/domain calls | `tools/traci/` | TraCI Python tests |
| SWIG bindings | Python/Java/C#/optional C | `.i` files and CMake | language integration suites |
| GUI interaction | FOX events/views/picking/snapshots | `src/gui`, `guisim` | GUI/pyautogui/TraCI GUI |
| FMI 2 | co-simulation lifecycle/variables | `src/fmi` | `tests/complex/fmi/` |

## Compatibility layers

File compatibility includes schema plus defaults, units, ordering, ID
references and failure behavior. API compatibility includes types, exception
semantics, step timing and subscriptions. Visual similarity is not simulation
compatibility; GUI commands that mutate state must map to the same core
operations.

## Source-of-truth hierarchy

- Wire command IDs/types: `src/libsumo/TraCIConstants.h` and server/client code.
- Direct domain semantics: libsumo domain implementations backed by `MS*`.
- XML vocabulary: `SUMOXMLDefinitions`, parsers/writers and `data/xsd/`.
- CLI option semantics: registry plus owning `checkOptions()` and consumer.
- Observed regression behavior: expected files/scripts in `tests/`.

See `interfaces/traci.md`, `interfaces/libsumo.md`,
`interfaces/command_line.md`, `interfaces/external_interfaces.md`,
`network/import_export_formats.md`, and
`simulation/devices_detectors_outputs.md`. Specialized runtime surfaces are
mapped in `simulation/mesoscopic_simulation.md` and
`simulation/rail_and_electric_systems.md`.

## Confidence

High at interface-family level.
