# GUI interaction model

## Purpose

The interaction layer maps FOX events, menus, hotkeys, dialogs, and TraCI GUI
commands into view-only operations or synchronized changes to the simulation.

Primary implementation:

- `src/gui/GUIApplicationWindow.cpp`
- `src/gui/GUIViewTraffic.cpp`
- `src/gui/GUIRunThread.cpp`
- `src/utils/gui/windows/GUIGlChildWindow.*`
- popup/parameter methods in `src/guisim/GUI*.cpp`
- `src/libsumo/GUI.cpp` and TraCI GUI handlers

Related tests: `tests/complex/sumo-gui/pyautogui_start_stop/`, GUI settings and
visualization tests, `tests/complex/traci/gui/`.

## Interaction categories

| Category | Examples | State affected |
|---|---|---|
| simulation lifecycle | load, reload, run, pause, step, quit | `GUINet`, load/run threads |
| navigation | pan, zoom, center, track | view/camera only |
| inspection | popup menus, parameter windows, select | GUI selection/tracker state |
| visualization | schemes, overlays, decals, internal lanes | visualization settings |
| controlled mutation | close lane/edge, add rerouter, vehicle/TLS commands | authoritative simulation state |
| capture | snapshot, video, breakpoint | view plus run scheduling |

## Run-state machine

`GUIRunThread` distinguishes network availability and whether the simulation is
startable, stoppable, or stepable. `resume()`, `singleStep()`, and `stop()` set
run intent; `run()`/`tryStep()`/`makeStep()` execute paced steps and post events.
`waitForSnapshots()` coordinates time-addressed captures so simulation time does
not outrun required rendering.

Loading is asynchronous. `GUILoadThread::run()` reports messages and completion
to the application window. Window shutdown must stop/join workers before
destroying their network and UI consumers.

## Object interaction

Rendered objects provide popup menus, parameter windows, centering boundaries,
and selection IDs. `GUIViewTraffic::centerTo()` navigates to an object;
double-click and gaming handlers resolve the object/lane under the cursor.
Tracking retains an object ID and updates the camera as that object moves.

## Safety rules for a replacement UI

- Send commands through a simulation-control boundary; do not mutate containers
  during paint.
- Distinguish current simulation time from wall-clock playback delay.
- Treat load completion, step completion, close, and error as asynchronous
  events.
- Handle an object disappearing between selection and command execution.
- Preserve pause/single-step determinism independent of render rate.

## Confidence

High for lifecycle and view commands; medium for platform-specific FOX event
ordering.
