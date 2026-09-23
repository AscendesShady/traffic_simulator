# Junctions: topology, right-of-way, and runtime arbitration

## Purpose

Junctions turn lane-to-lane movements into an explicit conflict model. The
build-time representation computes shapes, internal lanes, link ordering, foe
sets, and response rules. The runtime representation applies those rules while
vehicles approach and traverse links.

Primary implementation:

- `src/netbuild/NBNode.h`, `src/netbuild/NBNode.cpp`
- `src/netbuild/NBNodeCont.cpp`
- `src/microsim/MSJunction.h`
- `src/microsim/MSLogicJunction.*`, `MSRightOfWayJunction.*`,
  `MSInternalJunction.*`, `MSNoLogicJunction.*`
- `src/microsim/MSLink.h`, `src/microsim/MSLink.cpp`

Related tests:

- `tests/sumo/junction_model/`
- `tests/netconvert/function/junctions.join/`
- `tests/netconvert/node_building/`
- `tests/complex/traci/junction/`

## Build-time responsibilities

`NBNode` owns incoming/outgoing `NBEdge` references, node type, position and
shape, crossings/walking areas, and traffic-light associations. Important
passes include:

- `NBNode::computeLanes2Lanes()` — create/repair lane-level movements.
- `NBNode::computeLogic()` and `computeLogic2()` — derive conflict/response
  matrices.
- `NBNode::computeNodeShape()` and `computeInternalLaneShape()` — derive
  junction and connector geometry.
- `NBNode::needsCont()` — determine when an internal continuation junction is
  required.
- `NBNodeCont::joinJunctions()`, `computeLanes2Lanes()`, `computeLogics*()`, and
  `computeNodeShapes()` — apply operations over the network.

The generated response/foe bitsets and connection order are serialized by
`NWWriter_SUMO::writeJunction()` and connection writers. Runtime code consumes
their order; it is not safe to reorder connections without regenerating link
indices and signal mappings.

## Runtime classes

| Class | Responsibility |
|---|---|
| `MSJunction` | common ID/position/shape contract |
| `MSLogicJunction` | incoming/internal lane collections and common post-load initialization |
| `MSRightOfWayJunction` | initializes right-of-way conflicts from request/response logic |
| `MSInternalJunction` | conflict handling at an internal-lane split/continuation |
| `MSNoLogicJunction` | junction with no arbitration logic |
| `MSLink` | one directed movement from a lane to a target/via lane; stores signal state and approaching users |

`NLJunctionControlBuilder` creates the concrete runtime junction types while
`NLEdgeControlBuilder` creates lanes and connections.

## Decision process

Vehicles plan drive items for outgoing links. `MSLane::setJunctionApproaches()`
registers predicted arrival/leave information with each `MSLink`. During speed
planning and entry, `MSLink::opened()` and `blockedAtTime()` compare an ego
movement against approaching foe vehicles/persons, link states, priority,
continuation flags, impatience, and timing. `MSLink::getLeaderInfo()` also turns
vehicles or pedestrians on conflicting paths into virtual leaders.

Traffic lights do not replace junction conflict logic. `MSLink::setTLState()`
sets the signal aspect; foe checks, keep-clear behavior, internal-lane occupancy,
and pedestrian conflicts still affect safe entry.

## State transitions

```text
network connection -> MSLink created -> foe/response relations initialized
vehicle plans link  -> approaching record installed
movement executes   -> link opening rechecked -> internal/target lane entered
vehicle passes      -> approaching record removed
```

`MSLink::clearState()` and junction/state loading must clear or reconstruct the
approach maps; stale approach records can block or admit movements incorrectly.

## Edge cases

- Internal lanes disabled: movements connect directly, reducing geometric
  fidelity and changing where conflicts are represented.
- Zipper links use `MSLink::getZipperSpeed()` instead of ordinary priority.
- Walking areas and crossings add person conflicts (`checkWalkingAreaFoe()`).
- Rail signals and bidirectional rail movements add specialized link semantics.
- Keep-clear and `ignoreJunctionBlocker` can change whether downstream blockage
  prevents junction entry.
- Junction collision checks are option-controlled and may run even after link
  arbitration.

## Invariants

- Every runtime outgoing movement references valid from/to lanes and, when
  present, a valid via lane.
- Link indices, request/response bit positions, and traffic-light state strings
  describe the same movement order.
- Approach records belong to live traffic objects and are removed when plans
  change or objects leave.
- A vehicle must not enter a closed movement merely because its free-road speed
  allows it.

## Modification points

Change topology/conflict generation in netbuild; change dynamic admission in
`MSLink`; change signal policy in `microsim/traffic_lights`. A junction behavior
change normally requires all three test families: netconvert output, SUMO
junction scenarios, and TraCI traffic-light/junction tests.

## Confidence

High for the architecture and runtime decision boundary; medium for individual
node-type heuristics, which are numerous and option-dependent.
