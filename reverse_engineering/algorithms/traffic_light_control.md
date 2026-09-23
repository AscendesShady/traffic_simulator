# Traffic-light control algorithms

## Purpose

Select a valid phase/program over time and project its state characters onto
the corresponding junction links.

## Inputs

Programs/phases, durations/min/max durations, offset, current time, detector
gaps/occupancy, conditions/assignments, link mapping, WAUT schedule, TraCI
commands, and loaded state.

## Outputs

Active program/phase, next-switch time, per-link `LinkState`, switch events and
optional detector/signal output.

## State

`MSTLLogicControl` owns variants per TLS ID and the active program.
`MSTrafficLightLogic`/subclasses own phases, current step and switch state.
`MSSimpleTrafficLightLogic` handles phase sequences; actuated/delay/SOTL/NEMA/
rail/off logics add policy-specific state.

## Preconditions

Phase state length and TL link indices agree; durations are valid; all links
referenced by a logic exist after network loading.

## Decision Process

`MSNet::simulationStep()` calls `MSTLLogicControl::check2Switch()` before vehicle
movement. Due logic invokes `trySwitch()`, which selects/extends/transitions a
phase and returns/schedules the next delay. The control applies phase characters
to mapped `MSLink`s. Fixed-time logic advances cyclically; actuated logic obeys
min/max bounds and detector/custom-condition rules; other subclasses implement
their own selection policy. WAUT may switch programs using direct, GSP, or
stretch procedures.

## Mathematical Model

Fixed timing is modular cycle time plus offset. Actuated timing is bounded by
minimum/maximum duration and computed detector gap/priority. SOTL accumulates
traffic-dependent thresholds. The family has no universal optimization
objective.

## Constraints

Phase minimum/maximum duration, legal successor phases, link-index mapping,
yellow/red transition definitions, program availability, and controller-specific
detector requirements.

## State Transitions

`PROGRAM active -> PHASE i -> extend or successor -> apply link states ->
schedule next switch`; program changes may remap phase/time-in-cycle.

## Edge Cases

Off/blinking programs, missing detectors, online states, phase mutation through
TraCI, coordinated WAUT switch, rail signals, shared TLS across junctions, and
state restore mid-phase.

## Implementation

Primary: `src/microsim/traffic_lights/MSTLLogicControl.*`,
`MSTrafficLightLogic.*`, `MSSimpleTrafficLightLogic.*`,
`MSActuatedTrafficLightLogic.*`, other `*TrafficLightLogic*`, and `MSLink`.

## Related Classes

`MSPhaseDefinition`, `MSLink`, `MSLane`, detector classes,
`NLJunctionControlBuilder`/traffic-light builders.

## Related Tests

`tests/sumo/tls/`, `tests/complex/traci/trafficlight/`, and
`unittest/src/netbuild/NBTrafficLightLogicTest.cpp` (build-time duration only).

## Behavioural Invariants

One active program per TLS ID; phase state indexes match link indexes; switch
time does not silently move backward; loaded state restores program/phase timing;
red/yellow/link priority remain distinct from junction foe checks.

## Reimplementation Notes

MUST preserve program/phase/link mapping, timing and TraCI-observable semantics.
MAY implement controllers as pure state machines. OPTIONAL initially: SOTL,
NEMA, rail and custom-expression controllers.

## Confidence

High for control flow and fixed/actuated boundaries; medium for every specialized
controller.
