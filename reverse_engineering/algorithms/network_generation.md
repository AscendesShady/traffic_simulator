# Network generation algorithms

## Purpose

Create or normalize simulation-ready nodes, edges, lanes, connections,
junction logic, traffic lights and geometry from imported or synthetic data.

## Inputs

Importer records or grid/spider/random parameters, edge/type defaults,
coordinate/projection settings, topology-cleanup options, connection/TLS rules.

## Outputs

Computed `NB*` network and serialized `.net.xml` (plus optional exports).

## State

`NBNetBuilder` containers own mutable nodes, edges, types, districts, TLS,
crossings/stops/lines/shapes. Synthetic generation first uses temporary `NG*`.

## Preconditions

Identifiers and references are parseable; coordinates and base topology are
sufficient for the selected importer/generator.

## Decision Process

Import/generate base objects, apply filtering/join/split cleanup, compute
edge-to-edge and lane-to-edge connectivity, lane-to-lane assignments,
turnarounds/special lanes/crossings/TLS, right-of-way matrices, internal lanes,
node/edge/lane shapes, and final consistency. `NBNetBuilder::compute()` encodes
the exact pass order.

## Mathematical Model

Geometry uses polylines, offsets, intersections, Bezier/smoothing and angle/
radius thresholds. Random generation samples distance/angle/connectivity.
Junction priority is discrete rule inference plus conflict geometry; no single
network optimization objective exists.

## Constraints

Unique IDs, valid endpoints/lane indices, permissions, nondegenerate geometry,
connection/TL/foe index alignment, and option-selected cleanup policy.

## State Transitions

`RAW -> IMPORTED/GENERATED NB -> NORMALIZED TOPOLOGY -> COMPUTED SEMANTICS ->
SERIALIZED IMMUTABLE RUNTIME INPUT`.

## Edge Cases

Disconnected components, self loops, duplicate/overlapping edges, tiny radii,
left-hand networks, rail/water/pedestrian modes, joined junctions, missing
projection, explicit connections conflicting with inferred ones.

## Implementation

`src/netbuild/NBNetBuilder.cpp`, `NBNode*`, `NBEdge*`, `src/netimport/`,
`src/netgen/`, `src/netwrite/`; see `../network/network_generation.md`.

## Related Classes

`NILoader`, `NGNet`, `NGRandomNetBuilder`, `NBTrafficLightLogicCont`,
`NWWriter_SUMO`.

## Related Tests

`tests/netconvert/`, `tests/netgen/`, and conversion round trips in
`tests/complex/`.

## Behavioural Invariants

Later compute passes receive the prerequisites of earlier passes; serialized
movement order aligns with logic indices; runtime never needs to infer missing
core topology.

## Reimplementation Notes

MUST preserve output graph/control semantics for accepted inputs. MAY replace
the mutable builder pipeline. OPTIONAL: support only native/plain input before
third-party formats.

## Confidence

High for phases and boundaries; medium for individual heuristics/importers.
