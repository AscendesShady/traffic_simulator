# Speed, acceleration, and deceleration control

## Purpose

Combine independent speed constraints into the speed actually integrated for a
vehicle during a simulation step.

## Inputs

Previous/current speed, type maximum, lane limit, car-follow candidate,
leader/stop/link constraints, acceleration/deceleration/emergency limits,
lane-change advice, slope/friction, action step, and external influencer mode.

## Outputs

Next speed, acceleration, traveled distance, waiting-state update, signals, and
arrival/link timing.

## State

`MSVehicle::State` and previous state, drive-item vector, waiting time,
acceleration, influencer commands, stop state, and model parameters.

## Preconditions

All speeds are m/s; acceleration is m/s²; `DELTA_T`/action-step conversions are
consistent; a vehicle has a valid lane/type/model.

## Decision Process

`MSVehicle::planMove()` starts with free acceleration and repeatedly lowers the
candidate for leaders, stops, lane end, links, persons, and devices. Lane-change
logic can patch speed for safety/cooperation. `MSCFModel::finalizeSpeed()` applies
model and physical bounds plus external influence. `executeMove()` integrates
distance using Euler or ballistic behavior, crosses links/lanes, updates state
and reminders, and schedules arrival/teleport when necessary.

## Mathematical Model

Shared bounds use maximum next speed from acceleration and minimum next speed
from deceleration/emergency deceleration. Euler integration uses step speed for
distance; ballistic integration accounts for within-step speed change/stopping.
Model-specific desired/safe speed is documented in `car_following.md`.

## Constraints

Hard lane/type limit unless speed mode permits override; nonnegative committed
speed; safe stop/link arrival; acceleration/deceleration bounds; numerical
epsilon; vehicle action-step timing; optional friction/slope adjustments.

## State Transitions

`CURRENT -> PLANNED -> PATCHED/FINALIZED -> EXECUTED -> PREVIOUS=CURRENT`.

## Edge Cases

Zero step candidate, stopping inside ballistic step, emergency braking,
red signal, no leader, lane-limit change, influencer command expiration,
teleport threshold, reverse/rail motion.

## Implementation

`src/microsim/MSVehicle.cpp` (`planMove`, link processing, `executeMove`),
`src/microsim/cfmodels/MSCFModel.cpp` and concrete models,
`src/microsim/MSLane.cpp`.

## Related Classes

`MSCFModel`, `MSVehicleType`, `MSLink`, `MSAbstractLaneChangeModel`,
`MSVehicle::Influencer`.

## Related Tests

`tests/sumo/cf_model/`, `action_step_length/`, `tls/`, `junction_model/`, and
TraCI speed/speedMode tests; base formulas in `unittest/src/microsim/`.

## Behavioural Invariants

Planning precedes integration; all active hard constraints contribute; position,
speed and acceleration refer to the same completed step; link crossing cannot
bypass admission.

## Reimplementation Notes

MUST preserve integration mode and ordering, not only the chosen car-follow
formula. MAY express constraints as a composable minimum-bound pipeline.

## Confidence

High for pipeline; medium for every device/influencer special case.
