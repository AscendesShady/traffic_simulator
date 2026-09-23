# netgenerate

## Purpose

`netgenerate` produces synthetic grid, spider, or random networks and sends
them through the same `NBNetBuilder` and writer stack as `netconvert`.

Primary implementation:

- `src/netgen/netgen_main.cpp`
- `src/netgen/NGFrame.cpp`
- `src/netgen/NGNet.cpp`, `NGNode.cpp`, `NGEdge.cpp`
- `src/netgen/NGRandomNetBuilder.cpp`
- `src/netbuild/NBNetBuilder.cpp`, `src/netwrite/NWFrame.cpp`

Related tests: `tests/netgen/grid/`, `spider/`, `random/`, `function/`, and
`errors/`.

## Inputs

`NGFrame` registers topology selection and parameters: grid dimensions and
spacing, spider arms/circles/radius/center, random iterations/distance/angle/
connectivity/retries, default edge properties, bidirectionality, perturbation,
IDs, and junction type. Option validation prevents incompatible simultaneous
topology modes and degenerate values.

## Decision process

- Grid: `NGNet::createChequerBoard()` places indexed nodes and connects adjacent
  rows/columns, optionally adding boundary attachments.
- Spider: `NGNet::createSpiderWeb()` places radial/circular nodes and connects
  arms and rings, optionally with a center.
- Random: `NGRandomNetBuilder::createNet()` repeatedly selects/grows from
  connected nodes; `createNewNode()` samples distance and angle (or cardinal
  directions in random-grid mode), rejects invalid geometry, and uses
  connectivity probability to continue growth.

`NGNet::toNB()` converts each generated `NGNode`/`NGEdge` to `NBNode`/`NBEdge`
and may add a reverse edge according to `bidi-probability`. The result then
passes through `NBNetBuilder::compute()` before `NWFrame::writeNetwork()`.

## State and reproducibility

Generation uses `RandHelper`; random seed options in the common frame determine
repeatability. Random lane count, priority, type, bidirectionality, and spatial
perturbation consume the RNG, so adding an RNG call may change the entire
subsequent network even if local behavior appears unchanged.

## Invariants

- Exactly one generation family is selected.
- Node IDs remain unique and generated edges connect existing nodes.
- Minimum distances/angles and retry limits bound random growth.
- `NG*` is temporary topology; simulation semantics are finalized only in
  `NBNetBuilder::compute()`.

## Modification points

Add a topology family in `NGFrame` plus `netgen_main.cpp::buildNetwork`; create
only `NG*`/`NB*` data and retain the shared compute/write path. Add deterministic
fixtures and seeded random tests under `tests/netgen/`.

## Confidence

High for grid/spider/shared flow; medium for random-network geometric rejection
details, which are concentrated in `NGRandomNetBuilder`.
