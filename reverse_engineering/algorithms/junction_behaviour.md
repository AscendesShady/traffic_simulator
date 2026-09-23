# Junction behavior and conflict arbitration

## Purpose

Prevent incompatible lane-to-lane movements from occupying a conflict area at
unsafe times while respecting priority, signals, keep-clear and pedestrians.

## Inputs

`MSLink` state/direction/priority, foe link/lane sets, approaching vehicles and
persons with arrival/leave estimates, vehicle length/speed/deceleration,
impatience, visibility, continuation/internal-lane occupancy, and downstream
space.

## Outputs

Whether a movement is open, safe speed/virtual leaders, approach records, and
entry onto a via/target lane.

## State

Each link maintains mapped foes and an approaching-object map. Junction objects
own incoming/internal lanes. Vehicles hold drive items predicting link arrival
and departure.

## Preconditions

The network builder/loader produced consistent link order, foe/response logic,
internal-lane topology, and TL mapping.

## Decision Process

During plan movement, vehicles create drive items and lanes call
`setJunctionApproaches()`. `MSLink::opened()` checks signal/link state and calls
`blockedAtTime()` against foe approach intervals; it also accounts for priority,
continuation, impatience, and configured modes. `getLeaderInfo()` exposes
blocking vehicles/persons as virtual leaders so car-following can brake. At
movement execution the selected link is checked/entered and approach records
are removed or advanced.

## Mathematical Model

Conflict is temporal overlap of estimated occupation intervals, augmented by
braking feasibility and geometric/path checks. Arrival/leave times derive from
distance, speed, acceleration/deceleration, vehicle length and internal lengths.
Exact inequalities are in `MSLink::blockedAtTime()`, `blockedByFoe()`, and
`computeFoeArrivalTimeBraking()`.

## Constraints

Signal state, major/minor priority, foe response, downstream space, pedestrian
crossings, vehicle class, internal-lane availability, and zipper/rail special
rules.

## State Transitions

`unregistered -> approaching -> blocked/open -> via/internal lane -> target
lane -> approach record removed`.

## Edge Cases

No internal lanes, zipper merge, continuation after internal junction,
walking-area foe, oncoming pedestrian, impatience, ignore-foe modes, stranded
vehicles on red, spillback, and opposite-direction movement.

## Implementation

Primary: `src/microsim/MSLink.cpp` (`setApproaching`, `opened`,
`blockedAtTime`, `getLeaderInfo`), `MSLane.cpp`, `MSVehicle.cpp`, and junction
classes. Build-time source: `src/netbuild/NBNode.cpp`.

## Related Classes

`MSLink`, `MSRightOfWayJunction`, `MSInternalJunction`, `MSLane`, `MSVehicle`,
`MSPhaseDefinition`.

## Related Tests

`tests/sumo/junction_model/`, `tests/sumo/tls/`, pedestrian crossing tests,
and `tests/complex/traci/connection/`.

## Behavioural Invariants

Approach records represent live plans; incompatible movements are not admitted
solely because lanes are free; link/TL/foe indices agree; internal-lane entry
preserves route and occupancy.

## Reimplementation Notes

MUST preserve observable right-of-way and collision-avoidance semantics. MAY use
an explicit conflict-zone scheduler if its results match fixtures. Do not infer
priority from geometry at runtime when the network already encodes it.

## Confidence

High for the decision boundary; medium for all special-case timing branches.
