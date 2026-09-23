# Documentation QA

## QA baseline

- Repository revision inspected: `d8461b9d3306a4b26cf25b3dd49da551e459bef1`.
- Validation date: 2026-09-13.
- Scope: the local Eclipse SUMO checkout and the knowledge base under
  `docs/reverse_engineering/`.
- Constraint observed: no SUMO implementation, build, test, configuration, or
  license file was changed. All deliverables are in this documentation tree.

## Coverage

The requested document topology is complete. It contains:

| Area | Documents | Coverage judgment |
|---|---:|---|
| system-level architecture and contracts | 15 numbered documents (`00`-`14`) | complete for the requested outline |
| license and attribution | 1 additional document (`15`) | repository notices and major dependency boundaries recorded |
| source coverage audit | 1 additional document (`16`) | all production source directories and executable targets reconciled |
| practical tutorial | 1 additional document (`17`) | concise first-run workflow and capability map |
| simulation | 14 | engine, vehicles, CF, LC, routing, TLS, insertion/removal, PT, pedestrians, devices/outputs, mesoscopic execution, rail/electric, and other behavior |
| network | 8 | build-time/runtime models, nodes, edges, lanes, junctions, connections, generation, format boundaries |
| interfaces | 4 | TraCI, libsumo, CLI/configuration, and external interfaces |
| executable/tool ecosystem | 6 | netconvert, netgenerate, duarouter, secondary executables, sumolib, and wider tools |
| GUI | 4 | runtime architecture, rendering, interaction/thread model, and Netedit internals |
| algorithms | 13 | all algorithm families explicitly requested by the task |
| source navigation | 3 | class, function, and concept-to-source/test maps |
| QA | 1 | this report |

Including this report, the knowledge base contains 71 Markdown documents. The
algorithm documents all include the requested purpose, inputs, outputs, state,
preconditions, decision process, mathematical model, constraints, transitions,
edge cases, implementation, classes, tests, invariants, and reimplementation
notes. The reimplementation specification contains all 20 requested sections
and explicitly separates MUST PRESERVE, MAY CHANGE, OPTIONAL, and OUT OF SCOPE
requirements.

The documentation answers the target navigation questions through
`source_maps/source_reference_map.md`: each question leads to an orchestration
method, state owner, supporting implementation, and behavioral test family.

## Known Gaps

1. This is a source-level reverse-engineering pass, not a runtime conformance
   study. SUMO was not rebuilt and the TextTest/gtests were not executed merely
   to validate Markdown. Test mappings identify existing evidence; they do not
   claim every mapped test currently passes on this machine.
2. The TextTest tree is too large to inspect every case semantically. Counts in
   `13_TESTING_ARCHITECTURE.md` include organizational directories and are
   scale indicators, not exact executable scenario totals.
3. Netedit now has a subsystem-level architecture document, but its hundreds
   of individual element/frame/dialog/change subclasses are not specified one
   by one.
4. Individual emissions equations, calibrators, parking, taxi dispatch,
   intermodal routing, every rail-constraint subtype, coupled electric-circuit
   equations, and state serialization remain below the depth given to
   microscopic vehicle movement. Mesoscopic and rail/electric architecture now
   have focused subsystem documents, but their full numerical matrices were not
   executed.
5. Generated language bindings, FMI surfaces, platform-specific GUI
   behavior, and optional dependency internals were mapped but not exhaustively
   reverse-engineered.
6. Performance properties are qualitative. No benchmark was run, so complexity
   and hot-path notes should guide profiling rather than substitute for it.

These gaps do not prevent use as a reimplementation guide for the documented
core. They do constrain claims of complete SUMO feature compatibility.

## Uncertain Areas

- JuPedSim behavior beyond SUMO's adapter is external to this repository. The
  documentation has high confidence in the adapter boundary and lower
  confidence in the external engine's internal behavior.
- A few lifecycle/concurrency conclusions in the libsumo and TraCI documents
  are explicitly labeled as inferences from static ownership and early-return
  paths. They should be confirmed with focused multi-client and repeated-load
  tests before becoming compatibility requirements.
- Some build-to-runtime semantic correspondences, especially which build-time
  junction attributes survive as compact runtime fields, are inferred from the
  writer/loader pair rather than proven with an instrumented round trip.
- Test-strength judgments are based on discovered suites and representative
  implementation/test inspection. They are not mutation-testing or coverage
  measurements.
- The proposed architecture near the end of `14_REIMPLEMENTATION_SPEC.md` is
  deliberately marked as design inference. It is guidance, not a description
  of mandatory SUMO internals.

## Source Verification

The validation passes used the repository itself rather than filenames alone:

- Startup and load control were rechecked through `src/sumo_main.cpp`,
  `src/netload/NLBuilder.cpp`, option registration/parsing, and builder calls.
- Microscopic step ordering was rechecked against
  `MSNet::simulationStep()`/`postMoveStep()`, then followed into edge, lane,
  vehicle, insertion, removal, transfer, signal, and event controllers.
- Network generation was followed from import/generator entry points through
  `NBNetBuilder::compute()` and `NWWriter_SUMO::writeNetwork()`, then back into
  runtime SAX loading.
- TraCI, libsumo, GUI, router, and tool claims were checked against their entry
  points, build files, dispatch methods, and representative tests.
- License claims were checked against the root `LICENSE`, `NOTICE.md`,
  `.gitmodules`, and optional dependency declarations in CMake files. The
  license document records facts and review points, not legal advice.
