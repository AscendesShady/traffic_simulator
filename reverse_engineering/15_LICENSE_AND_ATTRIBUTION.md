# 15. License and attribution inventory

This is a repository evidence summary, not legal advice. Redistribution,
derivative-work, trademark and clean-room questions require qualified legal
review.

## Project license

The root `LICENSE` contains Eclipse Public License 2.0. `NOTICE.md` declares:

- `SPDX-License-Identifier: EPL-2.0 OR GPL-2.0-or-later`;
- Eclipse SUMO as an Eclipse Foundation trademark;
- copyright belonging to respective authors/employers;
- source repository at `https://github.com/eclipse-sumo/sumo`.

`NOTICE.md` also explains the project's view of modifications under `src/` and
`tools/`, and lists categories it considers separate modules in specified
circumstances. Treat that as notice text to preserve/review, not a conclusion
about a proposed reimplementation.

## Declared third-party content

The root notice lists at least:

| Content | Declared license(s) |
|---|---|
| Font Stash | Zlib and MIT or public-domain reference |
| FOX toolkit | FOX addendum, public-domain reference and X11 |
| Google Design Icons | CC-BY-4.0 |
| odrSpiral | Apache-2.0 |
| OpenStreetMap data files | ODbL-1.0 |
| PROJ | MIT, public-domain reference and Apache-2.0 |
| RTree | permissive license reference |
| tcpip | BSD-3-Clause |
| Wix templates | CPL-1.0 |
| Xerces-C++ | Apache-2.0 |
| JSON for Modern C++ | MIT |
| zstr | MIT |
| fmt | MIT |

The authoritative names/versions/details are in `NOTICE.md`; bundled code also
has local headers/notices under `src/foreign/`. Optional CMake dependencies
(Arrow/Parquet, Eigen, GDAL, FFMPEG, OpenSceneGraph, GL2PS, JuPedSim/GEOS,
Boost, gtest, zlib, gettext and others) must be inventoried from the actual
distribution rather than assumed covered by this table.

## Git submodules

`.gitmodules` declares `tools/contributed/traci4matlab`, `build_config/brew`,
and `tools/contributed/saga`. Each external repository has its own license and
revision; presence/packaging must be verified before redistribution.

## Reimplementation implications to review

- Ideas, observable behavior, APIs, file formats, copied expression, and linked
  or modified source raise different questions; this document does not decide
  them.
- Do not copy source, comments, tests, or expected-output files into a differently licensed
  project without review of EPL/GPL and third-party terms.
- Preserve notices and SPDX headers when required for material actually reused.
- Review trademark usage before naming/marketing compatibility.
- OSM-derived data and icons/assets can carry attribution/data-license duties
  separate from code.
- A clean-room behavioral specification and independently authored code may
  reduce copying risk, but counsel must define and audit the process.

## Evidence

Primary: `LICENSE`, `NOTICE.md`, `.gitmodules`, per-file SPDX headers,
`src/foreign/`, and top-level `CMakeLists.txt` dependency discovery.

## Confidence

High for what the repository declares; deliberately no legal conclusion and no
claim that the inventory covers every transitive/package-time dependency.
