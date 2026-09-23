# 13. Testing Architecture

This document describes how correctness of the SUMO codebase is verified: a small C++
unit-test layer plus a very large functional/regression ("texttest") suite that drives
the compiled binaries end-to-end and diffs their output against checked-in expected
results.

Sources consulted: `unittest/CMakeLists.txt`, `unittest/src/**`, `tests/README_Tests.md`,
`tests/config_all`, `tests/sumo/config.sumo`, `tests/runTests.sh`, `tests/runSumoTests.bat`,
`tests/texttestDiff.py`, directory listings under `tests/*`, and `git log --oneline`
(repository history dominated by commits such as `patching expected results refs #21, #18282`).

## 1. Overview of the two testing layers

| Layer | Framework | Location | What it exercises |
|---|---|---|---|
| C++ unit tests | **Google Test (gtest)** | `unittest/src/**` | Individual classes/functions in isolation (car-following math, geometry helpers, string utilities, event control, thread pool, height mapping, TL logic durations) |
| Functional/regression suite | **TextTest** (http://www.texttest.org) driving the compiled `sumo`, `netconvert`, `duarouter`, `netedit`, etc. binaries | `tests/**` (`tests/sumo`, `tests/netconvert`, `tests/duarouter`, `tests/netedit`, `tests/complex`, `tests/traci`, `tests/netgen`, `tests/jtrrouter`, `tests/dfrouter`, `tests/marouter`, `tests/od2trips`, `tests/polyconvert`, `tests/activitygen`, `tests/tools`) | Whole-program behaviour: simulation output, generated networks, routes, GUI/TraCI interaction, CLI tool output — compared byte-for-byte (after normalization) against recorded "expected" files |

Confirmed gtest usage: `unittest/src/unittest_main.cpp` does
```cpp
#include <gtest/gtest.h>
...
testing::InitGoogleTest(&argc, argv);
return RUN_ALL_TESTS();
```
and every test file under `unittest/src/**` uses `TEST(...)` / `TEST_F(...)` macros.

Confirmed TextTest usage: `tests/README_Tests.md` states "The files in this and all
subdirectories are test suites together with input and output files for use with
texttest (http://www.texttest.org)."

## 2. How each layer is run

### 2.1 Unit tests (gtest / CMake)
- `unittest/CMakeLists.txt` just does `include_directories(...)` and `add_subdirectory(src)`.
- `unittest/src/CMakeLists.txt` fans out into subdirectories: `microsim`, `netbuild`, `utils`
  (with `utils/common`, `utils/foxtools`, `utils/geom`, `utils/threadpool`), each with its own
  `CMakeLists.txt` building a test binary linked against gtest and the corresponding SUMO
  library subset.
- Built as normal CMake targets (the top-level SUMO CMake build enables `unittest` as a
  buildable target tree); run via the generated test executable(s) or `ctest`, same as any
  gtest/CMake project. No custom test runner script exists for this layer — it is plain
  CMake + gtest.

### 2.2 Functional/regression suite (TextTest)
Driven by shell/batch wrapper scripts at the root of `tests/`:
- `tests/runTests.sh` — the canonical POSIX entry point. Sets `LC_ALL=C`/`LANG=C` (for
  reproducible English messages), derives `SUMO_HOME`/`SUMO_BIN_DIR`, activates a Python
  venv if present, and ultimately invokes `texttest` with the discovered binaries
  (`sumo`, `sumo-gui`, etc., with optional `--gui` / `--debug` switches).
- `tests/runSumoTests.bat` — Windows entry point; launches `runExtraTests.py` with
  `-a sumo,sumo.gui,sumo.meso,sumo.meso.gui` (selects which named TextTest "app" configs
  to run — apps are the `testsuite.sumo`, `testsuite.sumo.gui`, `testsuite.sumo.meso`,
  etc. files scattered through `tests/sumo`).
- Other batch/shell scripts specialize by tool or mode: `runAllTests.bat`,
  `runCiTests.bat`, `runDebugTests.bat`/`.sh`, `runDemandRouterTests.bat`,
  `runNeteditInternalTests.bat`/`.sh`, `runNeteditExternalTests.bat`/`.sh`,
  `runNeteditOutputTest.bat`, `runToolTests.sh`, `runComplexToolTraciTests.bat`,
  `runTestsGui.bat`/`.sh`.
- `tests/toolrunner.py` / `tests/tracirunner.py` — Python helpers TextTest calls to
  execute Python-based tools/TraCI scripts as the "binary under test" for suites that
  aren't a compiled executable.
- `tests/texttestDiff.py` — registered as `diff_program` in a user's `~/.texttest/config`;
  on a failing comparison it opens the two files in `tkdiff` and, for `.net.xml` files,
  additionally launches `sumo-gui` on both networks side-by-side for visual comparison.
- `tests/updateTestsuiteFiles.py` — regenerates/updates the checked-in expected-result
  files (this is the tool behind the repository's frequent "patching expected results"
  commits).
- `tools/extractTest.py` (referenced from `tests/README_Tests.md`) extracts a
  single test case (with its inputs) for standalone
  manual reproduction outside TextTest.

### 2.3 Configuration and normalization
- `tests/config_all` is the shared TextTest config imported by every per-tool config
  (e.g. `tests/sumo/config.sumo` starts with `import_config_file:../config_all`).
- `[run_dependent_text]` in `tests/config_all` defines regex substitutions applied to
  output before comparison — e.g. `output:[0-9\.]+ms{REPLACE (TIME)}`,
  `output:spent [0-9\.]+s{REPLACE spent (TIME)}`, stripping of the SUMO version banner
  (`Eclipse SUMO .* v*`), and various XML-parser-message normalizations. This is how
  the suite avoids false failures from timing noise, version strings, or generated
  timestamps in config files (`cfg:<!-- generated on{[->]}-->`).
- Per-tool config files (`tests/sumo/config.sumo`, `.astar`, `.gui`, `.meso`, `.parallel`,
  `.perf`, `.idm`, `.ballistic`, `.sublanes`, `.CH`, `.CHWrapper`, `.nointernal`, etc.)
  select binary variants/option sets and list `copy_test_path` shared input fixtures
  (`net.net.xml`, `input_routes.rou.xml`, `input_additional.add.xml`, ...) reused across
  many scenario directories.

## 3. Functional-suite layout (per-scenario convention)

Each leaf test directory under `tests/<tool>/...` is one scenario: a small set of
input files plus checked-in expected-output files with matching names. Example,
`tests/sumo/cf_model/ACC/collisionAvoidanceOverride/`:
```
errors.sumo        errors.sumo.agg     fcd.sumo
input_routes.rou.xml   options.sumo    output.sumo
```
`options.sumo` (or `.sumo.<variant>`) holds the command-line/config options for that
run; `output.sumo`, `errors.sumo`, `fcd.sumo`, etc. are the **expected** captured
stdout/stderr/output-file contents for the option variant named `sumo`. A `testsuite.sumo`
(or `testsuite.sumo.<variant>`) file one level up lists the test-case subdirectory names
that belong to that TextTest "app" (seen in `tests/sumo/cf_model/testsuite.sumo`,
`tests/sumo/cf_model/ACC/testsuite.sumo`, etc.), so suites are organized hierarchically —
a directory of scenario folders plus a manifest file, arbitrarily nestable (e.g.
`cf_model/bugs/ticket3922`).

Top-level suite directories found in `tests/`:

| Directory | Tool under test | Approx. scenario count* |
|---|---|---|
| `tests/sumo` | `sumo` simulation engine | 5781 test dirs |
| `tests/netedit` | `netedit` GUI network editor | 11767 test dirs |
| `tests/netconvert` | `netconvert` | 1796 test dirs |
| `tests/complex` | Cross-tool / scenario / tutorial / TraCI / state / emissions tests | 1057 test dirs |
| `tests/duarouter` | `duarouter` | 767 test dirs |
| `tests/traci` | TraCI client protocol-level tests | 726 test dirs |
| `tests/od2trips` | `od2trips` | 198 test dirs |
| `tests/dfrouter` | `dfrouter` | 205 test dirs |
| `tests/netgen`/`netgenerate` | `netgenerate` | 136 test dirs |
| `tests/marouter` | `marouter` | 139 test dirs |
| `tests/jtrrouter` | `jtrrouter` | 104 test dirs |
| `tests/polyconvert` | `polyconvert` | 61 test dirs |
| `tests/activitygen` | `activitygen` | 19 test dirs |

*counted via `find <dir> -type d`; includes non-leaf organizational directories, so these
are orders-of-magnitude indicators, not exact scenario counts.

Within `tests/sumo`, behaviour-oriented subdirectories include (subdirectory counts from
`find tests/sumo/<name> -mindepth 2 -maxdepth 2 -type d`):

| Subsystem folder | Scenario subdir count |
|---|---|
| `bugs` (regression tickets) | 310 |
| `cf_model` (car-following) | 240 |
| `sublane_model` | 233 |
| `extended` | 222 |
| `rail` | 215 |
| `lc_model` (lane-changing) | 147 |
| `devices` | 150 |
| `junction_model` | 130 |
| `tls` (traffic lights) | 115 |
| `meso` | 82 |
| `action_step_length` | 55 |
| `pedestrian_model` | 52 |
| `opposite_direction_driving` | 48 |
| `gui` | 18 |

`tests/complex` additionally contains dedicated directories for `traci/` (per-API-domain:
`vehicle`, `person`, `trafficlight`, `junction`, `lane`, `edge`, `simulation`,
`inductionloop`, `lanearea`, `multientryexit`, `parkingarea`, `rerouter`,
`variablespeedsign`, `overheadwire`, `chargingstation`, `calibrator`, `poi`, `polygon`,
`route`, `routeprobe`, `busstop`, `vehicletype`, `gui`, `ContextSubscriptionFilters`,
`contextSubscriptions`, `misc`, `bugs`), `traci_java/` and `traas/` (Java TraCI client),
`state/` (save/load simulation state), `emissions/`/`emissionsMap/`, `fmi/` (FMI
co-simulation), `simpla/` (platooning add-on), `scenario_generation/`, `tutorial/`
(worked examples such as `hello`, `manhattan`, `city_mobil`, `traci_pedestrian_crossing`,
`traci_tls`, `traci_taxi`, `gtfs`), and `unit_tests/` (a TextTest-driven wrapper that
invokes the gtest binaries — `testcommon`, `testfoxtools`, `testgeom`, `testlibsumo`,
`testmicrosim`, `testnetbuild` — showing the two testing layers are cross-wired: the
functional suite can invoke the unit-test binaries as one more "app").

`tests/netedit` is organized by editor concern: `basic`, `bugs`, `elements`, `grid`,
`network`, `processing`, `scripts`, `selection`, `viewport`.

`tests/traci` (protocol-level, separate from `tests/complex/traci`) is organized by
TraCI mechanism: `context_subscription`, `get_variable`, `set_variable`, `testAPI`,
`variable_subscription`.

## 4. C++ unit test inventory (gtest)

| Test file | Class / behaviour under test |
|---|---|
| `unittest/src/microsim/MSCFModelTest.cpp` | `MSCFModel` base class: `brakeGap`, static `brakeGap`, static `freeSpeed` (incl. half-step variant) |
| `unittest/src/microsim/MSCFModel_IDMTest.cpp` | `MSCFModel_IDM`: `brakeGap`, `getSecureGap` |
| `unittest/src/microsim/MSEventControlTest.cpp` | `MSEventControl::execute` (via a `CommandMock`) |
| `unittest/src/netbuild/NBHeightMapperTest.cpp` | `NBHeightMapper::getZ` |
| `unittest/src/netbuild/NBTrafficLightLogicTest.cpp` | `NBTrafficLightLogic::getDuration` |
| `unittest/src/utils/common/FileHelpersTest.cpp` | `FileHelpers`: `checkForRelativity`, `getConfigurationRelative`, `getFilePath`, `fixRelative` |
| `unittest/src/utils/common/RGBColorTest.cpp` | `RGBColor`: `parseColor` (valid/invalid/short forms), `interpolate`, `operator==` |
| `unittest/src/utils/common/RandHelperTest.cpp` | `RandHelper`: ranged/uniform/normal random generation, `rand()` sequencing |
| `unittest/src/utils/common/StringTokenizerTest.cpp` | `StringTokenizer`: split on whitespace/newline/char, `reinit`, `size`, `front`, `get`, `getVector` |
| `unittest/src/utils/common/StringUtilsTest.cpp` | `StringUtils`: `prune`, `to_lower_case`, `latin1_to_utf8`, umlaut conversion, `replace`, `escapeXML`, `toInt` |
| `unittest/src/utils/common/ValueTimeLineTest.cpp` | `ValueTimeLine`: get/overwrite with and without "collect", gap filling |
| `unittest/src/utils/foxtools/MFXWorkerThreadTest.cpp` | `MFXWorkerThread`: init, task retrieval |
| `unittest/src/utils/geom/BoundaryTest.cpp` | `Boundary`: add, center, width/height, `around`, `overlapsWith`, `crosses`, `partialWithin`, `flipY`, `moveby` |
| `unittest/src/utils/geom/GeoConvHelperTest.cpp` | `GeoConvHelper`: `x2cartesian`, `cartesian2geo` |
| `unittest/src/utils/geom/GeomHelperTest.cpp` | `GeomHelper`: `intersects`, `intersection_position2D`, `closestDistancePointLine` (basic/on-line/outside cases) |
| `unittest/src/utils/geom/PositionVectorTest.cpp` | `PositionVector`: `around`, `area`, `scaleRelative`, `getCentroid`, `getPolygonCenter`, `getBoxBoundary`, `splitAt`, `intersectsAtLengths2D`, `nearest_offset_to_point2D`, `extrapolate2D` |
| `unittest/src/utils/threadpool/ThreadPoolTest.cpp` | Generic task-queue/thread-pool systems (`multiQueueTaskSystem`, `stealingTaskSystem`) via `DO_TEST` macro |

Total in this checkout: 17 `*Test.cpp` files and 105 `TEST`/`TEST_F` declarations,
almost entirely
low-level utility and numeric-formula coverage. There is **no** gtest coverage found for
`MSVehicle`, `MSLane`, `MSEdge`, `MSJunction`, `MSLink`, lane-changing models, insertion,
routing (`RO*`), or TraCI/libsumo server code — that behaviour is exercised only by the
functional suite (see below).

## 5. Behaviour → Implementation → Test coverage map

| Behaviour | Primary implementation | Dedicated test coverage |
|---|---|---|
| Car-following models | `src/microsim/cfmodels/MSCFModel*.{h,cpp}` (Krauss, IDM, ACC, CACC, Wiedemann, PWagner, etc.) | **Strong (functional)**: `tests/sumo/cf_model/**` (240 scenario dirs, incl. per-model subfolders like `ACC/`, `CACC/`, plus `bugs/`); **thin (unit)**: only base `MSCFModel` and `MSCFModel_IDM` have gtest cases |
| Lane-changing | `src/microsim/lcmodels/MS*LaneChangeModel*.{h,cpp}` | **Strong (functional)**: `tests/sumo/lc_model/**` (147 dirs), `tests/sumo/sublane_model/**` (233 dirs for sublane-level LC), `tests/sumo/opposite_direction_driving/**` (48 dirs); **none found (unit)** |
| Traffic lights / junction logic | `src/microsim/traffic_lights/MSTrafficLightLogic*`, `src/netbuild/NBTrafficLightLogic*`, `MSTLLogicControl` | **Strong (functional)**: `tests/sumo/tls/**` (115 dirs); `tests/complex/traci/trafficlight/**`; **thin (unit)**: only `NBTrafficLightLogicTest.cpp::getDuration` (netbuild-side, static duration logic) — no gtest for the runtime `MSTrafficLightLogic` switching logic |
| Junctions / right-of-way | `src/microsim/MSJunction*`, `MSLink` | **Strong (functional)**: `tests/sumo/junction_model/**` (130 dirs); **none found (unit)** |
| Routing | `src/router/RO*`, `src/microsim/devices/MSRoutingEngine.*`, `duarouter`/`jtrrouter`/`marouter`/`dfrouter` tools | **Strong (functional, tool-level)**: `tests/duarouter/**` (767), `tests/jtrrouter/**` (104), `tests/marouter/**` (139), `tests/dfrouter/**` (205); **none found (unit)** — no gtest for `ROEdge`/`RORoute`/routing algorithms |
| Network building | `src/netbuild/NB*` (`NBNetBuilder`, `NBEdge`, `NBNode`, ...) | **Functional**: `tests/netconvert/**` (1796 dirs), `tests/complex/netconvert/**` (roundtrip tests for OSM/OpenDRIVE/MATSim/plain); **thin (unit)**: only `NBHeightMapperTest` and `NBTrafficLightLogicTest` — the core edge/node/junction-computation classes (`NBEdge`, `NBNode`, `NBNetBuilder`) have no gtest coverage |
| Pedestrians | `src/microsim/transportables/MSPModel*` | **Moderate (functional)**: `tests/sumo/pedestrian_model/**` (52 dirs), `tests/complex/tutorial/traci_pedestrian_crossing`; **none found (unit)** |
| Public transport / stops | `src/microsim/MSStoppingPlace.h`, `MSDevice_Taxi`, GTFS import tooling | **Moderate (functional)**: scattered under `tests/complex/state` (bus/parking stop persistence), `tests/complex/traci/{busstop,chargingstation,parkingarea}`, `tests/complex/gtfs_mitte`, `tests/complex/tutorial/{gtfs,traci_taxi}`; **none found (unit)** |
| TraCI / libsumo | `src/traci-server/TraCIServer*`, `src/libsumo/*` | **Strong (functional, protocol + API level)**: `tests/traci/**` (726, protocol-level: `get_variable`, `set_variable`, `context_subscription`, `variable_subscription`, `testAPI`), `tests/complex/traci/**` (per-domain API tests), `tests/complex/traci_java`/`traas` (Java client), `tests/complex/unit_tests/testlibsumo` (gtest-via-texttest bridge); **no direct gtest** for `TraCIServer`/`Helper` classes themselves |
| Meso/mesoscopic simulation | `src/mesosim/ME*` | **Moderate (functional)**: `tests/sumo/meso/**` (82 dirs), `testsuite.sumo.meso` app variant | **none found (unit)** |
| Rail signals/crossings/constraints | `MSRailSignal*`, `MSDriveWay`, `MSRailCrossing` | **Moderate (functional)**: `tests/sumo/rail/**` (215 dirs) plus traffic-light/TraCI integration; **none found (unit)** |
| Battery/station finder/electric hybrid | `MSDevice_Battery`, `MSDevice_StationFinder`, `MSDevice_ElecHybrid`, charging/wire infrastructure | **Moderate (functional)**: `tests/sumo/devices/{battery,stationfinder,elechybrid}/**` and `tests/complex/traci/{chargingstation,overheadwire}/`; **none found (unit)** |
| GUI (sumo-gui) | `src/gui/*`, `src/utils/gui/*` | **Thin (functional)**: `tests/sumo/gui/**` (18 dirs), `tests/complex/sumo-gui/**` (fcdReplay, stateReplay, pyautogui_start_stop) | **none found (unit)** |
| netedit (network editor GUI) | `src/netedit/GNE*` | **Very strong (functional, largest suite)**: `tests/netedit/**` (11767 dirs across `basic`, `network`, `elements`, `selection`, `processing`, `grid`, `viewport`, `bugs`) | **none found (unit)** |
| Geometry/math utilities | `src/utils/geom/*` | **Strong (unit)**: `BoundaryTest`, `GeoConvHelperTest`, `GeomHelperTest`, `PositionVectorTest` | (functional coverage indirectly via everything that uses geometry) |
| Basic string/file/random utils | `src/utils/common/*` | **Strong (unit)**: `FileHelpersTest`, `RGBColorTest`, `RandHelperTest`, `StringTokenizerTest`, `StringUtilsTest`, `ValueTimeLineTest` | n/a |

**Summary judgment**: gtest unit coverage is narrow and utility-focused (geometry, strings,
random numbers, event scheduling, two car-following formulas, two netbuild helpers). All
"interesting" behavioural/emergent logic — car-following dynamics, lane-changing,
junction/right-of-way arbitration, traffic-light switching, routing, network computation,
TraCI semantics, GUI/netedit — is verified exclusively by the large black-box functional
suite in `tests/`, not by white-box unit tests. A new simulator built from this codebase's
design would need to reconstruct behavioural unit tests from scratch; the functional suite
is the closest thing to a spec for expected numeric outputs but exercises the *whole*
pipeline (CLI → parsing → simulation → output writers) rather than isolated components.

## 6. Regression-testing methodology (inferred)

**Confidence: Medium — inferred from repository structure and commit history, not from a
written test-strategy document.**

- Every functional test scenario stores a full expected transcript (`output.sumo`,
  `errors.sumo`, `*.agg` aggregation variants, `fcd.sumo`, etc.) checked into git next to
  its inputs.
- Comparison is **exact text diff after regex normalization** (see `[run_dependent_text]`
  in `tests/config_all`), not a numeric-tolerance comparison — timestamps, durations, and
  version banners are regex-replaced with placeholders before comparing; everything else,
  including all simulated numeric output (positions, speeds, timings), must match exactly.
- Because SUMO's simulation output is fully deterministic given fixed inputs and a fixed
  RNG seed (`RandHelper`, tested directly in `RandHelperTest.cpp`), any change to the
  physics/logic — even a legitimate bug fix or precision improvement — shifts trailing
  decimal digits of position/speed output across potentially thousands of scenario files
  simultaneously.
- This is consistent with the repository's git history, which is dominated by frequent,
  narrowly-scoped commits of the form `patching expected results refs #21, #<issue>`
  (see `git log --oneline`: e.g. `d8461b9d330`, `e43f3d38c3e`, `b927dfc12c7`,
  `6ee6a54c1b4`) — these are not new tests but **re-baselining** of previously-checked-in
  expected output after an intentional behavioural change, most likely produced by running
  `tests/updateTestsuiteFiles.py` and reviewing/committing the diff.
  Confidence: Medium — inferred; the commit messages and tooling are consistent with this
  workflow but no CONTRIBUTING doc found during this investigation states the policy
  explicitly.
- The ticket-tracking convention (`refs #21, #<issue>`) suggests issue `#21` is a
  long-lived umbrella/meta-ticket for "expected result maintenance," with the second
  number identifying the specific behavioural change that necessitated re-baselining.
- `tests/sumo/bugs/**` (310 scenario dirs) and `tests/sumo/cf_model/bugs/**` and similar
  `bugs/` subfolders elsewhere show the project's practice of adding a permanent
  regression scenario for each fixed ticket, keyed by ticket number
  (e.g. `tests/sumo/cf_model/bugs/ticket3922`).
