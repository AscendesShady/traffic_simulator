# Lane-changing algorithms

## Purpose

Choose whether and how a vehicle changes lateral position/lane while balancing
route necessity, strategic/cooperative/speed-gain/keep-right motives and safety.

## Inputs

Current/best lanes, route continuation, leaders/followers on neighboring lanes,
gaps and secure gaps, lane permissions, speed, impatience, vehicle/type
parameters, sublane geometry, external lane-change commands.

## Outputs

Lane-change desire/state flags, target lane/lateral offset, speed advice and
neighbor-cooperation messages; eventually updated lane membership/reservations.

## State

Each vehicle owns an `MSAbstractLaneChangeModel` instance. It stores state flags,
last maneuver times, shadow/target lanes, lateral progress, and model-specific
parameters. Each edge owns an `MSLaneChanger` or `MSLaneChangerSublane`.

## Preconditions

Candidate lanes belong to a compatible edge/lateral neighborhood, permit the
vehicle, and offer route continuation where strategically required.

## Decision Process

1. `MSVehicle::updateBestLanes()` evaluates downstream route suitability.
2. The edge's lane changer scans vehicles in positional order.
3. The model (`MSLCM_LC2013` by default or `MSLCM_SL2015` for sublane mode)
   evaluates left/right/continuous alternatives and produces bit flags.
4. Leader/follower safety and blocked-gap checks can prevent the maneuver or
   request cooperation/speed adaptation.
5. The orchestrator commits an instantaneous lane swap or advances a continuous
   lateral maneuver, maintaining shadow occupancy/reservations.

## Mathematical Model

Safety is expressed through leader/follower gaps and their car-follow model's
secure-gap/speed constraints. Desirability combines weighted motives and
thresholds; SL2015 additionally operates on lateral gaps/alignment and bounded
lateral speed. The exact expressions are model-specific; see
`../simulation/lane_changing.md` and concrete model source.

## Constraints

Vehicle width, lane width, lateral resolution, max lateral speed, permissions,
solid/change restrictions, route reachability, opposite/bidirectional traffic,
and simultaneous maneuvers.

## State Transitions

`KEEP -> WANTS/URGENT/BLOCKED -> CHANGING (continuous mode) -> COMPLETE`, with
shadow lane and reservations created/cleared around the maneuver.

## Edge Cases

Urgent route changes, insufficient follower gap, cooperative braking,
opposite-direction overtaking, multiple-lane changes, internal edges, lane end,
remote requests, and two simultaneous continuous changes.

## Implementation

Primary: `src/microsim/lcmodels/MSAbstractLaneChangeModel.*`,
`MSLCM_LC2013.*`, `MSLCM_SL2015.*`, `src/microsim/MSLaneChanger*`,
`MSEdge::changeLanes()`/`MSLane::changeLanes()`.

## Related Classes

`MSVehicle`, `MSLane`, `MSEdge`, `MSCFModel`, `MSLeaderInfo`.

## Related Tests

`tests/sumo/lc_model/`, `tests/sumo/sublane_model/`,
`tests/sumo/opposite_direction_driving/`; no dedicated gtest found.

## Behavioural Invariants

Lane containers remain ordered and contain the vehicle consistently; shadow/
reservation state is cleared on completion/removal; accepted maneuvers remain
route- and permission-compatible; speed advice is included before movement.

## Reimplementation Notes

MUST preserve motive priority, safety interaction, external mode semantics, and
occupancy during continuous changes. MAY replace flag encoding and scan data
structures. Treat LC2013 and SL2015 as separate compatibility targets.

## Confidence

High for orchestration; medium for the full heuristic surface of SL2015.