- A second completeness pass enumerated and read 2,112 native source/header
  files (627,037 lines) and 619 Python tool files (231,573 lines), grouped every
  production directory, and also classified the smaller Java, C#, JavaScript,
  SWIG, Windows-resource, shell/batch, MATLAB, and bundled `*.hpp` corpora. It
  reconciled every CMake executable target and then performed focused semantic
  reads for uncovered high-impact clusters. See
  `16_SOURCE_COVERAGE_AUDIT.md`.

Automated documentation checks were rerun after the source-coverage additions:

| Check | Result |
|---|---|
| Markdown documents checked | 71 |
| files with anything other than exactly one H1 | 0 |
| files with unbalanced fenced-code blocks | 0 |
| broken relative Markdown links | 0 |
| Mermaid blocks inspected | 11 |
| unique extracted repository/doc-path candidates | 585 |
| exact non-wildcard path failures after correction | 0 |
| paired `.h/.cpp` shorthand failures | 0 |
| wildcard/family patterns not treated as literal files | 104 |
| architectural class-map rows checked in cited header | 78; 0 failures |
| qualified source-map symbols checked in `src/` | 86 concrete checks; 0 failures |
| qualified names swept across all documents | 368 non-library/source names; 0 missing method/member names |
| simple backticked type/constant-like identifiers swept | 536; 535 found in source/build/test text, 1 verified as a repository label |
| immediate production source directories absent from docs | 0 |
| CMake executable targets absent from docs | 0 |
| duplicated long prose paragraphs | 0 exact duplicate groups among 755 checked |

Wildcard/family references include model families, recursive test trees, and
compact `{h,cpp}` notation. They describe multiple files and are not presented
as literal paths; concrete `.h/.cpp` pairs were expanded and checked separately.
Source line numbers are used sparingly because this is a living repository;
file plus class/function is the stable locator.

The one simple backticked identifier not present in source/build/test file text
is an intentional repository label: `ContextSubscriptionFilters` is a TraCI
test-suite directory. `SUMO_BIN_DIR`, previously classified alongside it, is
present in test/CI script text once the validation corpus includes those file
types. During the global sweep, two stale pseudo-class references and one
incorrectly owned event-queue method were corrected to their actual
implementations.

The Mermaid graphs were compared with the same call/build relationships used
by the prose. In particular, they preserve the direction from import/generation
to `NBNetBuilder`, writer, runtime loader, and simulation; the simulation-step
diagram follows the actual micro/meso branch and movement/insertion/removal
ordering. No graph is intended as a complete include graph.

## Documentation/Implementation Conflicts

One material source-level conflict was retained rather than normalized away:

- `MSVehicleControl::isPendingRemoval()` returns container membership in the
  FOX build, but the non-FOX branch compares `std::find(...)` to `end()` with
  equality. That yields true when the vehicle is *absent*, contrary to the
  method name, the FOX branch, and the duplicate-prevention caller in
  `scheduleVehicleRemoval()`. See `src/microsim/MSVehicleControl.cpp:145`.
  The knowledge base treats this as an implementation anomaly requiring a
  focused regression test, not behavior that a reimplementation must preserve.

During QA, several draft references were also found to describe plausible but
nonexistent directories (for example, old pedestrian, netconvert, randomTrips,
and testlibtraci paths). They were replaced with paths that exist in this
checkout. No remaining known prose/source conflict was silently converted into
a requirement.

## Internal consistency

- `03_EXECUTION_FLOW.md`, the simulation subsystem documents, the algorithm
  documents, and `14_REIMPLEMENTATION_SPEC.md` agree on the step boundary:
  decision/planning precedes movement commit; buffered lane integration,
  deferred removal, insertion, transfers, output, and clock advancement retain
  explicit ordering.
- `05_STATE_MODEL.md` and `09_INVARIANTS.md` use the same ownership model as the
  class/function maps: `MSNet` owns global orchestration, control objects own
  lifecycle registries, lanes own ordered occupancy, and vehicles own route and
  motion state.
- The reimplementation requirements preserve observable semantics and data and API
  contracts without requiring C++, FOX, singleton registries, current class
  boundaries, or identical containers.
- Testing requirements correspond to the behavior-to-implementation mappings
  in `13_TESTING_ARCHITECTURE.md`; structural checks are not presented as proof
  of traffic-model equivalence.

## Recommended Next Investigation

1. Add a focused cross-build regression for the non-FOX pending-removal branch
   before using removal behavior as an executable oracle.
2. Build a small black-box conformance corpus covering one representative case
   per MUST PRESERVE requirement: departure retries, CF safety bounds, lane
   changes, junction conflicts, signal switching, arrival/teleport removal,
   state save/load, and TraCI subscription timing.
3. Instrument one network round trip from plain XML through netconvert to
   runtime loading, recording how connections, request indices, internal lanes,
   permissions, geometry, and TLS link indices transform.
4. Perform field-level Netedit element/change compatibility work if editor
   file and workflow parity is part of the new simulator's product scope.
5. Measure deterministic replay across timestep sizes, thread settings,
   routing workers, save/load, and remote-control intervention rather than
   assuming determinism from static code.
6. Obtain legal review before copying implementation material or redistributing
   a derivative. Use `15_LICENSE_AND_ATTRIBUTION.md` as an inventory, not a
   legal conclusion.

## Final assessment

The knowledge base is structurally complete for the requested scope,
source-navigable, explicit about uncertainty, and suitable as the starting
specification for a SUMO-inspired simulator. Its strongest evidence concerns
core microscopic execution, vehicle/network state, routing, signals,
interfaces, and build/runtime network transformation. The gaps above should be
treated as a prioritized backlog for broader compatibility, not filled by
guesswork.
