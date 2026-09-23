# Edges: NBEdge (build-time) and MSEdge (runtime)

## NBEdge — build-time edge

`NBEdge` (`src/netbuild/NBEdge.h/.cpp`) is the mutable representation of a
road segment while it is being imported, split, joined, or otherwise edited.

### Attributes (constructor parameters / stored fields)

| Attribute | Field | Notes |
|---|---|---|
| id | `Named::myID` (base class) | |
| from/to node | `myFrom`, `myTo` (`NBNode*`) | Set at construction; can be repointed by `reinitNodes` when joining |
| type string | `myType` | Refers to an edge-type default (`NBTypeCont`); `getTypeID()` |
| speed | `mySpeed` | m/s |
| friction | `myFriction` | |
| number of lanes | `myLanes.size()` | `getNumLanes()` |
| priority | `myPriority` | Integer; `JunctionPriority` enum defines `MINOR_ROAD=0`, `PRIORITY_ROAD=1`, `ROUNDABOUT=1000` |
| lane width | `myLaneWidth` (edge default) / per-lane `Lane::width` | |
| end offset | `myEndOffset` / per-lane `Lane::endOffset` | Setback from the junction |
| geometry | `myGeom` (`PositionVector`) | Edge shape; per-lane shapes derived via `computeLaneShapes()`/spread function |
| lane spread | `LaneSpreadFunction` | `center` vs `right`, controls lateral lane offset computation |
| street name | `myStreetName` | Non-unique display name |

`NBEdge::Lane` (nested struct, L143-196) carries **per-lane overrides**:
shape, speed, friction, `permissions`/`preferred` (`SVCPermissions`
bitmasks), `changeLeft`/`changeRight` (lane-change permission bitmasks),
`endOffset`, `laneStopOffset`, `width`, `oppositeID` (opposite-direction
lane pairing, for overtaking), `accelRamp` flag, `customShape`, lane `type`
string, and OSM-derived `turnSigns`.

`NBEdge::Connection` (nested struct, L198-340) is the actual persisted
lane-to-lane turning connection record — see
`docs/reverse_engineering/network/connections.md`.

```
Source:
src/netbuild/NBEdge.h (class NBEdge L92; struct Lane L143; struct Connection L198; enum JunctionPriority L384)
```

### Building step state machine

`NBEdge::EdgeBuildingStep` (`enum class`, L109-124) tracks how far
connection-computation has progressed for this edge:
`INIT_REJECT_CONNECTIONS` → `INIT` → `EDGE2EDGES` → `LANES2EDGES` →
`LANES2LANES_RECHECK` | `LANES2LANES_DONE` | `LANES2LANES_USER`.
`addEdge2EdgeConnection`/`addLane2LaneConnection` transition the edge
through these states and reject connection attempts made in
`INIT_REJECT_CONNECTIONS` (used for edges whose connections must come
verbatim from user/import data, never inferred).

### Container-level operations (`NBEdgeCont`)

`NBEdgeCont` (`src/netbuild/NBEdgeCont.h`) is the dictionary
(`std::map<std::string, NBEdge*>`) and drives batch operations invoked from
`NBNetBuilder::compute()`: `computeLaneShapes()`, `computeEdgeShapes()`,
`computeEdge2Edges()`, `computeLanes2Edges()`, `joinSimilarEdges`,
`splitGeometry`, `guessRoundabouts`, `guessSpecialLanes` (bike/sidewalk
guessing), `appendTurnarounds`, `recheckLanes()`.

```
Source:
src/netbuild/NBNetBuilder.cpp (call sequence, e.g. L440,521,535,554,571)
```

## MSEdge — runtime edge

`MSEdge` (`src/microsim/MSEdge.h/.cpp`) is largely immutable after
`closeBuilding()`: topology (successors/predecessors, allowed lanes per
vClass) is precomputed into caches so that the simulation's per-step routing
and lane-selection queries are O(1)/cache-lookup rather than recomputation.

### Attributes

| Attribute | Accessor | Notes |
|---|---|---|
| numerical id | `getNumericalID()` (`myNumericalID`, `const int`) | Dense index for array-based router/edge-weight storage |
| function | `getFunction()` (`SumoXMLEdgeFunc myFunction`, `const`) | `NORMAL`, `INTERNAL`, `CROSSING`, `WALKINGAREA`, `CONNECTOR` (TAZ connector) |
| lanes | `getLanes()` → `const std::vector<MSLane*>&` | Owned via `myLanes` pointer-to-vector |
| priority | `getPriority()` | Loaded from `.net.xml`, informational for routing/right-of-way display |
| distance/mileage | `getDistance()`, `getDistanceAt(pos)` | Kilometrage annotation, e.g. for railways |
| street name / edge type / routing type | `getStreetName()`, `getEdgeType()`, `getRoutingType()` | |
| successors / predecessors | `mySuccessors`, `myPredecessors` (`MSEdgeVector`) | Cached in `recalcCache()` |
| bidi edge | `getBidiEdge()` (`myBidiEdge`) | Opposite-direction "superposable" edge, mainly for rail |

