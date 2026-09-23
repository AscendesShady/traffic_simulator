# Connections and links

## Purpose

A connection is the legal, directed movement from one lane to another. At
build time it is an `NBEdge::Connection`; in a loaded simulation it becomes an
`MSLink` attached to the source `MSLane`. Connections are the join point for
topology, route continuity, traffic-light indices, junction conflicts, internal
lanes, and vehicle-class permission.

Primary implementation:

- `src/netbuild/NBEdge.h`, `src/netbuild/NBEdge.cpp`
- `src/netbuild/NBNode.cpp`
- `src/netwrite/NWWriter_SUMO.cpp`
- `src/netload/NLEdgeControlBuilder.cpp`, `NLHandler.cpp`
- `src/microsim/MSLink.h`, `src/microsim/MSLink.cpp`

Related tests: connection-focused cases below `tests/netconvert/function/` and
`tests/netconvert/node_building/`, plus `tests/sumo/junction_model/` and
`tests/complex/traci/connection/`.

## Build-time record

The connection record identifies destination edge and lane, source lane,
optional internal/via geometry, direction, permissions, uncontrolled/pass flags,
keep-clear behavior, speed, traffic-light ID/link indices, and custom conflict
information. Importers may load explicit connections; otherwise
`NBEdgeCont::computeEdge2Edges()`, `NBEdge::computeLanes2Edges()`, and
`NBNode::computeLanes2Lanes()` infer them.

`NBNode::computeLogic*()` derives response/foe relations after connections are
stable. `NWWriter_SUMO::writeConnection()` and `writeInternalConnections()`
serialize normal and internal movements.

## Runtime object

`MSLink` references:

- the destination lane and optional via lane;
- movement direction and link state;
- foe links/lanes and internal-lane relationships;
- approaching vehicles/persons with predicted arrival and leave data;
- optional controlling traffic-light logic;
- keep-clear, visibility, and continuation metadata.

The source lane owns its outgoing link vector (`MSLane::addLink()`). Routes are
edge sequences, so `MSLane::succLinkSec()` selects the lane-level link compatible
with the vehicle's next route edge and continuation lanes.

## Runtime contract

`MSLink::opened()` answers whether a movement is usable at a predicted arrival
window. It incorporates signal state and `blockedAtTime()` conflict checks.
`setApproaching()`/`removeApproaching()` maintain the temporal reservation-like
view used by foes. `getLeaderInfo()` supplies physical/conflict leaders to the
vehicle speed planner.

This is not a hard reservation system: arrival estimates can change each step,
and final movement remains subject to current lane and collision logic.

## Invariants and change safety

- Destination and via lanes must be topologically consistent.
- Lane indices are local to their edges and must remain in range.
- Traffic-light state character at index *i* must control the connection whose
  TL link index is *i*.
- Foe/response bitsets use the same connection ordering written in the network.
- Route continuity at edge level is insufficient if no permitted lane-level
  connection exists for the vehicle class.

Safest change point for imported connectivity is netbuild/importer code followed
by a complete recomputation. Editing serialized connection indices or runtime
link vectors independently is high risk.

## Confidence

High — the build/serialize/load/runtime chain is explicit in the cited classes.
