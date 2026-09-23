# Netedit architecture

## Purpose

`netedit` is SUMO's interactive authoring application. It edits four related
models: the build-time road network, additional infrastructure, demand, and
generic/mean data. It is not a visual wrapper around a running `MSNet`.

Primary implementation:

- `src/netedit/netedit_main.cpp`
- `src/netedit/GNEApplicationWindow.h`
- `src/netedit/GNELoadThread.h`
- `src/netedit/GNENet.h`
- `src/netedit/GNEViewNet.h`
- `src/netedit/GNEUndoList.h`
- `src/netedit/elements/`, `src/netedit/frames/`, `src/netedit/dialogs/`, and
  `src/netedit/changes/`

Related tests: `tests/netedit/`, especially `basic/`, `elements/`, `network/`,
`processing/`, `selection/`, and `viewport/`.

## Responsibilities

- Load existing networks/configurations or create a blank `NBNetBuilder` model.
- Present network, demand, additional, and data editing modes.
- Maintain typed editable elements and their parent/child relationships.
- Convert user gestures and dialogs into undoable `GNEChange` operations.
- Mark derived network state dirty and recompute it before operations that need
  connections, junction logic, geometry, or paths.
- Validate and serialize network, routes, additionals, data, and mean data to
  their appropriate files.

## Inputs

Netedit accepts configuration/network input plus optional route, additional,
data, mean-data, edge-type, TLS, and polygon files. `GNELoadThread::fillOptions()`
registers the actual option surface. Loading eventually builds the editable
network around `NBNetBuilder`, rather than loading microscopic `MSLane` and
`MSVehicle` objects.

## Outputs

The editor can save a simulation-ready network, plain node/edge/connection
files, joined-junction data, demand/routes, additional objects, generic data,
mean data, and GUI configuration. Each category has independent dirty/saved
state; saving the network does not imply demand or additionals were saved.

## State

| State | Owner | Meaning |
|---|---|---|
| build-time graph | `NBNetBuilder`, accessed by `GNENet` | mutable nodes, edges, connections, TLS and derived topology |
| attribute carriers | `GNENetHelper::AttributeCarriers` | typed registries for network, additional, demand and data elements |
| editor mode | `GNEViewNet` and frame helpers | active supermode, edit mode, selection and visualization options |
| undo/redo history | `GNEUndoList` | ordered changes and nested command groups |
| path caches | `GNEPathManager` instances | derived network/demand/data paths invalidated by edits |
| saving status | `GNENetHelper::SavingStatus` | dirty state per serializable category |
| spatial index | `GNENet` grid | drawable/pickable attribute carriers |

## Dependencies

Netedit depends on `netbuild` for normalized topology and computation,
`netimport`/`netwrite` for file conversion, `router` for path validation,
`utils/gui` and FOX for UI/rendering, and shared XML/options/geometry classes.
It does not depend on the microscopic movement loop.

## Consumers

Its outputs are consumed by netconvert, SUMO, routing tools, and external
workflows. Tests also drive the application through GUI events and compare
saved files/screens or normalized output.

## Execution

1. `netedit_main.cpp::main()` initializes XML, options, FOX, and
   `GNEApplicationWindow`.
2. `GNELoadThread::run()` performs background loading/new-network creation and
   returns a `GNEEvent_FileLoaded` to the GUI thread.
3. `GNEApplicationWindow` creates the view parent, menus, frames, and current
   `GNENet`.
4. `GNEViewNet` dispatches mode-specific pointer/key operations to frames and
   model methods.
5. Model mutations are represented by `GNEChange` subclasses and registered in
   `GNEUndoList`.
6. `GNENet::requireRecompute()` invalidates derived network state;
   `GNENet::computeNetwork()` invokes the netbuild computation path when needed.
7. Category-specific save methods serialize the current models.

Loading and computation are separated from UI mutation. FOX objects must be
created/updated on the correct thread; the loader communicates via the event
queue rather than directly replacing live widgets.

## Important Classes

| Class | Responsibility | Source |
|---|---|---|
| `GNEApplicationWindow` | top-level application, commands, file lifecycle | `src/netedit/GNEApplicationWindow.h` |
| `GNELoadThread` | option registration and background model loading | `src/netedit/GNELoadThread.h` |
| `GNENet` | central editable model and netbuild bridge | `src/netedit/GNENet.h` |
| `GNEViewNet` | canvas, picking, mode dispatch and view options | `src/netedit/GNEViewNet.h` |
| `GNEUndoList` | grouped undo/redo ownership and command history | `src/netedit/GNEUndoList.h` |
| `GNEAttributeCarrier` | common typed/validated attribute interface | `src/netedit/elements/GNEAttributeCarrier.h` |
| `GNEChange` | reversible mutation base | `src/netedit/changes/GNEChange.h` |
| `GNEPathManager` | computed paths and invalidation | `src/netedit/GNEPathManager.h` |

Element subclasses are divided into `network/`, `additional/`, `demand/`, and
`data/`. Frames are editing controllers for those model families; dialogs are
not the source of truth for persisted state.

## Important Functions

- `GNELoadThread::run()` and `loadNetworkOrConfig()` establish the model.
- `GNENet::createJunction()`/`createEdge()` and typed deletion methods mutate
  graph and element registries through undo operations.
- `GNENet::computeNetwork()` refreshes derived netbuild state.
- `GNENet::saveNetwork()` and category-specific save methods define persistence.
- `GNEUndoList::begin()`/`add()`/`end()` group atomic user commands;
  `undo()`/`redo()` replay their inverse/forward operations.
- `GNEViewNet::onCmdSetSupermode()` and `onCmdSetMode()` change interaction
  interpretation without changing the underlying model by themselves.

## Behaviour and invariants

- Every user-visible mutation that is meant to be reversible must have a
  balanced change object and maintain parent/child and registry consistency.
- Nested undo groups must be closed or aborted; undo/redo during an open group
  is rejected by `GNEUndoList`.
- Topology edits invalidate connections, junction logic, paths, spatial
  indexing, and saving state as applicable.
- Network, demand, additionals, and data have distinct validity and save
  lifecycles.
- Selection and rendering state must not become serialized traffic semantics
  unless a command explicitly changes a model attribute.
- Recomputed network values may differ from hand-edited derived values; option
  choices determine what is preserved or rebuilt.

## Edge Cases

- Closing or loading with unsaved categories requires category-aware handling.
- Deleting an element with children must delete, detach, or reject dependent
  objects through typed change logic; raw container removal is unsafe.
- Undo after a recomputation-sensitive operation must restore both primary
  attributes and invalidation state.
- Invalid demand paths can coexist while editing but must be surfaced or
  cleaned before valid serialization.
- Loading errors are transferred from the worker thread to the application;
  partial UI/model replacement must not occur.

## Modification Points

- Add a persisted element by updating its `GNEAttributeCarrier` subclass, tag
  property metadata, handler/build path, frame/dialog exposure, writer, change
  types, and tests.
- Add a network operation in `GNENet` and express it through `GNEChange`; do not
  mutate `NB*` and GUI registries independently.
- Change interaction in the relevant frame or `GNEViewNet`, while leaving
  serialization and computation in model/build layers.
- Changes to netbuild can alter editor recomputation and saved output even when
  no Netedit file changes; run `tests/netedit/processing/` and network suites.

## Confidence

High for startup, ownership, undo, recomputation, and persistence boundaries.
Medium for the complete behavior of every one of the hundreds of individual
element/frame/dialog subclasses; those remain covered by the source coverage
audit and focused Netedit tests rather than duplicated here.
