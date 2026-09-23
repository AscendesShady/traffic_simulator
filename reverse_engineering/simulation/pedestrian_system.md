# Pedestrian System

## Purpose

Simulates people (and, degenerately, containers) moving on foot across the network: sidewalks, walkingareas (junction interiors), and crossings. Provides a pluggable "pedestrian model" abstraction so the position/collision-avoidance algorithm can be swapped (simple kinematic replay, stripe-based 2D micro-model, or delegation to the external JuPedSim library) without changing the rest of the simulation (routing, stages, output).

## Responsibilities

- Advance each walking transportable's position/edge/lane every simulation step.
- Resolve interactions between pedestrians and other pedestrians, and between pedestrians and vehicles at shared spaces (walkingareas, crossings, edges without sidewalks).
- Decide when a pedestrian may cross a link controlled by traffic lights/right-of-way (junction model integration).
- Report jamming/blocking status back to `MSTransportable` and to vehicles that need to know whether a crossing is currently obstructed.
- Provide position/angle for rendering and for TraCI (`moveTo`, `moveToXY`).

## Inputs

- The `MSStageMoving` (concretely `MSStageWalking`, or `MSStageTranship` for containers) describing the edge route, depart/arrival position, configured speed, and stopping-place destination.
- Static network structure: `MSEdge`/`MSLane` (`isWalkingArea()`, `isCrossing()`, `getWidth()`, `getShape()`, `getLinkCont()`), `MSLink` (right-of-way, `opened()`), `MSJunction`.
- Vehicles present on lanes/walkingareas/crossings, queried for physical obstruction.
- Options: `pedestrian.model`, and (Striping-specific) `pedestrian.striping.*` (stripe width, dawdling, jam thresholds, oncoming reservation factor, etc. — parsed in `MSPModel_Striping::MSPModel_Striping`, `src/microsim/transportables/MSPModel_Striping.cpp:117-150`).

## Outputs

- Updated `myEdgePos` / lateral position per transportable, consumed via `MSTransportableStateAdapter::getPosition/getAngle/getEdgePos`.
- Lane/edge transitions that call back into `MSStageMoving::moveToNextEdge` (which invokes `MSTransportable::proceed` chain when the stage ends).
- Blocking queries used by vehicles/junctions: `MSPModel::blockedAtDist`, `MSPModel::nextBlocking`, `MSPModel::hasPedestrians`.
- Jam counters (`MSTransportableControl::registerJammed`), warnings, and (for Striping) collision warnings when `--check-accidents` style option is enabled (`MSGlobals::gCheck4Accidents`).

## State

- Per-model "active" registry: `MSPModel_Interacting::myActiveLanes` maps `MSLane* -> vector<MSPModel_InteractingState*>` (used by Striping and JuPedSim, both derive from `MSPModel_Interacting`); `MSPModel_NonInteracting` instead schedules a `MoveToNextEdge` event per transportable and holds no lane registry.
- Per-pedestrian state object (`MSTransportableStateAdapter` subclass) stored on the `MSStageMoving` as `myPState`: position along lane (`myEdgePos`), lateral offset (`myPosLat`), direction (`myDir`), speed, waiting time, jammed flag.
- Striping additionally caches static geometry once per network: `myWalkingAreaPaths` (precomputed shapes across walkingareas connecting every incident lane pair) and `myMinNextLengths`, both `static` on `MSPModel_Striping` (`src/microsim/transportables/MSPModel_Striping.cpp:80-82`).

## Dependencies

- `microsim/transportables/MSStageMoving`, `MSStageWalking`, `MSStageTranship` (stage layer providing route/edge/reminders).
- `microsim/MSLane`, `MSEdge`, `MSLink`, `MSJunction` (network + right-of-way).
- `microsim/MSVehicle` (obstacle geometry for shared space and for `getVehicleObstacles`).
- `MSNet::getBeginOfTimestepEvents()` for scheduling (`MovePedestrians` command for Striping/JuPedSim; `MoveToNextEdge` command for NonInteracting).
- JuPedSim model additionally depends on the external `jupedsim` C API and `geos_c` (`#include <jupedsim/jupedsim.h>`, `HAVE_JUPEDSIM` build flag).

## Consumers

