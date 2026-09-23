# Devices, detectors, and outputs

Battery, station-finder, electric-hybrid, charging-station, overhead-wire, and
traction-circuit behavior is mapped in `rail_and_electric_systems.md`; this
page owns their common device/reminder/output lifecycle.

## Purpose

This subsystem turns movement and lifecycle events into optional behavior,
measurements, and serialized observations. Its three extension mechanisms are
related but distinct:

- devices are attached to vehicles or transportables;
- move reminders observe traffic objects crossing or occupying lanes;
- detector/output controllers aggregate and flush interval or step data.

Primary implementation:

- `src/microsim/devices/MSDevice.h`
- `src/microsim/devices/MSVehicleDevice.h`
- `src/microsim/MSMoveReminder.h`
- `src/microsim/output/MSDetectorControl.h`
- `src/microsim/output/MSMeanData.h`
- `src/microsim/MSNet.cpp`
- construction in `src/microsim/MSFrame.cpp` and `src/netload/NLDetectorBuilder.cpp`

## Responsibilities

- Select and instantiate optional per-object devices from options, type
  parameters, explicit IDs, and assignment probabilities.
- Notify interested components when an object enters, moves on, idles on,
  parks on, reroutes over, or leaves a lane.
- Collect point, area, route-probe, edge/lane mean, emission, queue, and other
  measurements over defined intervals.
- Serialize step snapshots and lifecycle summaries.
- Preserve device/detector interval state across checkpoint save/load where
  implemented.
- Supply measurements to feedback controllers only through an explicit
  dependency, such as actuated TLS detectors or calibrators.

## Inputs

Inputs include global output/device options, vehicle/type parameters,
additional XML detector definitions, network references, movement callbacks,
simulation time, and final lifecycle reason. Emissions and energy devices also
consume vehicle type, speed, acceleration, slope, and model-specific energy
parameters.

## Outputs

Output families include tripinfo and routes, summary/statistics, FCD and full
state, emissions, battery/electric-hybrid/charging, detector intervals,
edge/lane mean data, queue, stop, collision, link/rail, and optional
Amitran/VTK forms. Availability and schema differ by configured writer.

## State

| State | Owner | Lifetime / update |
|---|---|---|
| attached device vector | `MSBaseVehicle` / transportable | constructed with object; notified through lifecycle; destroyed with owner |
| device selection RNG and explicit IDs | `MSDevice` static state | configuration/run lifetime; affects equipment assignment |
| active reminder offsets | traffic object | lane-relative tracking while reminder remains interested |
| detector registry and intervals | `MSDetectorControl` | network lifetime; updated/flushed at configured boundaries |
| accumulated samples | detector/`MSMeanData` values | current interval; reset or rolled after write |
| output streams | `OutputDevice` registry | opened from options, written during steps/close, closed at shutdown |

## Dependencies

Devices depend on the vehicle/transportable lifecycle and may depend on
routing, stops, taxi dispatch, emissions, electric infrastructure, or TraCI.
Detectors depend on lanes/edges/reminder callbacks and are created by network
load builders. Outputs depend on common I/O/XML infrastructure. `MSNet` owns
the ordering that makes these observations temporally meaningful.

## Consumers

Files are consumed by users, analysis tools, regression tests, and subsequent
routing/assignment runs. Live detector values may be consumed by signal
controllers, calibrators, and APIs. Device state may affect behavior directly
(rerouting, GLOSA, taxi, station finding, blue-light behavior, takeover) or be
observational only (FCD, emissions accounting, route/trip output).

## Execution

1. `MSDevice::insertOptions()` registers device families.
2. Vehicle construction calls `MSDevice::buildVehicleDevices()` in a fixed
   order. This order matters where one device is passed to another, such as a
   station finder supplied to the battery device.
3. Loader-built detectors register with `MSDetectorControl`; lane-associated
   observers are installed as move reminders.
4. Movement invokes reminder/device callbacks at enter, move, leave, idle,
   parking, reroute, and stop boundaries.
5. Near the end of a normal step, `MSNet::writeOutput()` updates detectors and
   dispatches configured step writers.
6. `MSDetectorControl::writeOutput()` closes completed aggregation intervals.
7. During final removal/close, devices generate terminal output and remaining
   detector intervals/streams are flushed.

The `onlyMove` path in `MSNet::simulationStep()` defers `postMoveStep()`, so
normal output and clock advancement occur only when the split step is
completed.

## Important Classes

