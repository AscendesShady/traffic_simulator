# sumolib — SUMO's Python Object Model and XML Toolkit

`tools/sumolib` (Python package, `SUMO_HOME/tools/sumolib`) is the shared library that essentially every Python tool in `tools/` imports. It has no C++ counterpart; it re-parses the same XML formats the C++ core reads/writes (`.net.xml`, `.rou.xml`, `.add.xml`, detector output, etc.) into Python objects, and provides generic XML-parsing helpers used across the tool ecosystem.

## Package layout (`tools/sumolib/`)

| Module/dir | Role |
|---|---|
| `__init__.py` | Package entry point; re-exports submodules; `checkBinary()` (locates a SUMO executable via `SUMO_HOME`/`<NAME>_BINARY` env vars), `call()`/`saveConfiguration()` (invoke a SUMO binary from Python using an `argparse`-like options object) |
| `net/` | Object model for a parsed `.net.xml` network: `Net`, `Edge`, `Lane`, `Node`, `Connection`, `Roundabout`, `TLS`/`Phase`/`TLSProgram` |
| `xml/` | Generic, format-agnostic SAX/ElementTree-based XML parsing helpers (`parsing.py`), XSD attribute lookup (`xsd.py`) |
| `route.py` | Helpers for reading/writing `.rou.xml` route files |
| `vehicletype.py` | Vehicle type parsing/generation helpers |
| `shapes.py` / `shapes/` | POI/polygon (`.poi.xml`/`.poly.xml`) helpers |
| `output/` | Helpers for reading simulation output files (tripinfo, fcd, etc.) |
| `sensors.py`, `scenario/` | Detector and scenario-level helpers |
| `geomhelper.py` | 2D geometry utilities (distance, projection onto polyline, angle) shared by net and route tooling |
| `color.py`, `statistics.py`, `miscutils.py`, `options.py`, `translation.py`, `version.py`, `fpdiff.py` | Cross-cutting utilities: RGBA color parsing, running statistics, misc helpers (`openz` for transparent gzip open), `ArgumentParser`/`pullOptions` (mirrors the C++ `OptionsCont` sub-topic/option-file conventions in Python), translation strings, version string, floating point diff |
| `net/generator/` | Procedural network generation helpers (grids, etc.) used by test/example generators |

## `sumolib.net` — the network object model

Source: `tools/sumolib/net/__init__.py` (1148 lines) plus `edge.py`, `lane.py`, `node.py`, `connection.py`, `roundabout.py`, `netshiftadaptor.py`.

`Net` (defined at `tools/sumolib/net/__init__.py:214`) is the in-memory mirror of a SUMO network, populated by the SAX-based `NetReader` (`net/__init__.py:852`, a `xml.sax.handler.ContentHandler`) when a `.net.xml` file is read via `sumolib.net.readNet(filename)` (`net/__init__.py:1110`). Internally it keeps:

- `_id2node` / `_id2edge` / `_id2tls` dicts plus parallel `_nodes`/`_edges`/`_tlss` lists (insertion order preserved for the lists, O(1) lookup via the dicts).
- `_rtreeEdges` / `_rtreeLanes` — lazily built R-trees (`_initRTree`, `net/__init__.py:363`) used by `getNeighboringEdges`/`getNeighboringLanes` for spatial nearest-neighbor queries (e.g., snapping GPS points to lanes).
- `_roundabouts`, `_edgeTypes` (default edge type registry), `hasInternal`/`hasWalkingArea` flags (set when the network was exported with internal-lane/pedestrian info).
- Coordinate system state: `_location` (netOffset/convBoundary/origBoundary/projParameter as written by netconvert), with `convertLonLat2XY`/`convertXY2LonLat`/`hasGeoProj`/`getGeoProj` (`net/__init__.py:566-558`) wrapping `pyproj` when available, so tools can round-trip between the network's local x/y and geographic lon/lat exactly as netconvert projected them.

Key object classes:

- **`Edge`** (`tools/sumolib/net/edge.py`, 304 lines) — id, from/to `Node`, function (normal/internal/connector/walkingarea), priority, list of `Lane`s, incoming/outgoing `Connection`s, allowed/disallowed vehicle classes, shape.
- **`Lane`** (`lane.py`, 343 lines) — per-lane speed, length, width, shape, allow/disallow class lists, outgoing `Connection`s.
- **`Node`** (`node.py`, 231 lines) — id, type (priority/traffic_light/dead_end/...), coordinate, incoming/internal lanes, request/foe matrices for junction logic.
- **`Connection`** (`connection.py`) — from-edge/lane to to-edge/lane link, direction, linked TLS id/link index, right-of-way state character, optional via-lane id (internal lane) for junction-internal geometry.
- **`Roundabout`** (`roundabout.py`) — node/edge ring membership.
- **`TLS`/`Phase`/`TLSProgram`** (top of `net/__init__.py:65-206`) — traffic-light program object model: connections indexed by link number, list of `Phase` (duration + state string, optionally min/max duration and "next" phase indices for actuated/NEMA logic), multiple named `TLSProgram`s per TLS id.

Routing support built on this model (`Net` methods, `net/__init__.py:589-855`):

- `getShortestPath` / `getFastestPath` / `getOptimalPath` — Dijkstra-style shortest path by length or by travel time (`fastest=True` uses `edge.getSpeed()`), with `vClass` filtering, U-turn `reversalPenalty`, and an internal `_shortestPathCache` (`initRoutingCache`, an `lru_cache`-backed memo) to reuse a Dijkstra heap across repeated queries from the same origin.
- `getReachable` — BFS/DFS reachable-edge set for a given vehicle class, used to validate route/TAZ connectivity.
- `getUpstreamEdges` — walks incoming edges up to a distance, optionally stopping at TLS-controlled or turnaround edges (used by detector/rerouter placement tools).

This object model is a **read/write-light mirror**, not a re-implementation of netconvert's algorithms: it does not recompute lane geometry, junction logic, or right-of-way — it only exposes what netconvert already computed and wrote into the XML (shape strings, request/foe bitsets, connection `state` characters). Tools that need to *modify* a network (e.g. `tools/net/*.py`) mutate this object graph and rely on `Net`/`Edge`/`Node` `toXML()`-style serialization or call back into netconvert/netedit for anything requiring re-derivation of junction logic.

## `sumolib.xml` — generic XML parsing helpers

Source: `tools/sumolib/xml/parsing.py` (769 lines), `xsd.py` (143 lines).

This is a schema-driven convenience layer used by most `tools/*.py` scripts to read arbitrary SUMO XML files (routes, additional files, output files) without hand-writing a SAX handler each time:

- `parse(xmlfile, element_names, ...)` (`parsing.py:401`) — streaming parse (via `xml.etree.cElementTree` iterparse, or `lxml` if available) that yields lightweight generated "compound objects" (namedtuple-like, built by `compound_object()`, `parsing.py:200`) for each matching element, with attribute name/type conversion (`DEFAULT_ATTR_CONVERSIONS`, e.g. `speed`→float, `shape`→list of float pairs) and optional XSD-based attribute discovery (`_attrs_from_xsd_url`, `xsd.py`) when an element's full attribute set isn't given explicitly.
- `parse_fast` / `parse_fast_nested` / `parse_fast_structured` (`parsing.py:570-721`) — a faster, regex/line-based parser for flat or two-level-nested XML (e.g. `<vehicle><route .../></vehicle>`) used by performance-sensitive tools (route file post-processing) that don't need a full XML parser.
- `compound_object` / `AttrFinder` / `NestingHandler` — build ad hoc, attribute-typed Python objects mirroring arbitrary SUMO element schemas at parse time, so the same generic parser works for tripinfo output, `.rou.xml`, `.add.xml`, `.edg.xml`, etc.
- `create_document`, `sum`, `average`, `quoteattr`, `contextualRename` — small XML authoring/aggregation helpers for tools that also *write* SUMO XML.

## Practical role in the ecosystem

`sumolib` is the reason most of `tools/*.py` can be short, single-purpose scripts: instead of re-implementing an XML network reader, each tool does `net = sumolib.net.readNet(args.net)` and operates on `Edge`/`Lane`/`Node`/`Connection` objects with the same IDs and topology the C++ simulation uses. `sumolib.xml.parse` plays the analogous role for the many non-network XML formats (routes, additional infrastructure, outputs). Tools that need a live-running simulation instead of static files use TraCI/libsumo (documented separately) — `sumolib` itself does no socket or shared-memory communication.