- `MSTransportableControl` owns the selected model instance (`myMovementModel`) plus always keeps a `MSPModel_NonInteracting` instance around (`myNonInteractingModel`) for tranship/container stages and for "beaming" style transitions (`src/microsim/transportables/MSTransportableControl.cpp:65`).
- `MSLink`/junction right-of-way logic calls `MSPModel::blockedAtDist` / `nextBlocking` to make vehicles yield to pedestrians on crossings.
- GUI (`GUIPerson`, declared as a friend of `MSPModel_Striping::PState`) reads position/angle for rendering.
- TraCI/libsumo person control uses `moveTo`/`moveToXY` on the state adapter to implement remote control.

## Execution

Selection happens once, at `MSTransportableControl` construction (`src/microsim/transportables/MSTransportableControl.cpp:63-78`): reads `pedestrian.model` (`"striping"` default per `MSFrame.cpp:587`, though defaulted to `"nonInteracting"` for the `--gui` config in `MSFrame.cpp:975-976`) and instantiates `MSPModel_Striping`, `MSPModel_JuPedSim` (if `HAVE_JUPEDSIM`), or falls back to the always-present `MSPModel_NonInteracting`.

- **NonInteracting**: `add()` schedules a single `MoveToNextEdge` `Command` per transportable at `now + computeDuration(...)`. Each firing advances the transportable exactly one edge (kinematic interpolation between two fixed endpoints, no interaction with anyone) and reschedules for the next edge — O(1) per pedestrian per edge, not per-timestep.
- **Striping / JuPedSim (via `MSPModel_Interacting`)**: a single recurring `MovePedestrians` command is scheduled once modelling becomes active, firing every `DELTA_T` and iterating `myActiveLanes` to update every pedestrian on every lane each simulation step — O(active pedestrians) per step, with per-lane sorting and neighbor lookups (see algorithm doc).
- **JuPedSim**: delegates position updates to the external library's simulation loop (`MSPModel_JuPedSim::execute`), translating SUMO network geometry into a JuPedSim "geometry" once and syncing agent state each step.

## Important Classes

| Class | File | Role |
|---|---|---|
| `MSPModel` | `MSPModel.h` | Abstract factory-selected interface: `add`, `remove`, `blockedAtDist`, `nextBlocking`, `usingInternalLanes`, `canTraverse` (static helper to test route direction consistency). |
| `MSTransportableStateAdapter` | `MSPModel.h` | Abstract per-pedestrian state/position interface implemented by each model's `PState`. |
| `MSPModel_NonInteracting` | `MSPModel_NonInteracting.h/.cpp` | Prototype/simplest model: linear interpolation between edge endpoints, event-driven, no collision avoidance. Also used for containers/tranship. |
| `MSPModel_Interacting` | `MSPModel_Interacting.h/.cpp` | Shared base for models that track pedestrians per-lane and interact with vehicles/junctions (`myActiveLanes`, `blockedAtDist`, `nextBlocking`). |
| `MSPModel_InteractingState` | `MSPModel_Interacting.h` | Common per-pedestrian fields (`myEdgePos`, `myPosLat`, `myDir`, `mySpeed`, `myWaitingTime`, `myAmJammed`) shared by Striping/JuPedSim states. |
| `MSPModel_Striping` | `MSPModel_Striping.h/.cpp` | SUMO's main pedestrian model: divides lane width into discrete "stripes", each pedestrian picks a stripe by per-stripe utility each step. |
| `MSPModel_Striping::PState` | `MSPModel_Striping.h` | Per-pedestrian state: stripe position, next-lane info (`NextLaneInfo`), current `WalkingAreaPath`. |
| `MSPModel_Striping::Obstacle`/`Obstacles` | `MSPModel_Striping.h` | Lightweight per-stripe obstacle descriptor (position bounds, speed, type: PED/VEHICLE/END/LINKCLOSED/ARRIVALPOS) used as the model's core "what do I see ahead" data. |
| `MSPModel_JuPedSim` | `MSPModel_JuPedSim.h/.cpp` | Delegates pedestrian dynamics to the external JuPedSim C library; SUMO supplies geometry and per-step goals, reads back agent positions. |

## Important Functions

