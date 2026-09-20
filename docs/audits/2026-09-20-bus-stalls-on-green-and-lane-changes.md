# Buses stalled on green; car-following and discretionary lane changes (2026-09-20)

## Symptom

Windowed runs showed buses sitting still at a green signal.

## Method

Headless probe (`src/experiments/headless_run.py` loop, TSP+DBL on, the
double-demand benchmark regime, 3 seeds x 10 min): flag every bus stopped
for >= 8 s while its target node showed GREEN for its approach, and snapshot
why (`should_stop` inputs, lead vehicle, reservation state).

## Findings

52 stalls; 49 were "in a queue behind a stopped vehicle". Root cause was the
following law in `Vehicle.update`: inside the 12-37 px gap band a vehicle
could only *decelerate* toward the gap-proportional target speed, so one at
rest never restarted until a full 37 px opened ahead. Queues restarted as a
~1 s/vehicle wave and a bus behind a car crawling at 30 px stayed at 0.

Remaining 2-3 per seed: a straight bus at the bar with a clear road, denied
an intersection reservation because a same-approach permissive left-turner
holds the corner (`SignalController._vehicle_blocks_entry`, the corner-sweep
rule that the left-turn starvation gate is built on). Left as is: that is
right-of-way policy, and it applies to cars and buses alike.

## Changes

- `vehicle.py`: `follow_target_speed` is the one following law (stop < 12 px,
  gap/30 x desired up to 37 px, free flow beyond); the kinematics ramp
  *toward* it in both directions.
- `vehicle.py`: MOBIL-lite discretionary lane changes for straight cars
  between lanes 0 and 1 (lane 2 stays the left-turn/DBL lane). Incentive =
  follow-speed gain as a fraction of desired speed on the driver's own
  heterogeneous desired speed; inward needs +20 %, outward may lose 10 %
  (keep-outer). Safety = nobody alongside in the target lane and >= 30 px
  behind. Per-frame hazard 1/120 from the run-seeded RNG. No per-lane speed
  cap, so the travel-delay DV keeps each vehicle's own free flow.
- `vehicle.py`: cooperative slides (`step_lane_vacate`, DBL evictions
  included) check only the target lane band, not the lane being left; the
  origin-lane leader is a following-distance matter.
- `main.py`: spawn/dispatch entry clearance uses a vehicle-width lateral
  window (`SPAWN_LATERAL_BLOCK_PX`) so a vehicle straddling two lanes blocks
  both; previously buses could spawn onto a sliding car.
- `main.py`, `measure_saturation.py`: the single-lane saturation measure runs
  under `vehicle.lane_changes_suspended()`.

## Measured effect

| | before | after |
|---|---|---|
| green-stalls >= 8 s, seed 1, 10 min, double demand | 17 | 4 |
| saturation flow @ 0.5x speed (script, 5 seeds) | 1440 veh/h/lane | 1793 (HCM base 1900) |
| same-direction overlaps, 3 seeds x 5 min, sampled 2 Hz | 0 | 0 |
| discretionary lane changes, 5 min default demand | 0 | 60-100 |

Same seed no longer reproduces pre-change traffic (the hazard draws from the
shared RNG). `config_hash` now includes `vehicle.movement_model_signature()`
(following-law and lane-change tunables), so rows from before this date
cannot pair with rows after it.