| Class/family | Role | Source |
|---|---|---|
| `MSDevice` | option registration, equipment selection, common state hooks | `src/microsim/devices/MSDevice.h` |
| `MSVehicleDevice` | vehicle-attached callback contract | `src/microsim/devices/MSVehicleDevice.h` |
| `MSMoveReminder` | lane passage/lifecycle observer contract | `src/microsim/MSMoveReminder.h` |
| `MSDetectorControl` | detector registry, update and interval-write coordination | `src/microsim/output/MSDetectorControl.h` |
| `MSDetectorFileOutput` | detector serialization interface | `src/microsim/output/MSDetectorFileOutput.h` |
| `MSMeanData` | interval aggregation over edge/lane value trackers | `src/microsim/output/MSMeanData.h` |
| `MSDevice_Routing` | periodic/event rerouting | `src/microsim/devices/MSDevice_Routing.h` |
| `MSDevice_Tripinfo` | terminal trip/lifecycle reporting | `src/microsim/devices/MSDevice_Tripinfo.h` |
| `MSDevice_Battery` | battery/charging energy state | `src/microsim/devices/MSDevice_Battery.h` |
| `MSDevice_Taxi` and `MSDispatch` | reservation/fleet dispatch integration | `src/microsim/devices/MSDevice_Taxi.h` |

Other concrete devices include emissions, electric hybrid, SSM, takeover,
driver state, Bluetooth, blue-light, GLOSA, friction, FCD/replay, station
finder, transportable, and vehicle-route output implementations under
`src/microsim/devices/`.

## Important Functions

- `MSDevice::buildVehicleDevices()` is the centralized vehicle-device factory.
- `MSMoveReminder::notifyEnter()`/`notifyMove()`/`notifyLeave()` define passage
  observation semantics.
- `MSDetectorControl::add()` registers detector/interval ownership.
- `MSDetectorControl::updateDetectors()` performs time-based updates.
- `MSDetectorControl::writeOutput()` flushes due intervals.
- `MSNet::writeOutput()` defines cross-writer step ordering.
- `MSDevice::generateOutput()` and device overrides emit terminal records.

## Behavioural contracts

- A reminder returning false from applicable callbacks must be detached
  according to the caller contract; stale reminders are unsafe.
- Enter/move/leave reasons distinguish departure, lane transition, parking,
  teleport, arrival, vaporization, and other lifecycle paths. Collapsing them
  changes output semantics.
- Interval boundaries use simulation time and must not double-count samples
  when a vehicle changes lanes or crosses an edge.
- Terminal device output must be generated exactly once despite deferred
  removal and multiple removal causes.
- Observational devices must not alter vehicle physics. Behavioral devices may
  do so only at their declared integration point.
- Enabling or disabling output should not change traffic evolution except when
  the configured component is explicitly a feedback controller or consumes a
  shared random stream.
- Output ordering, IDs, units, defaults, unfinished-object policy, and schema
  are observable compatibility behavior.

## Edge Cases

- Vehicles that never depart may still require unfinished or discard handling.
- Teleport, parking, collision, remote removal, and arrival take different
  reminder/device paths but converge on lifecycle cleanup.
- Zero-length or partially completed aggregation intervals require defined
  closing behavior.
- A detector reference to a missing lane/edge is a load-time validation issue.
- State restore must not duplicate device construction or interval samples.
- A device selected by probability must remain reproducible under saved RNG
  state and documented seeding.

## Tests

- Device families: `tests/sumo/devices/` with suites for battery, blue-light,
  Bluetooth, driver state, electric hybrid, emissions, FCD replay, friction,
  GLOSA, rerouting, SSM, station finding, taxi, and transportables.
- Output families: `tests/sumo/output/`, including E1/E2/E3 detectors,
  tripinfo, vehicle routes, FCD, emissions, mean data, queue, summary, stops,
  collisions, route probes, and statistics.
- State persistence: relevant cases under `tests/complex/state/`.
- API observation/control: corresponding domains under `tests/complex/traci/`.

## Modification Points

- New device: register options and construction in `MSDevice`, implement the
  correct callback/state/output hooks, integrate API/schema support if exposed,
  and add lifecycle tests including non-departed and abnormal removal cases.
- New detector: build it through `NLDetectorBuilder`, register it with
  `MSDetectorControl`, define interval reset/write semantics, and test lane
  transitions plus close behavior.
- New output: register its option in `MSFrame`, write it at the correct
  `MSNet::writeOutput()` or lifecycle point, and define unfinished/state rules.
- Do not derive core physics from a new observational output path; place
  behavioral feedback behind an explicit controller contract.

## Confidence

High for construction, callback, aggregation, and output ordering boundaries.
Medium for every individual device's domain algorithm; the catalog is mapped,
but each optional model would require its own focused reverse-engineering pass
for exact reimplementation.
