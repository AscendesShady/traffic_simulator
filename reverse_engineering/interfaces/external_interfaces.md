# External interfaces beyond TraCI and libsumo

## Scope

SUMO exposes several integration surfaces. TraCI and libsumo are documented
separately; this page records adjacent interfaces so a reimplementation does not
mistake one compatibility target for all of them.

## Interface matrix

| Interface | Boundary | Primary implementation | Compatibility concern |
|---|---|---|---|
| libtraci | C++ client over TraCI socket | `src/libtraci/Connection.*`, domain classes | wire protocol, command IDs, response types, subscriptions |
| Python TraCI | Python client over socket, optionally redirected to libtraci | `tools/traci/` | public module/domain API and protocol semantics |
| SWIG libsumo/libtraci | Python/Java/C# bindings | `src/libsumo/*.i`, `src/libtraci/libtraci.i`, CMake files | generated-language types and exceptions |
| FMI 2 | co-simulation shared library | `src/fmi/` | FMI lifecycle and packaged model description |
| XML files | network, routes, configuration, additional data, outputs | `data/xsd/`, parsers/writers | schema, IDs, units, ordering and defaults |
| Parquet output | optional columnar output backend | `src/utils/iodevices/`, build flags | schema and optional Arrow/Parquet dependency |
| GUI automation/remote control | GUI events plus TraCI GUI domain | `src/gui/`, `src/libsumo/GUI.*`, TraCI GUI handlers | view IDs, screenshots, selection and timing |

## libtraci

`libtraci::Connection` owns a `tcpip::Socket`, labels active connections,
serializes commands, receives exact replies, and stores subscription results.
Domain classes reuse libsumo's public headers/types but invoke the remote server
through `Domain.h` helpers. Thus libtraci resembles libsumo at the API surface
while retaining process/socket failure modes and step synchronization.

Source: `src/libtraci/Connection.cpp`, `Domain.h`, `Simulation.cpp`.

## Python client selection

`tools/traci/__init__.py` can import the compiled `libtraci` module when
requested by environment; otherwise it exposes the pure-Python client.
`tools/traci/main.py` manages connection labels, process startup, and the public
`simulationStep()` convenience function. Application code should not rely on
the two backends having identical performance or process isolation even where
their domain calls match.

## File interface

XML is a first-class API. `data/xsd/` contains schemas, while SAX handlers in
`src/netload`, `src/router`, `src/utils/xml`, and other modules construct runtime
objects. Writers under `src/microsim/output` and `src/netwrite` define observable
serialization. Compatibility includes defaults, units, ID references, and error
handling—not just element names.

## FMI

`src/fmi/CMakeLists.txt` builds `libsumofmi2`; the adapter embeds/control steps
through libsumo. FMI is optional (`ENABLE_FMI`) and should be treated as an
adapter over simulation lifecycle rather than a separate engine.

## Tests

- libsumo and language bindings: `tests/complex/unit_tests/testlibsumo/`,
  `tests/complex/traci_java/`, and `tests/complex/traas/`; native TraCI
  scenarios are exercised throughout `tests/complex/traci/`.
- Python/wire protocol: `tests/traci/`, `tests/complex/traci/`.
- FMI: `tests/complex/fmi/`.
- XML and outputs: tool-specific functional suites plus `data/xsd/` validation
  uses in tooling.

## Change rule

Keep transport-independent domain semantics in the libsumo API classes where
possible; adapt transport in TraCI/libtraci. A change to public constants or
compound value layouts must be synchronized across server handlers, C++ and
Python clients, SWIG bindings, and tests.

## Confidence

High for interface boundaries; medium for optional build/package details that
depend on the configured platform.