- `MSPModel_Striping::moveInDirection` (`MSPModel_Striping.cpp:846`) — top-level per-timestep driver; for each active lane, builds/transforms pedestrian coordinates (special-cased for walkingareas, which may host multiple crossing paths and vehicle "foe" translations), then calls `moveInDirectionOnLane` and `arriveAndAdvance`.
- `MSPModel_Striping::moveInDirectionOnLane` (`MSPModel_Striping.cpp:1045`) — per-lane per-pedestrian obstacle assembly: merges neighbor pedestrians, next-lane obstacles, vehicle obstacles, closed-link obstacles, and arrival-position obstacles, then calls `PState::walk`.
- `MSPModel_Striping::PState::walk` (`MSPModel_Striping.cpp:1915`) — the core decision function: computes a utility value per stripe and picks the best one (see algorithm doc `pedestrian_movement.md`).
- `MSPModel_Striping::PState::distanceTo` (`MSPModel_Striping.cpp:2459`) — signed forward distance to an obstacle along the walking direction, with special sentinel values `DIST_OVERLAP`/`DIST_BEHIND`/`DIST_FAR_AWAY`.
- `MSPModel_Striping::PState::moveToNextLane` (`MSPModel_Striping.cpp:1776`) — lane-transition logic: pops onto the precomputed `NextLaneInfo`, resolves the walkingarea path to use, and calls back into `MSStageMoving::moveToNextEdge`.
- `MSPModel_Striping::getNextLane` (`MSPModel_Striping.cpp:402`) — computes the next lane/link/direction a pedestrian will use, called once per lane entry (cached in `PState::myNLI`), not per step.
- `MSPModel_NonInteracting::PState::computeDuration` (`MSPModel_NonInteracting.cpp:119`) — computes the fixed travel time for one edge given constant max speed; this is the entire "movement model" for this class.

## Behaviour

- Pedestrian lanes ("sidewalks") are picked via `getSidewalk<MSEdge,MSLane>`; if an edge has no dedicated sidewalk lane, pedestrians walk along a `SIDEWALK_OFFSET` from the edge border (NonInteracting) or a lateral offset for lanes not allowing `SVC_PEDESTRIAN` (Striping).
- Both models are direction-agnostic in the same data structure: a pedestrian moving `FORWARD` or `BACKWARD` along a lane is expressed with a signed direction constant (`MSPModel::FORWARD/BACKWARD/UNDEFINED_DIRECTION`).
- Junction crossing behaviour (walkingareas/crossings) is only modelled by NonInteracting in a degenerate straight-line sense; Striping models it fully with per-path stripe grids and vehicle "foe" injection; JuPedSim models it through the external simulator's own continuous-space geometry.
- Right-of-way integration: Striping pedestrians check `link->opened(...)` before entering a link's stopline and can react to closed links exactly like a vehicle would (`OBSTACLE_LINKCLOSED`); this is how a pedestrian actually "waits" at a red light or yields to priority traffic.

## Edge Cases

- **No sidewalk on edge**: NonInteracting falls back to the first lane with an offset (`MSPModel_NonInteracting.cpp:170-181`); Striping does the analogous offset via `checkDepartLane`.
- **Disconnected route across a walkingarea**: if `ignore-route-errors` is set, Striping guesses a direction from junction topology instead of throwing (`MSPModel_Striping.cpp:1836-1856`); otherwise throws `ProcessError`.
- **Jamming**: Striping tracks `myWaitingTime`; once it exceeds `jamTime`/`jamTimeCrossing`/`jamTimeNarrow` (or is already jammed), the pedestrian "squeezes" through ignoring other pedestrians at `vMax * jamFactor`, and is counted via `MSTransportableControl::registerJammed()` with a one-time warning (`MSPModel_Striping.cpp:2053-2067`).
- **Stopping-place capacity**: arriving pedestrians treat a full destination `MSStoppingPlace` as an obstacle (`OBSTACLE_ARRIVALPOS`, "arrival_blocked") rather than instantly occupying it (`MSPModel_Striping.cpp:1155-1164`).
- **Vehicles crossing a walkingarea**: represented as ephemeral dummy `PStateVehicle` obstacles synthesized every step from a vehicle's actual footprint (`MSPModel_Striping::addVehicleFoe`, `MSPModel_Striping.cpp:999`).
- **Containers**: `MSPModel_Striping::add` explicitly refuses non-person transportables (returns `nullptr`); containers always use `MSPModel_NonInteracting`/tranship regardless of `pedestrian.model`.
- **Self-loop routes**: `distanceTo` special-cases an obstacle that is actually the same pedestrian (looped route) to avoid self-blocking (`MSPModel_Striping.cpp:2468`).

