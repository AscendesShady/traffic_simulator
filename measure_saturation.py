"""Measure this simulator's actual saturation flow, headless.

Saturation flow (s) is the maximum rate one lane discharges past the stop bar
while a queue is still present. Webster's cycle-length formula needs it, and
the textbook 1800 veh/hr does not apply here: this is a small, slow abstraction
with its own vehicle lengths, speeds and car-following rules, so the number has
to be measured from the simulation itself.

Method (queue discharge, the HCM convention):

  1. Hold the approach RED while oversupplied demand builds a standing queue.
  2. Turn the approach GREEN and stop spawning, so a known, finite queue
     discharges and nothing new joins it.
  3. Time every stop-bar crossing, discard the first few vehicles as startup
     lost time, and take the mean headway of the vehicles behind them.
  4. s = 3600 / saturation_headway.

Holding permanent green from the start does NOT measure this. With no standing
queue the vehicles simply cruise through at free-flow speed and the count
tracks whatever rate they were injected at, so the "flow" rises with offered
demand instead of settling at the lane's capacity. Measuring the discharge of
a queue is what makes the result demand-independent.

This script only imports and drives the existing classes. It modifies no
project file, opens no window, and writes nothing to disk.
"""
from pathlib import Path
import statistics
import sys

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import canvas_gemini as canvas  # noqa: E402
import control_panel  # noqa: E402
import main  # noqa: E402
from signal_controller import SignalController  # noqa: E402

FPS = 60
MEASURED_APPROACH = "EB"
MEASURED_NODE = canvas.INT_X[0]          # Node A, x = 300
# main.py injects at x=-20, but the road from there to the stop bar only
# holds about eight cars, which caps the standing queue far too short to get a
# stable headway sample. Injecting further upstream (off-canvas, physics is
# unaffected) provides real queue storage without changing vehicle behaviour.
SPAWN_X = -900
CULL_X = canvas.WIDTH + 150

# Vehicles to queue before releasing the green. Long enough that the
# steady-state part of the discharge dominates the startup transient.
TARGET_QUEUE = 30
QUEUE_BUILD_LIMIT_SECONDS = 400.0
DISCHARGE_LIMIT_SECONDS = 200.0

# The first vehicles off the line accelerate from rest, so their headways are
# inflated by startup lost time and are excluded, per the HCM method.
STARTUP_LOST_VEHICLES = 4

# Offered demand while the queue builds. Only has to exceed zero discharge
# behind a red, so its exact value cannot influence the measured headway.
QUEUE_BUILD_VEH_PER_MIN = 120

# One release is a single sample of a stochastic vehicle mix, so the reported
# figure averages several independent seeds and states the spread.
SEEDS = (1, 777, 4242, 20260909, 31337)


def signal_state(approach_green):
    """Signal data for the vehicles, bypassing normal phase cycling.

    The controller is still stepped every frame so its reservation and
    conflict logic stays authoritative; only the displayed aspect is forced.
    Node B is held green so the measured lane never suffers spillback.
    """
    measured = "GREEN" if approach_green else "RED"
    return {
        canvas.INT_X[0]: {
            "EB": measured, "WB": "RED", "NB": "RED", "SB": "RED",
        },
        canvas.INT_X[1]: {
            "EB": "GREEN", "WB": "RED", "NB": "RED", "SB": "RED",
        },
    }


def single_lane_coords():
    """Three lane slots that all resolve to one physical lane.

    try_spawn_vehicle picks lane 0 or 1 for straight traffic. Pointing every
    slot at the same y keeps the run to a single lane, which is the unit
    saturation flow is defined in.
    """
    lane_y = canvas.H_Y - 1.5 * canvas.LANE
    return [lane_y, lane_y, lane_y]


def queued_upstream(vehicles):
    """Vehicles still waiting behind the measured stop bar."""
    return [
        vehicle
        for vehicle in vehicles
        if vehicle.is_front_bumper_upstream(
            MEASURED_NODE, canvas.H_Y, canvas.ROAD_W, canvas.STOP
        )
    ]


