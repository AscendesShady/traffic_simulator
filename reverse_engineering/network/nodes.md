# Nodes: NBNode (build-time) and MSJunction (runtime)

## NBNode — build-time junction

`NBNode` (`src/netbuild/NBNode.h/.cpp`) represents a junction while
netconvert/netgenerate/netedit is constructing the network. It owns far more
transient/derivation state than its runtime counterpart because it is both
the *input* to and *output* of the junction-shape/right-of-way inference
algorithms.

Core stored data (`private:` section, `src/netbuild/NBNode.h` L942-1024):

| Field | Type | Meaning |
|---|---|---|
| `myPosition` | `Position` | Nominal junction center (from input node data) |
| `myIncomingEdges`, `myOutgoingEdges`, `myAllEdges` | `EdgeVector` | Edges ending/starting/touching this node |
| `myType` | `SumoXMLNodeType` | `priority`, `traffic_light`, `right_before_left`, `unregulated`, `rail_signal`, `rail_crossing`, `zipper`, `district`, ... |
| `myPoly` | `PositionVector` | Computed (or user-set) junction outline shape |
| `myRequest` | `NBRequest*` | The right-of-way / foe-relationship matrix for this junction (see algorithms doc) |
| `myTrafficLights` | `std::set<NBTrafficLightDefinition*>` | TLS programs controlling this node, if any |
| `myCrossings`, `myWalkingAreas` | vectors | Pedestrian infrastructure at this junction |
| `myBlockedConnections` | `NBConnectionProhibits` | Explicit "connection A blocks connection B" relationships |
| `myRadius`, `myKeepClear`, `myRightOfWay`, `myFringeType`, `myRoundaboutType` | scalars/enums | Per-node tunables affecting shape/behavior computation |

### Junction shape computation

`NBNode::computeNodeShape(double mismatchThreshold)` computes `myPoly`
(delegated to `NBNodeShapeComputer`, not detailed here) from the incident
edges' end geometries and widths; it is invoked once edges are sorted
clockwise (`NBNodesEdgesSorter::sortNodesEdges`) and edge shapes are known.
`NBNode::updateSurroundingGeometry()` re-derives edge end-geometry after the
node shape changes — shape computation is iterative/mutually dependent
between nodes and their incident edges.

```
Source:
src/netbuild/NBNode.h (myPoly L974, computeNodeShape L556, updateSurroundingGeometry L559)
src/netbuild/NBNodeShapeComputer.h/.cpp (shape algorithm, not read in depth for this doc)
```

### Lane-to-lane connection computation entry points

`NBNode::computeLanes2Lanes()` is the per-node driver that (via the nested
`ApproachingDivider` Bresenham callback) decides, for each outgoing edge,
which of the incoming lanes feed which of its lanes — spreading connections
left/right from a "central" approach lane. `NBNode::computeLogic()` /
`computeLogic2()` then derive the `NBRequest` right-of-way matrix, and
`buildInnerEdges()`/`buildCrossings()`/`buildWalkingAreas()` materialize
internal lanes and pedestrian infrastructure. See
`docs/reverse_engineering/algorithms/network_generation.md` for the full
decision process.

```
Source:
src/netbuild/NBNode.h (class ApproachingDivider L85-132; computeLanes2Lanes L397; computeLogic L400; computeLogic2 L403; buildInnerEdges L693)
```

### Pruning / topology-editing operations

`NBNode` also implements node-level graph-editing primitives used by
`NBNetBuilder::compute()`: `removeSelfLoops`, `replaceIncoming`/
`replaceOutgoing` (used when joining/splitting edges), `checkIsRemovable`
(geometry-only nodes that can be collapsed), `geometryLike()` (a node with
exactly one through-connection, i.e. no real decision point).

```
Source:
src/netbuild/NBNode.h (removeSelfLoops L371, replaceIncoming/replaceOutgoing L668-677, checkIsRemovable L608, geometryLike L729-730)
```

## MSJunction — runtime junction

`MSJunction` (`src/microsim/MSJunction.h`) is deliberately thin: by the time
a `.net.xml` is loaded, all shape/right-of-way inference is already done and
persisted in the file, so the runtime object only needs to *hold* and
*expose* that data, not compute it.

Stored data (`protected:`, L145-168):

| Field | Type | Meaning |
|---|---|---|
| `myType` | `SumoXMLNodeType` | Same enum as build-time; loaded from `<junction type="...">` |
| `myPosition`, `myPosition2` | `Position` | Junction center (secondary position used by GUI) |
| `myShape` | `PositionVector` | Junction outline, loaded verbatim from `.net.xml`, not recomputed |
| `myName` | `std::string` | Optional human-readable name |
| `myIncoming`, `myOutgoing` | `ConstMSEdgeVector` | Incident edges (populated via `addIncoming`/`addOutgoing` during loading) |

Behavioral surface is minimal: `getFoeLinks()`, `getFoeInternalLanes()`, and
`getLogic()` return empty/`nullptr` by default and are overridden by the
subclass that actually enforces right-of-way:

- `MSNoLogicJunction` — no conflict checking (e.g. `unregulated`, or nodes
  where only one stream can ever be present).
- `MSRightOfWayJunction` — holds an `MSJunctionLogic` foe/response matrix
  (the runtime counterpart of `NBRequest`) and implements TLS-free
  priority/right-before-left resolution.
- `MSInternalJunction` — represents the small "virtual" junction inside a
  multi-step internal-lane crossing (used only when internal links are
  enabled and a junction requires more than one internal lane segment in
  sequence).

`MSJunction::postloadInit()` performs any post-parse fixups (e.g. building
per-link foe caches) once the whole network graph is available.

```
Source:
src/microsim/MSJunction.h (class MSJunction; getFoeLinks/getFoeInternalLanes L100-106; getLogic L141; postloadInit L78)
src/microsim/MSNoLogicJunction.h (class MSNoLogicJunction)
src/microsim/MSRightOfWayJunction.h (class MSRightOfWayJunction)
src/microsim/MSInternalJunction.h (class MSInternalJunction)
```

## Build-time → runtime mapping

| NBNode concept | MSJunction realization |
|---|---|
| `myType` (`SumoXMLNodeType`) | `myType`, same enum, written/read verbatim through `.net.xml` `<junction type>` |
| `myPoly` (computed shape) | `myShape`, loaded verbatim (not recomputed at runtime) |
| `myRequest` (`NBRequest`, foe matrix) | `MSJunctionLogic` inside `MSRightOfWayJunction`, loaded from the `<junction>`'s serialized request/response bitsets in `.net.xml` |
| `myCrossings`/`myWalkingAreas` | Represented as ordinary `MSEdge`s with `SumoXMLEdgeFunc::CROSSING` / `WALKINGAREA` plus their lanes, not as `MSJunction` fields |
| `myTrafficLights` | Not stored on `MSJunction` itself; TLS control lives in `MSTLLogicControl`/`MSTrafficLightLogic`, referenced by the junction's links via their `tlID` |

Confidence: Medium — the mapping table is inferred from field/name
correspondence and the shared XML schema; the `.net.xml` junction/request
element parsing code itself (`NLJunctionControlBuilder`/`NLHandler`) was
inspected only at the header/class level, not read line-by-line.
