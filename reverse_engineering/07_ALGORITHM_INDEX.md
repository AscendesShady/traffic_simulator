# 07. Algorithm index

| Algorithm / behavior | Detailed document | Primary source | Principal tests |
|---|---|---|---|
| car following | `algorithms/car_following.md` | `src/microsim/cfmodels/`, `MSVehicle.cpp` | `tests/sumo/cf_model/`, CF gtests |
| speed/integration | `algorithms/speed_control.md` | `MSVehicle::planMove/executeMove`, `MSCFModel` | CF/action-step/TLS tests |
| lane changing | `algorithms/lane_changing.md` | `lcmodels/`, `MSLaneChanger*` | LC/sublane/opposite tests |
| link/junction arbitration | `algorithms/junction_behaviour.md` | `MSLink.cpp`, junction/lane/vehicle classes | junction/TLS/pedestrian tests |
| traffic-light control | `algorithms/traffic_light_control.md` | `microsim/traffic_lights/` | TLS and TraCI trafficlight |
| insertion/flow expansion | `algorithms/vehicle_insertion.md` | `MSInsertionControl`, `MSEdge`, `MSLane` | departure/flow/TraCI add |
| arrival/removal/transfer | `algorithms/vehicle_removal.md` | `MSVehicleControl`, `MSVehicleTransfer` | arrival/teleport/state |
| collision recovery | `algorithms/collision_handling.md` | `MSLane::detect/handleCollision*` | CF/LC/junction/pedestrian bugs |
| offline/runtime routing | `algorithms/routing.md` | `src/utils/router`, `src/router`, `MSRoutingEngine` | router suites/rerouting |
| public transport | `algorithms/public_transport.md` | stops, vehicle stop logic, transportables | PT/person/busstop/state |
| pedestrian movement | `algorithms/pedestrian_movement.md` | `MSPModel*`, walking stages | pedestrian/TraCI/tutorial |
| network compute/generation | `algorithms/network_generation.md` | `NBNetBuilder`, `NG*`, import/write | netconvert/netgen |
| demand generation/expansion | `algorithms/demand_generation.md` | od/activitygen/randomTrips/insertion | `src/od/`, `src/activitygen/`, `tools/randomTrips.py` |
| mesoscopic movement | `simulation/other_simulation_behaviour.md` | `src/mesosim/` | meso variants |

## Selection versus orchestration

Several algorithms are families selected by configuration: car-following,
lane-changing, routing, TLS and pedestrian movement. Their selection/defaults
are part of observable behavior. Orchestration—especially step order and how
algorithms exchange constraints—is as important as each formula.

## Evidence levels

- High confidence: call path and common state/constraints traced in source.
- Medium confidence: individual specialized variants only mapped/sketched.
- Tests listed are behavioral corpora, not proof of exhaustive coverage; see
  `13_TESTING_ARCHITECTURE.md`.