def measure(speed_scale, heavy_ratio=None, seed=20260909):
    """Discharge a standing queue and return its saturation statistics."""
    config = control_panel.global_config
    approach_cfg = dict(control_panel.approach_configs[MEASURED_APPROACH])
    if heavy_ratio is not None:
        approach_cfg["heavy_ratio"] = heavy_ratio
    approach_cfg.update(
        {
            "active": True,
            "model": "Poisson",
            "rate": QUEUE_BUILD_VEH_PER_MIN,
            "turn_split": 1.0,          # everything straight, one lane only
        }
    )

    config["random_seed"] = seed
    config["vehicle_speed_scale"] = speed_scale
    main.apply_configured_vehicle_speed_scale()
    main.reset_traffic_generation()

    controller = SignalController(
        {"green_time": 240, "is_running": True},
        yellow_time=60,
        red_clearance_time=60,
    )
    lane_coords = single_lane_coords()
    vehicles = []
    upstream_state = {}

    def step(frame, signals, spawning):
        """Advance one frame; return stop-bar crossings recorded this frame."""
        if spawning:
            main.try_spawn_vehicle(
                vehicles, MEASURED_APPROACH, MEASURED_APPROACH, SPAWN_X,
                lane_coords, approach_cfg, min_gap=40,
            )
        crossings = []
        for vehicle in list(vehicles):
            key = id(vehicle)
            if key not in upstream_state:
                upstream_state[key] = vehicle.is_front_bumper_upstream(
                    MEASURED_NODE, canvas.H_Y, canvas.ROAD_W, canvas.STOP
                )
            vehicle.update(
                signals, canvas.INT_X, canvas.H_Y,
                road_w=canvas.ROAD_W, stop_offset=canvas.STOP,
                lane_w=canvas.LANE, all_vehicles=vehicles,
                signal_controller=controller,
            )
            after = vehicle.is_front_bumper_upstream(
                MEASURED_NODE, canvas.H_Y, canvas.ROAD_W, canvas.STOP
            )
            if upstream_state[key] and not after:
                crossings.append(vehicle)
            upstream_state[key] = after
        controller.update(vehicles)
        for vehicle in [v for v in vehicles if v.x > CULL_X]:
            upstream_state.pop(id(vehicle), None)
        vehicles[:] = [v for v in vehicles if v.x <= CULL_X]
        return crossings

    # --- 1. Build a standing queue behind a red signal -------------------
    red = signal_state(False)
    build_frames = int(QUEUE_BUILD_LIMIT_SECONDS * FPS)
    for frame in range(build_frames):
        step(frame, red, spawning=True)
        if len(queued_upstream(vehicles)) >= TARGET_QUEUE:
            break
    queue_at_release = len(queued_upstream(vehicles))

    # --- 2. Release the green against a finite queue, no new arrivals ----
    green = signal_state(True)
    crossing_frames = []
    mix = []
    discharge_frames = int(DISCHARGE_LIMIT_SECONDS * FPS)
    for frame in range(discharge_frames):
        for vehicle in step(frame, green, spawning=False):
            crossing_frames.append(frame)
            mix.append("truck" if vehicle.is_heavy else "car")
        if not queued_upstream(vehicles):
            break   # queue exhausted; anything later is not saturated flow

    # --- 3. Saturation headway from the steady-state vehicles ------------
    headways = [
        (later - earlier) / FPS
        for earlier, later in zip(crossing_frames, crossing_frames[1:])
    ][STARTUP_LOST_VEHICLES:]
    saturation_headway = statistics.fmean(headways) if headways else float("nan")
    veh_per_hr = 3600.0 / saturation_headway if headways else float("nan")

    return {
        "speed_scale": speed_scale,
        "heavy_ratio": approach_cfg["heavy_ratio"],
        "queue_at_release": queue_at_release,
        "discharged": len(crossing_frames),
        "steady_state_vehicles": len(headways),
        "cars": mix.count("car"),
        "trucks": mix.count("truck"),
        "headway_s": saturation_headway,
        "veh_per_hr": veh_per_hr,
    }


def measure_across_seeds(speed_scale, heavy_ratio=None, seeds=SEEDS):
    """Average independent releases so one unlucky mix cannot set the answer."""
    runs = [
        measure(speed_scale, heavy_ratio=heavy_ratio, seed=seed)
        for seed in seeds
    ]
    flows = [run["veh_per_hr"] for run in runs]
    headways = [run["headway_s"] for run in runs]
    return {
        "speed_scale": speed_scale,
        "heavy_ratio": runs[0]["heavy_ratio"],
        "runs": len(runs),
        "veh_per_hr": statistics.fmean(flows),
        "veh_per_hr_sd": statistics.pstdev(flows),
        "veh_per_hr_min": min(flows),
        "veh_per_hr_max": max(flows),
        "headway_s": statistics.fmean(headways),
        "queue_at_release": runs[0]["queue_at_release"],
        "steady_state_vehicles": sum(r["steady_state_vehicles"] for r in runs),
        "cars": sum(run["cars"] for run in runs),
        "trucks": sum(run["trucks"] for run in runs),
    }


def report(result):
    print(
        f"  Saturation flow @ speed_scale={result['speed_scale']:.2f}: "
        f"{result['veh_per_hr']:.0f} veh/hr/lane"
    )
    print(
        f"    (mean of {result['runs']} releases of a {result['queue_at_release']}"
        f"-vehicle queue; {result['steady_state_vehicles']} steady-state "
        f"headways after {STARTUP_LOST_VEHICLES} startup vehicles each)"
    )
    print(
        f"    mean saturation headway {result['headway_s']:.2f}s; "
        f"spread {result['veh_per_hr_min']:.0f}-{result['veh_per_hr_max']:.0f} "
        f"veh/hr (sd {result['veh_per_hr_sd']:.0f})"
    )
    print(
        f"    mix discharged: {result['cars']} cars / {result['trucks']} trucks"
    )


def main_entry():
    print("=" * 70)
    print("SATURATION FLOW CALIBRATION  (queue-discharge method)")
    print(
        f"  one lane, {MEASURED_APPROACH} at node x={MEASURED_NODE}; "
        f"queue of {TARGET_QUEUE} built behind red, then released"
    )
    print("=" * 70)

    results = {}
    for scale in (1.0, 0.5):
        results[scale] = measure_across_seeds(scale)
        report(results[scale])

    print()
    print("Vehicle mix sensitivity (speed_scale=1.0):")
    default_heavy = control_panel.approach_configs[MEASURED_APPROACH][
        "heavy_ratio"
    ]
    for heavy_ratio in (0.0, default_heavy, 0.5, 1.0):
        mix = measure_across_seeds(1.0, heavy_ratio=heavy_ratio)
        label = "  <- project default" if heavy_ratio == default_heavy else ""
        print(
            f"  heavy_ratio={heavy_ratio:.2f}: {mix['veh_per_hr']:6.0f} veh/hr "
            f"(headway {mix['headway_s']:.2f}s, "
            f"{mix['cars']} cars / {mix['trucks']} trucks){label}"
        )

    print()
    fast, slow = results[1.0]["veh_per_hr"], results[0.5]["veh_per_hr"]
    print(f"Speed-scale sensitivity: 1.0x is {fast / slow:.2f}x the 0.5x flow.")
    print(
        "Use the value matching the speed scale the benchmark runs at when "
        "computing Webster timings."
    )


if __name__ == "__main__":
    main_entry()
