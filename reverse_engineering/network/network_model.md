# Network Model: Build-Time vs Runtime

## Two distinct layers

SUMO maintains **two independent object models of a road network** that share
naming/ID conventions and an XML interchange format (`.net.xml`) but are
implemented by different classes, live in different translation units, and
are used at different times:

| Layer | Namespace/prefix | Directory | When it exists | Purpose |
|---|---|---|---|---|
| Build-time | `NB*` (NetBuilder) | `src/netbuild/` | Only inside `netconvert`/`netgenerate`/`netedit` while constructing or editing a network | Import raw, possibly inconsistent node/edge/connection data; infer junction shapes, lane counts, turn lanes, right-of-way, TLS programs; write out a consistent `.net.xml` |
| Runtime | `MS*` (MicroSim) | `src/microsim/` | Inside `sumo`/`sumo-gui` after loading a `.net.xml` | Represent the final, already-consistent network for the simulation loop: vehicle movement, right-of-way execution, routing |

The two layers never share C++ objects. `netconvert` never links against
`libmicrosim`, and `sumo` never links against `libnetbuild`. The only
contract between them is the `.net.xml` XML schema (written by
`src/netwrite/NWWriter_SUMO.cpp`, read by `src/netload/NLHandler.cpp` and
`src/netload/NLEdgeControlBuilder.cpp` / `NLJunctionControlBuilder.cpp`).
`src/netimport/NIImporter_SUMO.cpp` can also read a `.net.xml` back into the
*build-time* `NB*` model (used when re-running netconvert on an existing
network, e.g. netedit), so the same file format has two independent parsers
for the two different in-memory models.

```
Source:
src/netconvert_main.cpp (fillOptions/main — links NIFrame, NILoader, NBFrame, NBNetBuilder, NWFrame)
src/netload/NLEdgeControlBuilder.cpp (NLEdgeControlBuilder::closeEdge/beginEdgeParsing — construct MSEdge/MSLane from XML)
src/netimport/NIImporter_SUMO.h (re-import of .net.xml into NB* model)
```

## Build-time model (NB*)

Central classes, all under `src/netbuild/`:

- `NBNode` — a junction/intersection under construction: position, incident
  edges, junction shape (`myPoly`), node type (priority/traffic_light/right_before_left/...),
  crossings, walking areas, and the algorithms that compute lane-to-lane
  connections and right-of-way (`NBRequest`).
- `NBEdge` — a road segment under construction: geometry, per-lane data
  (`NBEdge::Lane`), and a mutable list of `NBEdge::Connection` (lane-to-lane
  turning connections plus internal-junction/TLS metadata).
- `NBEdgeCont`, `NBNodeCont`, `NBDistrictCont`, `NBTrafficLightLogicCont` —
  dictionaries/containers owning all edges, nodes, districts and TLS
  definitions for one network-building run, plus batch operations (joining,
  splitting, removing, "guessing").
- `NBNetBuilder` — orchestrates the entire build pipeline in
  `NBNetBuilder::compute()` (see `docs/reverse_engineering/algorithms/network_generation.md`
  and `docs/reverse_engineering/tools/netconvert.md` for the full step list).
- `NBConnection` — a lightweight (from-edge, from-lane, to-edge, to-lane,
  tlIndex) descriptor used to track a specific connection through
  successive edge/node transformations (splitting, joining) during the build
  — distinct from `NBEdge::Connection`, which is the actual persisted
  per-edge connection record.
- `NBDistrict` — VISUM-style traffic-analysis-zone (TAZ) source/sink
  container, unrelated to the simulation's runtime edges.

`NBEdge` objects are explicitly staged through `EdgeBuildingStep` values
(`INIT` → `EDGE2EDGES` → `LANES2EDGES` → `LANES2LANES_RECHECK` /
`LANES2LANES_DONE` / `LANES2LANES_USER`), reflecting that connections are
computed in successive passes rather than all at once.

```
Source:
src/netbuild/NBNode.h (class NBNode)
src/netbuild/NBEdge.h (class NBEdge, enum class EdgeBuildingStep)
src/netbuild/NBConnection.h (class NBConnection)
src/netbuild/NBNetBuilder.h/.cpp (class NBNetBuilder)
```

## Runtime model (MS*)

Central classes, all under `src/microsim/`:

- `MSEdge` — an immutable-topology road: numerical ID, `SumoXMLEdgeFunc`
  (`NORMAL`, `INTERNAL`, `CROSSING`, `WALKINGAREA`, `CONNECTOR`), its `MSLane`
  vector, cached successor/predecessor edges per vehicle class
  (`mySuccessors`, `myPredecessors`, `AllowedLanesByTarget`), and vehicle/
  person occupancy queries.
- `MSLane` — a single lane: shape, permissions (`SVCPermissions`), the
  vehicle container(s) (`myVehicles`, `myPartialVehicles`), outgoing
  `MSLink` objects (`myLinks`), and the car-following/lane-changing state
  used every simulation step.
- `MSJunction` (and subclasses `MSRightOfWayJunction`, `MSNoLogicJunction`,
  `MSInternalJunction`) — a junction: position, shape, incoming/outgoing
  `MSEdge` lists, and (for controlled junctions) an `MSJunctionLogic`
  right-of-way matrix.
- `MSLink` — a lane-to-lane connection at runtime: the request/respond
  right-of-way protocol, TLS control, internal ("via") lane if internal-link
  simulation is enabled. See `docs/reverse_engineering/network/connections.md`
  for the data model.
- `MSEdgeControl` — owns the full `MSEdgeVector` of the loaded network and
  drives per-step vehicle movement (`planMovements`, `executeMovements`,
  `changeLanes`) via a "LaneUsage" active/inactive-lane bookkeeping scheme.
