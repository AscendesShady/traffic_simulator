# Other simulation behavior

## Scope

This page indexes runtime behavior not owned solely by vehicle motion, routing,
signals, public transport, or pedestrians.

## Microscopic versus mesoscopic execution

`MSGlobals::gUseMesoSim` selects the middle of `MSNet::simulationStep()`.
Microscopic mode executes lane-level plan/move/lane-change phases. Mesoscopic
mode delegates to `MELoop::simulate()` and represents traffic with `MESegment`
queues. Shared demand, events, outputs, TraCI, and controls surround both paths,
but state resolution and available variables differ.

Primary implementation: `src/mesosim/`, `src/microsim/MSNet.cpp`.
Tests: `tests/sumo/meso/` and TextTest `sumo.meso` variants.

See `mesoscopic_simulation.md` for segment construction, event ordering,
capacity/headway, insertion, stops, teleportation, state, and API differences.

## Events

`MSEventControl` stores time-ordered `Command` objects. `MSNet` has begin-step,
insertion, and end-step queues, each with distinct ordering guarantees.
Commands may reschedule themselves by returning a delay. Moving an event to a
different queue is a behavioral change even at the same timestamp.

Primary implementation: `src/microsim/MSEventControl.*`;
unit test `unittest/src/microsim/MSEventControlTest.cpp`.

## Devices and move reminders

`MSDevice`/`MSVehicleDevice` modules attach optional behavior/output to vehicles
(tripinfo, routing, emissions, battery, taxi, SSM, ToC, Bluetooth, etc.).
`MSMoveReminder` attaches detectors/rerouters/output accounting to lanes and is
notified as vehicles enter, move, leave, park, teleport, or arrive.

Primary implementation: `src/microsim/devices/`, `MSMoveReminder.*`,
`MSBaseVehicle::initDevices()` and reminder methods.
Tests: `tests/sumo/devices/` plus output-specific suites.

## Detectors and output

Detectors are built while loading additional files and coordinated by
`MSDetectorControl`. Some collect per passage through reminders; others inspect
lane/edge state. `MSNet::writeOutput()` orders interval updates and output
writers. Output is observable behavior and can influence memory/performance but
must not feed physics unless a controller explicitly consumes it.

Primary implementation: `src/microsim/output/`, `src/netload/NLHandler.cpp`,
`MSDetectorControl.*`, `MSNet::writeOutput()`.

## Collisions and teleports

Collision detection is staged after events, movements, lane changes,
insertions, and remote-control updates. `MSLane` detects geometric/longitudinal
overlap and applies configured action. Gridlock teleports are triggered by
waiting-time thresholds in movement logic and continued through
`MSVehicleTransfer`.

Primary implementation: `MSLane::detectCollisions()`,
`handleCollisionBetween()`, `MSVehicleTransfer.*`, and collision options in
`MSFrame`/`MSGlobals`.

## State save/load

`MSStateHandler` serializes/restores simulation-wide state. Individual classes
implement `saveState`, `loadState`, and `clearState` for lanes, vehicles,
insertion, transfers, traffic lights, RNGs, devices, and controls. A valid
checkpoint requires compatible network/static definitions and synchronized
time offsets.

Primary implementation: `src/microsim/MSStateHandler.*` and state methods on
runtime classes. Tests: `tests/complex/state/`.

## Randomness

`RandHelper` supplies global and named RNG streams; lane/flow/vehicle parsing
may own separate streams. Saved states include RNG state for reproducible
continuation. Changing which stream is used or the number/order of draws is
observable.

## Global configuration

`MSFrame::setMSGlobals()` copies validated options into static `MSGlobals`
members used in hot paths. These include step length/mode, internal lanes,
collision and teleport behavior, lateral resolution, route checks, and other
global policy. New code should prefer explicit ownership where feasible, but a
SUMO-compatible change must account for every static reader.

## Confidence

High for the subsystem boundaries; medium for individual device/output
semantics, which require their dedicated source and tests.
