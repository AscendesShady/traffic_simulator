# Demand generation and expansion

## Purpose

Create trips, flows, persons and routes from OD matrices, activity models,
random sampling or explicit definitions, then expand time-based demand during
simulation.

## Inputs

Network/TAZ graph, OD matrices, time windows, activity/population parameters,
edge probabilities, flows/repetition rules, vehicle/person types, RNG seeds.

## Outputs

Trip/flow/route/person XML and, at runtime, concrete uniquely identified traffic
objects scheduled for departure.

## State

Tool-specific models (`ODMatrix`, activitygen city/population/activity classes),
Python sampling state, routing network, and runtime `MSInsertionControl::Flow`
counters/RNG.

## Preconditions

Referenced edges/TAZs/types exist or are supplied later in a valid load order;
time intervals and probabilities are valid.

## Decision Process

- `od2trips` reads OD cells, scales/rounds vehicle counts, samples or spreads
  departure times, and emits trips/flows between districts.
- `activitygen` constructs demand from city/population/activity inputs.
- Python tools such as `tools/randomTrips.py` sample endpoints/departures and
  can invoke routing to validate routes.
- Explicit flow elements are parsed into repetition parameters; at each step
  `MSInsertionControl::determineCandidates()` expands due repetitions, applies
  scale/quota, assigns `flowID.index`, and schedules insertion.

## Mathematical Model

Demand tools use weighted/random sampling and interval allocation; runtime
probabilistic flow emits when a uniform draw is below rate times step seconds.
Deterministic flows use period/number/end-derived offsets. Rounding and RNG order
are observable.

## Constraints

Nonnegative counts/rates, valid intervals, unique generated IDs, route or TAZ
reachability, class permissions, scale semantics, and one probabilistic attempt
per eligible flow per step.

## State Transitions

`AGGREGATE DEMAND -> trip/flow definitions -> concrete LOADED vehicle/person ->
PENDING departure -> INSERTED or DISCARDED`.

## Edge Cases

Zero demand, fractional scale, probability above effective per-step range,
missing TAZ sink/source, disconnected route, duplicate flow IDs, state restore,
and demand whose insertion is delayed indefinitely.

## Implementation

`src/od/`, `src/od2trips_main.cpp`, `src/activitygen/`, `tools/randomTrips.py`,
route tools in `src/router/`, and `src/microsim/MSInsertionControl.cpp`.

## Related Classes

`ODMatrix`, `ROLoader`, `SUMOVehicleParameter`, `MSRouteHandler`,
`MSVehicleControl`.

## Related Tests

`tests/od2trips/`, `tests/activitygen/`, `tests/tools/trip/randomTrips/`,
`tests/duarouter/flows/`, and SUMO flow/insertion tests.

## Behavioural Invariants

Generated IDs are unique; totals/scaling are auditable; time units agree;
runtime flow state is checkpointable; generation and insertion remain separate.

## Reimplementation Notes

MUST preserve explicit flow expansion if consuming SUMO demand. MAY omit
activitygen/OD/random-trip authoring from a minimal engine and accept already
routed demand.

## Confidence

Medium — runtime expansion was traced directly; standalone demand tools are
documented at architecture level and need deeper per-tool work for exact parity.