## Tests

- Dedicated suites exist under `tests/sumo/pedestrian_model/`, with additional
  person lifecycle cases under `tests/sumo/basic/person/` and interaction cases
  distributed across junction, output, device, and sublane suites. These paths
  were enumerated in this checkout; individual case semantics were not exhaustively
  audited.

## Modification Points

- To add a new pedestrian model: subclass `MSPModel` (or `MSPModel_Interacting` if it must interact with vehicles/junctions) and register it in `MSTransportableControl::MSTransportableControl` alongside the `pedestrian.model` option (`MSTransportableControl.cpp:66-77`) and in `MSFrame.cpp:587-588`.
- Tuning Striping's behaviour is almost entirely done through the `pedestrian.striping.*` options consumed once in its constructor — no need to touch algorithm code for parameter changes.
- The stripe-utility scoring in `PState::walk` (penalties, lookahead horizons) is the single place governing emergent crowd behaviour; all magic constants are named `static const double` members of `MSPModel_Striping` for discoverability.

## Pedestrian Model Comparison

| Model | Class | Interaction | Junction handling | Vehicle interaction | Cost | Trade-off |
|---|---|---|---|---|---|---|
| nonInteracting | `MSPModel_NonInteracting` | None — each pedestrian moves independently, no collision avoidance | Straight-line interpolation across walkingareas, no crossing logic | None; pedestrians never block or yield to vehicles | O(1) events per pedestrian per edge; cheapest by far | Fast, deterministic travel time, but no realism for crowding/crossings; used by default in GUI configs for speed and always used for containers/tranship |
| striping | `MSPModel_Striping` | Full 2D lane-as-stripes micro-model with per-stripe utility, lateral movement, oncoming avoidance | Explicit walkingarea path geometry, crossing right-of-way via `MSLink::opened`, reserved oncoming stripes | Full: vehicles injected as obstacles on shared space/walkingareas, pedestrians can block vehicle right-of-way (`blockedAtDist`) | O(active pedestrians) every `DELTA_T`, plus per-lane sort/merge — the most CPU-intensive built-in model | SUMO's default/most validated model; realistic jamming, squeezing, and vehicle-pedestrian conflict but higher CPU cost and more tunable parameters to get right |
| interacting (abstract) | `MSPModel_Interacting` | N/A — shared infrastructure only (active-lane registry, junction approach registration, blocking queries) | Provides `blockedAtDist`/`nextBlocking`/`usingInternalLanes` implementations reused by Striping and JuPedSim | Provided as shared plumbing, not itself a selectable model | N/A | Not user-selectable; exists purely to avoid duplicating lane-registry and vehicle-blocking code between Striping and JuPedSim |
| jupedsim | `MSPModel_JuPedSim` | Delegated to the external JuPedSim continuous-space/force-based simulator | Delegated: SUMO network geometry (incl. additional walkable areas) exported once as JuPedSim "geometry"; the external engine handles crossings/routing within it | Partial: SUMO still needs to translate vehicle footprints so JuPedSim can avoid them; requires `HAVE_JUPEDSIM` build with `jupedsim`/`geos_c` | Cost dominated by the external library's own simulation loop (typically social-force / collision-free speed model) | Potentially higher pedestrian-dynamics fidelity (state-of-the-art crowd models) at the cost of an external native dependency, a required build flag, and looser coupling to SUMO's own event/geometry model (`usingShortcuts()` returns true when extra walkable area is used, signalling that travel-time shortcuts don't apply) |

## Confidence

High for NonInteracting and Striping (core algorithm files, constructor options,
and the full `walk()` function were read). Medium for JuPedSim because the
external library's own algorithm is opaque here, and medium for assertion-level
test completeness despite verified pedestrian/person suite roots.