### Edge function / type classification

`isNormal()`, `isInternal()`, `isCrossing()`, `isWalkingArea()`,
`isTazConnector()` are all thin wrappers over the single `myFunction` enum
comparison — this is the runtime equivalent of asking "is this a real road,
an intersection-interior segment, a pedestrian crossing, a walking area, or
a virtual TAZ source/sink connector".

```
Source:
src/microsim/MSEdge.h (getFunction L259; isNormal L264; isInternal L269; isCrossing L274; isWalkingArea L288; isTazConnector L292; myFunction L946)
```

### Lane/successor caching for routing

`allowedLanes(destination, vclass, ignoreTransientPermissions)` and
`allowedLanes(vclass)` look up precomputed `AllowedLanesCont`
(`std::vector<std::pair<SVCPermissions, shared_ptr<const vector<MSLane*>>>>`)
keyed by destination edge (`AllowedLanesByTarget`,
`std::map<const MSEdge*, AllowedLanesCont>`) — i.e. "which lanes of *this*
edge may a vehicle of class X use in order to reach edge Y". This is
rebuilt by `recalcCache()` whenever the network's permission/connection
data is loaded or changed and is central to routing correctness.
`getSuccessors(vClass)`/`getViaSuccessors(vClass, ignoreTransientPermissions)`
give the router the reachable-edges graph directly, including via-edges
(internal edges) when relevant.

```
Source:
src/microsim/MSEdge.h (AllowedLanesCont/AllowedLanesByTarget L79-83; allowedLanes L229-242; getSuccessors L397; getViaSuccessors L403; recalcCache L119)
```

### Edge lifecycle

`MSEdge(id, numericalID, function, streetName, edgeType, routingType,
priority, distance)` constructs a not-yet-usable edge; `initialize(lanes)`
attaches the lane vector; `closeBuilding()` (called once, after all edges
and connections in the network are loaded) finalizes cross-edge caches; and
`buildLaneChanger()` (called after `closeBuilding()`) sets up the
`MSLaneChanger` for multi-lane edges.

```
Source:
src/microsim/MSEdge.h (constructor L100; initialize L114; closeBuilding L123; buildLaneChanger L126)
```

## Build-time → runtime attribute mapping

| NBEdge | MSEdge | Notes |
|---|---|---|
| `myFrom`/`myTo` (`NBNode*`) | not directly stored; edges are wired to `MSJunction` via `MSJunction::myIncoming/myOutgoing` (populated at load time), not an `MSEdge` field | `getFromJunction()`/`getToJunction()` exist on `MSEdge` (`src/microsim/MSEdge.h` L423,427) — inferred to be set during `NLEdgeControlBuilder`/`NLJunctionControlBuilder` loading, not derived at runtime |
| `myLanes` (`vector<NBEdge::Lane>`, value type, mutable) | `myLanes` (`const vector<MSLane*>*`, pointer to heap-allocated `MSLane` objects) | Lane count and per-lane attributes are frozen once written to `.net.xml` |
| `myConnections` (`vector<NBEdge::Connection>`) | Represented as `MSLink` objects owned per-lane (`MSLane::myLinks`), not per-edge | See connections.md |
| `EdgeBuildingStep myStep` | none — runtime edges have no "in progress" state | |
| `myPriority` (mutable, recomputed by `NBEdgePriorityComputer`) | `myPriority` (loaded, `const`-like usage) | |
| internal/normal distinguished implicitly (edges created during `buildInnerEdges()` get special IDs like `:nodeID_n`) | explicit `SumoXMLEdgeFunc` enum (`NORMAL`/`INTERNAL`/...) | Runtime makes the internal/normal/crossing/walkingarea distinction a first-class typed field rather than an ID convention |

Confidence: Medium — the "not directly stored" claim about `from`/`to`
`NBNode`↔`MSJunction` wiring is inferred from `getFromJunction()`/
`getToJunction()` existing as accessors without a visible corresponding
`NBEdge`-style raw node pointer field in the header excerpt reviewed; the
`NLJunctionControlBuilder`/`NLEdgeControlBuilder` wiring code that actually
sets this was not read line-by-line.
