# Rendering pipeline

## Purpose

Rendering converts the current GUI/runtime object registry and visualization
settings into 2-D OpenGL frames, optional snapshots/video, and optional 3-D
views. It is read-mostly relative to simulation state.

Primary implementation:

- `src/gui/GUIViewTraffic.cpp`
- `src/utils/gui/windows/GUISUMOAbstractView.*`
- `src/utils/gui/globjects/GUIGlObject*`
- `src/guisim/GUIEdge.cpp`, `GUILane.cpp`, `GUIVehicle.cpp`, `GUIPerson.cpp`
- `src/osgview/` (optional 3-D)

Related tests: visualization cases under `tests/sumo/gui/visualization/` and
snapshot/replay cases under `tests/complex/sumo-gui/`.

## Frame flow

1. The abstract view establishes viewport, projection, zoom, and draw mode.
2. `GUIViewTraffic::doPaintGL()` obtains objects intersecting the visible
   boundary from spatial registries.
3. Each `GUIGlObject` subtype implements `drawGL()` using
   `GUIVisualizationSettings`.
4. `GUIEdge::drawGL()` delegates lane rendering; `GUILane::drawGL()` draws lane
   geometry, markings, connections, vehicles, persons, and overlays as enabled.
5. Picking uses object IDs/priorities to resolve objects under the cursor.
6. `saveFrame()`/snapshot hooks capture rendered pixels; optional encoders build
   video.

Source: `src/gui/GUIViewTraffic.cpp:349` (`doPaintGL`),
`src/guisim/GUIEdge.cpp:275`, `src/guisim/GUILane.cpp:546`.

## Data inputs

Rendering reads geometry from lanes/edges/junctions/shapes, dynamic positions
from traffic objects, TL/link states, detector values, selected-object state,
and configured color/scale schemes. `GUINet::updateColor()` refreshes cached
colors for current visualization settings.

The renderer can display loaded edge-data and mean-data attributes through
`GUINet::loadEdgeData()` and related getters. These overlays are visualization
data, not necessarily the live routing weights.

## Concurrency contract

The run thread can update traffic while the GUI paints. GUI subclasses expose
secure vehicle access and `GUINet::lock()`/`unlock()` protects shared structures.
A replacement renderer should consume immutable snapshots or equivalent locks;
iterating mutable lane containers concurrently is unsafe.

## Edge cases

- Mesoscopic simulation draws vehicles from segments rather than microscopic
  lane containers.
- Secondary shapes, bidirectional lanes, internal lanes, and exaggerated widths
  alter geometry selection.
- Headless `sumo` does not instantiate GUI subclasses.
- OSG/FFMPEG/GL2PS paths are conditional on build features.
- Rendering order and picking priority differ: visual overlap does not define
  traffic priority.

## Modification points

Colors/scales belong in visualization settings and GUI object value methods;
new visual objects implement/register `GUIGlObject`; camera/input belongs in
view classes. Never alter `MSVehicle` physics just to change appearance.

## Confidence

High for the 2-D pipeline; medium for optional 3-D/video backends.
