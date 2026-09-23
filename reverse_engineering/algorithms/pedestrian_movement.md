# Pedestrian movement algorithms

## Purpose

Advance walking stages through sidewalks, crossings, walking areas and access
links while interacting with vehicles and person-plan transitions.

## Inputs

Walking route/stage, pedestrian type/speed, lane geometry and permissions,
crossings/signals, other pedestrians, vehicles, and selected pedestrian model.

## Outputs

Position/lane/direction/speed, next-edge/stage transitions, blocking/jam state,
and vehicle-conflict information.

## State

`MSPerson` owns a staged plan; `MSStageWalking` owns walking route/progress;
`MSPModel` subclasses own model-specific pedestrian state. The transportable
control schedules departures and stage changes.

## Preconditions

Walking route is connected for pedestrian permissions and required walking
areas/crossings exist or fallback behavior is defined.

## Decision Process

The selected model registers persons entering a walking stage and advances them
each simulation step/event. Non-interacting mode follows simplified lane-route
progress. Striping mode uses lateral stripes, leaders and conflict logic.
Optional JuPedSim delegates local motion through its adapter. Lane/link code
queries pedestrian positions when vehicles approach crossings/walking areas.

## Mathematical Model

Model-dependent. Non-interacting movement is route progress at desired/bounded
speed. Striping discretizes lateral space and chooses usable stripes/speeds;
JuPedSim supplies external operational dynamics. Common stage timing still uses
SUMO time and geometric lane positions.

## Constraints

Pedestrian permissions, walking direction, lane/access geometry, desired speed,
crossing signal, other agents, and vehicle conflict checks.

## State Transitions

`WAITING/DEPART -> WALKING(edge/lane) -> ACCESS/CROSSING/WALKING_AREA ->
ARRIVE_STAGE -> NEXT_STAGE/REMOVED`.

## Edge Cases

Disconnected pedestrian route, same-edge start/end, bidirectional sidewalk,
jammed pedestrian, vehicle collision, red crossing, access to stop/parking,
missing JuPedSim dependency, and save/load.

## Implementation

`src/microsim/transportables/MSPerson.*`, `MSStageWalking.*`,
`MSPModel.*`, `MSPModel_Striping.*`, `MSPModel_NonInteracting.*`, optional
JuPedSim adapter; vehicle conflict in `MSLink.cpp`/`MSLane.cpp`.

## Related Classes

`MSTransportableControl`, `MSLane`, `MSEdge` (including crossing/walking-area
flags), `MSLink`, `MSPerson::MSPersonStage_Access`, and
`MSStoppingPlace::Access`.

## Related Tests

`tests/sumo/pedestrian_model/`, junction/crossing cases, TraCI person tests,
and pedestrian tutorials under `tests/complex/tutorial/`.

## Behavioural Invariants

One active stage/location per person; position lies on the active geometry;
route index advances monotonically except explicit reroute; vehicle and person
views of crossing conflict agree.

## Reimplementation Notes

MUST preserve stage transitions, crossings and selected model semantics. MAY
replace spatial indexing. OPTIONAL: implement one pedestrian model initially;
do not label it compatible with striping/JuPedSim.

## Confidence

Medium-high for architecture; medium for model equations.
