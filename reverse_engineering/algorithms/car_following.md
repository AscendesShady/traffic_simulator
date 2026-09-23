# Car-following algorithm family

## Purpose

Compute a vehicle's longitudinal safe/desirable speed from its own state,
leader/gap, physical/type limits, lane/link constraints, and selected model.

## Inputs

Current/previous speed, gap, leader speed/deceleration, vehicle-type parameters,
lane speed, action-step timing, stop/link distances, and external influencer.

## Outputs

A next-step speed bound and supporting safe-gap/arrival-time calculations.

## State

Common parameters live in the vehicle type's `MSCFModel`; model-specific state
may live in per-vehicle variables. The vehicle retains current/previous speed,
acceleration, waiting time, and planned drive items.

## Preconditions

Distances use SUMO lane coordinates/metres, speed is m/s, time conversions use
`SUMOTime`, and the selected model is compatible with integration mode.

## Decision Process

`MSVehicle::planMove()` finds physical and virtual leaders, stops, lane speed,
and junction constraints. It repeatedly tightens a speed candidate using
model methods such as `followSpeed`, `stopSpeed`, and `freeSpeed`.
`MSCFModel::finalizeSpeed()` combines the candidate with acceleration,
deceleration/emergency bounds, dawdling/model effects, stop processing,
influencer constraints, and lane-change patching. `executeMove()` integrates
the accepted speed.

## Mathematical Model

There is no single formula. `MSCFModel_Krauss`/`KraussOrig1` implement the
default stochastic safe-speed family; `MSCFModel_IDM` uses the IDM acceleration
law; ACC/CACC, EIDM, Wiedemann, W99, Kerner, Wiedemann, rail, and other classes
implement different equations. Shared helpers include discrete/ballistic
`brakeGap`, `freeSpeed`, minimum/maximum next speed, and arrival-time estimates.
See `../simulation/car_following.md` for formula-level notes.

## Constraints

Vehicle maximum speed, lane limit, acceleration/deceleration/emergency
deceleration, nonnegative speed (except intermediate ballistic stop encoding),
safe gap, stop line, and link/junction permissions constrain the result.

## State Transitions

`current state -> planned speed/drive items -> lane-change/junction patches ->
executed position/speed -> previous-state update`.

## Edge Cases

No leader, zero/negative gap, leader emergency braking, sub-second/action-step
updates, ballistic stopping within a step, opposite driving, external speed
control, and numerical epsilon around zero.

## Implementation

Primary implementation: `src/microsim/cfmodels/MSCFModel.*`, concrete
`MSCFModel_*.{h,cpp}`, and `src/microsim/MSVehicle.cpp`.

## Related Classes

`MSVehicle`, `MSVehicleType`, `MSLane`, `MSLink`, `MSAbstractLaneChangeModel`.

## Related Tests

`tests/sumo/cf_model/`, `tests/sumo/action_step_length/`,
`unittest/src/microsim/MSCFModelTest.cpp`,
`unittest/src/microsim/MSCFModel_IDMTest.cpp`.

## Behavioural Invariants

The committed speed respects applicable hard bounds; gap units and time
integration agree; a model is selected per type; external overrides are applied
through the documented speed-mode/influencer contract.

## Reimplementation Notes

MUST preserve model selection, units, step integration, leader/stop/junction
constraints, and observable option semantics. MAY redesign class hierarchy and
caching. Implement one model completely before claiming family compatibility.

## Confidence

High for the pipeline; model-specific mathematics range from high (tested base/
IDM paths) to medium where only functional fixtures cover them.
