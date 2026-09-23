# netconvert

## Purpose

`netconvert` imports one or more network descriptions, normalizes them into
SUMO's build-time network model, computes missing traffic semantics, and writes
a simulation-ready network plus optional alternative exports.

Primary implementation:

- `src/netconvert_main.cpp`
- `src/netimport/NIFrame.*`, `NILoader.*`, `NIImporter_*`
- `src/netbuild/NBFrame.*`, `NBNetBuilder.*`, `NB*`
- `src/netwrite/NWFrame.*`, `NWWriter_*`

Related tests: `tests/netconvert/` and network conversion/round-trip cases under
`tests/complex/`.

## Execution flow

1. `fillOptions()` composes option sets from `SystemFrame`, `NIFrame`,
   `NBFrame`, and `NWFrame`.
2. `OptionsIO::getOptions()` merges configuration and command line.
3. `checkOptions()` validates import, build, and writer options.
4. `NBNetBuilder nb` creates the shared mutable model.
5. `NILoader::load()` invokes every importer whose input option is set.
6. `NBNetBuilder::compute()` runs the ordered topology/geometry/control passes.
7. `NWFrame::writeNetwork()` dispatches SUMO and optional output writers.

Source: `src/netconvert_main.cpp:51` (`fillOptions`), `:82`
(`checkOptions`), and `:96` (`main`).

## Input and transformation contract

Importers do not write final networks directly. They populate `NBNodeCont`,
`NBEdgeCont`, `NBTypeCont`, `NBTrafficLightLogicCont`, district, stop/line, and
shape containers owned by `NBNetBuilder`. This permits mixed inputs but also
means IDs, coordinate systems, and explicit connections can collide. Importer
order and options determine conflict handling.

The compute phase may split/merge/remove edges and nodes, infer lane
connections, add turnarounds, generate internal lanes, guess traffic lights and
roundabouts, calculate right-of-way matrices, and repair shapes. Treat output
as a normalized product, not a lossless copy of source data.

## Outputs

`NWWriter_SUMO::writeNetwork()` is authoritative for `.net.xml` consumed by
`sumo`. `NWFrame` can additionally call Amitran, MATSim, OpenDRIVE,
DLR/Navteq, and plain-XML writers. Optional output formats have distinct
capabilities and should not be assumed round-trip equivalent.

## Edge cases and constraints

- No usable input or no output is rejected by option validation.
- Malformed XML is reported through the common SAX/error infrastructure.
- Duplicate IDs and references to missing nodes/edges/connections are handled
  by importer/container policy; `--ignore-errors` changes some failures but
  cannot make an inconsistent graph valid.
- Coordinate projection depends on PROJ/GDAL availability for applicable
  inputs.
- Connection and traffic-light indices must be recomputed after topology edits.

## Modification points

Add formats in `netimport`/`netwrite`; add generic network behavior in
`netbuild`; add CLI options in the corresponding `*Frame`. Never embed
simulation-time state in `NB*`: runtime loading belongs to `netload`/`MS*`.

## Confidence

High — the main function and all three pipeline stages were traced.