- `MSJunctionControl : public NamedObjectCont<MSJunction*>` — dictionary of
  all junctions.
- `MSNet` — the simulation-wide singleton that owns one `MSEdgeControl*
  myEdges` and one `MSJunctionControl* myJunctions`, exposed via
  `getEdgeControl()`/`getJunctionControl()`.

```
Source:
src/microsim/MSEdge.h (class MSEdge, lines 77-330ish; SumoXMLEdgeFunc via getFunction())
src/microsim/MSLane.h (class MSLane; myVehicles/myPartialVehicles L1507,1519; myLinks L1606; myPermissions L1565)
src/microsim/MSJunction.h (class MSJunction)
src/microsim/MSEdgeControl.h (class MSEdgeControl)
src/microsim/MSNet.h (myEdges/myJunctions L932-934; getEdgeControl L445; getJunctionControl L485)
```

## Loading a built network into the runtime model

1. `sumo`/`sumo-gui` parse `.net.xml` with `src/netload/NLHandler.cpp`
   (a SAX-style XML handler).
2. `NLEdgeControlBuilder` incrementally builds one `MSEdge` per `<edge>`
   element (`beginEdgeParsing`/`closeEdge`) and one `MSLane` per `<lane>`
   child (`addLane`), registering each into the global `Named` dictionaries
   via `MSEdge::dictionary(id)` / lane dictionary lookups, then finally
   returns an `MSEdgeControl` holding all edges
   (`NLEdgeControlBuilder::build()` → `new MSEdgeControl(myEdges)`).
3. `NLJunctionControlBuilder` builds one `MSJunction` (of the appropriate
   subclass, chosen by `SumoXMLNodeType`) per `<junction>` element, wiring
   in its incoming/outgoing edges and, for controlled junctions, its
   `MSJunctionLogic`/`MSLink` foe relationships.
4. `<connection>` elements are resolved into `MSLink` objects owned by the
   origin `MSLane` (`myLinks`), pointing at the destination `MSLane` and,
   if internal-lane simulation is enabled, via an internal `MSLane`.
5. `MSNet::closeBuilding(...)` receives the finished `MSEdgeControl*` and
   `MSJunctionControl*` and stores them as `myEdges`/`myJunctions`, after
   which routing, TLS logic, and vehicle emission become possible.

```
Source:
src/netload/NLHandler.h (class NLHandler)
src/netload/NLEdgeControlBuilder.h/.cpp (NLEdgeControlBuilder::closeEdge L174, buildEdge L272 "new MSEdge(...)", MSLane ctor call L98)
src/netload/NLJunctionControlBuilder.h (class NLJunctionControlBuilder)
src/microsim/MSNet.h (closeBuilding signature L202)
```

Confidence: Medium — the loading sequence above is inferred from class
responsibilities and constructor call sites; the exact call order inside
`NLBuilder::build()` was not traced statement-by-statement.

## Key invariants

These are invariants the build pipeline (`NBNetBuilder::compute`) and the
runtime loader together are responsible for establishing and preserving;
violating them produces either a netconvert warning/error or undefined
behavior at simulation time.

- **Every `MSLane` belongs to exactly one `MSEdge`.** `MSLane`'s constructor
  takes `MSEdge* const edge` and lanes are stored inside
  `MSEdge::myLanes` (`std::vector<MSLane*>`); there is no shared-lane or
  multi-edge-lane concept. (`src/microsim/MSLane.h` ctor; `src/microsim/MSEdge.h` L168-174)
- **Every `NBEdge` has exactly one `NBNode* from` and one `NBNode* to`.**
  Self-loops (`from == to`) are explicitly detected and removed early in
  `NBNetBuilder::compute` via `NBNode::removeSelfLoops`.
  (`src/netbuild/NBNetBuilder.cpp` L91)
- **Connections are only valid between an edge's outgoing lane and a lane of
  an edge that starts at the same node the first edge ends at.**
  `NBEdge::addEdge2EdgeConnection`/`addLane2LaneConnection` return `false`
  (silently reject) if `dest` does not start at `myTo`. (`src/netbuild/NBEdge.h` L895-948)
- **Lane and connection state passes through explicit staging
  (`EdgeBuildingStep`)** so that later passes (e.g. `recheckLanes()`) know
  whether connections were computed, loaded from user input, or still need
  validation. (`src/netbuild/NBEdge.h` enum `EdgeBuildingStep`)
- **Internal edges/lanes only exist when `--no-internal-links` is false.**
  `NBNode::buildInnerEdges()` materializes the internal lane/connection
  geometry; on the runtime side `MSEdge::isInternal()`
  (`myFunction == SumoXMLEdgeFunc::INTERNAL`) marks the corresponding
  edges. Without internal links, `MSLink` connections have zero length and
  no via-lane. (`src/netbuild/NBNode.h` L692-693; `src/microsim/MSEdge.h` L269-271)
- **IDs are globally unique dictionaries.** Both layers register objects in
  a `Named`/`NamedObjectCont` dictionary keyed by string ID
  (`NBEdgeCont`/`NBNodeCont` as `std::map<std::string, NBEdge*/NBNode*>`;
  `MSEdge::dictionary(id)`, `MSJunctionControl : NamedObjectCont<MSJunction*>`).
  Runtime edges additionally carry a dense `myNumericalID` used for fast
  array-indexed router state. (`src/microsim/MSEdge.h` L100,307-309,937;
  `src/microsim/MSJunctionControl.h`)

Confidence: High for structural invariants directly visible in headers;
Medium for behavioral claims about what happens when they are violated
(inferred from error-handling code paths, not exhaustively traced).
