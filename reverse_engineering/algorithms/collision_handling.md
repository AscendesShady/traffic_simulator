# Collision detection and handling

## Purpose

Detect physical overlap that escaped preventive models, classify/report it,
and apply configured vehicle/intermodal consequences.

## Inputs

Ordered lane vehicles, partial/shadow occupancy, positions, lengths, widths,
minimum gap factor, bounding polygons, junction foe lanes, pedestrians,
simulation stage, and collision action/stop time.

## Outputs

Collision records/warnings, optional temporary collision stops, and sets of
vehicles to teleport or remove.

## State

Per-lane collision options/check flags; per-vehicle collision stop; `MSNet`
collision map and stage counters; pending removal and transfer containers.

## Preconditions

Lane containers and partial occupancy are current for the stage being checked.

## Decision Process

`MSNet` invokes collision detection after several mutating phases.
`MSLane::detectCollisions()` compares longitudinal neighbors and, when enabled,
junction foe-lane bounding geometry. `detectCollisionBetween()` computes gap,
opposite direction and lateral overlap, rejecting self/passed/non-overlap cases.
`handleCollisionBetween()` classifies rear/frontal/side/junction, records it, may
create a collision stop, then applies `none`, `warn`, `teleport`, or `remove`.
Intermodal handling follows a parallel policy.

## Mathematical Model

Longitudinal collision occurs when effective gap is below negative epsilon,
with configurable fraction of `minGap`; sublane mode also requires negative
lateral gap. Junction collision uses bounding boxes plus polygon overlap.
Collision-stop speed adjustment depends coarsely on relative angle (<45,
45–135, >=135 degrees).

## Constraints

Action option, remote-control exemption for teleport action, ignore-collision
flags, no self collision, stage de-duplication, and optional junction checks.

## State Transitions

Conceptually: NORMAL -> COLLIDED -> STOPPED -> configured action, or directly
to teleporting/deferred removal; `warn` may leave vehicles in place. These are
documentation-level lifecycle labels, not one implementation enum.

## Edge Cases

Opposite vehicles, simultaneous lane changes, different internal-lane lengths,
partial vehicles, pedestrians in crossings/walking areas, remote affected
vehicles, repeated detection in multiple stages.

## Implementation

`src/microsim/MSLane.cpp:1696-2305`, `MSNet.cpp` collision staging,
`MSVehicleTransfer.cpp`, options in `MSFrame.cpp:410-429`.

## Related Classes

`MSVehicle`, `MSPerson`, `MSLink`, `MSVehicleControl`, `MSMoveReminder`.

## Related Tests

Collision scenarios distributed under `tests/sumo/cf_model/`, `lc_model/`,
`junction_model/`, `pedestrian_model/`, and bug-ticket suites.

## Behavioural Invariants

Every collision is attributed to a stage/type; action is applied at most once
per record window; lane/dictionary state remains consistent after removal or
teleport.

## Reimplementation Notes

MUST preserve option semantics and outputs if claiming compatibility. Collision
handling is a recovery layer, not a substitute for car-follow/junction safety.

## Confidence

High for vehicle overlap/action; medium for all geometric junction/intermodal
branches.
