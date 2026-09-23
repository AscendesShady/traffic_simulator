# main.py
import pygame
import sys
import random
import math
import json
import csv
import subprocess
import atexit
import time
import uuid
import hashlib
import statistics
import datetime
import copy
import shutil
from collections import deque
import tkinter as tk
from tkinter import ttk
from pathlib import Path
# Running this file directly (python src/.../x.py, or the IDE Run button) puts
# its own folder on sys.path instead of the repo root; put the root back so
# the src.* imports below resolve the same way they do under run.py / -m.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from src.ui import canvas_gemini as canvas
from src.ui import control_panel
from src.core import guard
from src.core import webster
from src.experiments import batch_runner
from src.agents import rule_controller
from src.core import vehicle as vehicle_module
from src.core.vehicle import (
    Vehicle, Bus, DBL_LANE_INDEX, BUS_PASSENGERS, lane_changes_suspended,
)
from src.core.signal_controller import SignalController, TSP_MAX_ADJUST_FRACTION, MIN_GREEN_FRAMES
from src.ui.telemetry_dashboard import TelemetryDashboard, build_excel_export_filename
from src.telemetry.telemetry_exporter import TelemetryExporter
from src.telemetry.gridlock_monitor import GridlockMonitor, report_path_for
from src.telemetry.bus_event_log import (
    BUS_NODE_EVENT_HEADERS,
    BusEventTracker,
    iter_bus_node_event_rows,
    write_bus_events_sheet,
)
from src.telemetry import real_world_units

# ==========================================================
# STOCHASTIC SPAWNER ENGINE (COMPOUND POISSON / EXACT RATE)
# ==========================================================
CONGESTION_MODEL = "Congestion Peak"
# A vehicle whose centre is within this of a spawn lane's centre blocks a
# spawn there: one vehicle width (widest is the 14 px bus), so a vehicle
# sliding between lanes counts for the lane it is straddling.
SPAWN_LATERAL_BLOCK_PX = 15.0
CONGESTION_PEAK_SECONDS = 30
CONGESTION_RECOVERY_SECONDS = 30
CONGESTION_RATE_MULTIPLIER = 4.0
CONGESTION_MIN_PEAK_RATE_VPM = 90.0
MAX_PENDING_ARRIVALS = 5000
POST_DISCHARGE_METER_SECONDS = 10
POST_DISCHARGE_RELEASE_GAP_FRAMES = 60
post_discharge_meter_frames_remaining = 0


def _new_spawner_state():
    return {
        # Exogenous demand: one seeded stream per source, and every offered
        # arrival is drawn complete (turn, lane, type, speed, color) at offer
        # time and queued at the source until the entry is clear, so what the
        # network does downstream can never change what is offered upstream.
        # Seeded off the global RNG once, at reset, so a test that seeds
        # `random` gets deterministic sources; a run reseeds them per source
        # from its own seed (seed_demand_streams) and never touches them again.
        "rng": random.Random(random.getrandbits(64)),
        "clock": 0,
        "arrivals": deque(),
        "frames_since_spawn": 999,
        "pending_arrivals": 0,
        # Passengers aboard the arrivals queued here (car 4, truck 1): the
        # latent-demand exposure accumulate_frame_metrics integrates.
        "pending_pax": 0.0,
        "congestion_cycle_frame": 0,
        "peak_active": False,
        "effective_rate_vpm": 0.0,
        "requested_arrivals": 0,
        "offered_passengers": 0.0,
        "admitted_arrivals": 0,
        "overflow_arrivals": 0,
        "last_model": None,
    }


spawner_states = {
    approach_key: _new_spawner_state()
    for approach_key in ("EB", "WB", "A_NB", "A_SB", "B_NB", "B_SB")
}

bus_dispatch_counters = { r_id: 0.0 for r_id in control_panel.bus_routes_config.keys() }
bus_pending_trips = {r_id: deque() for r_id in control_panel.bus_routes_config}
bus_schedule_time_s = 0.0
bus_sequence_counter = 0
network_throughput = {
    "passengers_served_total": 0,
    "passengers_served_bus": 0,
    "passengers_served_car": 0,
    "vehicles_served_total": 0,
    "buses_served": 0,
    "cars_served": 0,
    # Vehicle-frames spent stopped (speed below the queue threshold); the
    # exporter turns this into mean stopped delay per served vehicle.
    "stopped_vehicle_frames": 0,
    # Passenger-frames of stopped delay, split bus vs car/truck -- the
    # exporter turns these into person-hours and per-passenger means.
    "bus_passenger_delay_frames": 0,
    "car_passenger_delay_frames": 0,
    # Passenger-frames of travel-time delay: time lost below the vehicle's
    # own free-flow speed, sum(1 - speed/max_speed) per frame. Unlike the
    # stopped counters above this also sees crawl, so a controller cannot
    # look better by turning stops into creeping. Floats.
    "bus_passenger_travel_delay_frames": 0.0,
    "car_passenger_travel_delay_frames": 0.0,
    # Passenger-frames in the network (every vehicle, every frame): the
    # exposure a censored (unfinished) vehicle contributes.
    "passenger_frames_in_network": 0,
    # The same delays, but only for vehicles that have completed: the
    # counters above include vehicles still in the network, whose delay is
    # in the numerator while they are missing from the served denominator,
    # so every per-vehicle / per-passenger MEAN divides these instead.
    # Filled by _record_completed_vehicle_delay.
    "completed_stopped_vehicle_frames": 0,
    "completed_bus_passenger_delay_frames": 0,
    "completed_car_passenger_delay_frames": 0,
    # Arrival-to-departure delay of completed vehicles, vehicle-weighted:
    # frames lost below own free-flow speed over the whole route (HCM
    # control delay summed over the nodes the vehicle crossed). Float.
    "completed_control_delay_frames": 0.0,
    # Source wait of completed vehicles (entry_delay_frames), vehicle-weighted.
    "completed_entry_delay_frames": 0,
    # Latent demand: passenger-frames spent waiting at a source because the
    # entry was full -- offered arrivals not yet on the road (car 4 / truck 1
    # pax each) and bus trips due but not yet dispatched (BUS_PASSENGERS).
    # Exposure, like the travel-delay counters: every frame, finished or not.
    # Campaign 2026-09-22 served only 58-62 % of the vehicles it offered and
    # none of this wait was in any DV; a controller that admits less traffic
    # looked better per vehicle. See total_person_hours_delay_incl_entry_steady.
    "car_passenger_entry_wait_frames": 0.0,
    "bus_passenger_entry_wait_frames": 0.0,
    "vehicles_waiting_entry_frames": 0,
    # Surrogate-safety counters mirrored from vehicle.SAFETY_COUNTERS each
    # frame, so warm-up discard gives them steady-state values too.
    "ssm_emergency_decel_frames": 0,
    "ssm_emergency_decel_events": 0,
    "ssm_leader_clamp_frames": 0,
    "ssm_stop_line_clamp_frames": 0,
    "ssm_ttc_conflict_frames": 0,
    "ssm_ttc_conflicts": 0,
    # Congestion sampled once per simulated frame, for a run-long mean/max.
    "vehicles_in_network_frame_sum": 0,
    "vehicles_in_network_sample_count": 0,
    "vehicles_in_network_max": 0,
}
# network_throughput as it stood when the warm-up window ended, so a
# steady-state DV is "current counter minus this". Empty until that frame.
network_throughput_at_warmup = {}
# 120 s at 60 fps: the network takes ~90 s to fill from empty, so a
# cumulative DV over a short run is mostly fill. Operator-configurable.
WARMUP_DISCARD_FRAMES = 7200


def _empty_approach_metric():
    return {
        f"node_{node_x}_{approach}": 0
        for node_x in canvas.INT_X
        for approach in ("EB", "WB", "NB", "SB")
    }


def _new_run_metrics():
    """Exact per-run counters that do not belong in sampled telemetry."""
    return {
        "buses_spawned": 0,
        "bus_trips_scheduled": 0,
        "bus_trips_missed": 0,
        "bus_source_delay_sec": 0.0,
        "vehicles_in_network_current": 0,
        "vehicles_in_network_steady_sum": 0,
        "vehicles_in_network_steady_samples": 0,
        "bus_delay_samples_sec": [],
        "car_delay_samples_sec": [],
        "bus_delay_samples_steady_sec": [],
        "car_delay_samples_steady_sec": [],
        "cross_street_vehicle_delay_frames": 0,
        "cross_street_passenger_delay_frames": 0,
        "cross_street_vehicle_ids": set(),
        "cross_street_passenger_ids": set(),
        "cross_street_unique_pax": 0,
        "queue_sum_by_approach": _empty_approach_metric(),
        "queue_max_by_approach": _empty_approach_metric(),
        "queue_samples": 0,
        "spillback_events": 0,
        "spillback_active_ids": set(),
        "node_stopped_vehicle_frames": {str(node_x): 0 for node_x in canvas.INT_X},
        # Stopped frames on the approach, credited to the node once the
        # vehicle has crossed it (completed cohort for node_*_mean_delay_sec).
        "node_completed_stopped_frames": {str(node_x): 0 for node_x in canvas.INT_X},
        "node_throughput_veh": {str(node_x): 0 for node_x in canvas.INT_X},
        "node_phase_failures": {str(node_x): 0 for node_x in canvas.INT_X},
        "node_max_queue_len": {str(node_x): 0 for node_x in canvas.INT_X},
        "last_node_phases": {},
        # turn -> (frame of the telemetry snapshot the decider saw, frame the
        # decision first took effect here); see _record_decision_applied.
        "decision_applied": {},
    }


run_metrics = _new_run_metrics()


def reset_run_metrics():
    run_metrics.clear()
    run_metrics.update(_new_run_metrics())


def warmup_discard_frames():
    try:
        return max(0, int(control_panel.global_config.get(
            "warmup_discard_frames", WARMUP_DISCARD_FRAMES
        )))
    except (TypeError, ValueError, OverflowError):
        return WARMUP_DISCARD_FRAMES


def snapshot_warmup_baseline(master_frame_count):
    """Freeze the cumulative counters at the end of warm-up, once per run."""
    if network_throughput_at_warmup or master_frame_count < warmup_discard_frames():
        return
    network_throughput_at_warmup.update(network_throughput)
    network_throughput_at_warmup["frame"] = int(master_frame_count)


# Offered-demand fingerprint: every offered arrival (source, offer frame and
# the complete attribute draw) so "same seed = same demand" is a one-column
# check, and pair_against_baseline refuses a pair whose schedules differ.
# Reset each run.
# (offer frame, fingerprint) per exogenous arrival, in draw order. Not a
# rolling digest: see _demand_draw_hash_hex.
_demand_draw_state = {"offers": []}

# Wall-clock seconds this run has actually spent running (not paused, not
# calibrating), accumulated by the Tk loop. Divided into the sim clock it
# gives sim_seconds_per_wall_second: the ratio the agent's latency
# arithmetic silently assumes is 1.0. decision_lag_seconds measures a model
# call in wall time and eta_at_decision_land subtracts it from a sim-time
# ETA, so at any other ratio every arm plans with the wrong horizon -- and
# the error scales with the ratio, which varies with machine load and with
# how much CPU a local model is taking. Headless leaves this at zero (it is
# not paced against a clock) and the column is then empty.
_pace_state = {"wall_seconds": 0.0}

# In-network saturation-flow check (validation, not calibration). Every
# straight stop-bar crossing on a green is logged per lane; within one green,
# headways from the 5th crossing on that are no longer than
# CALIBRATION_INTERRUPTION_FACTOR x the calibrated saturation headway are
# saturation headways in HCM's sense (7th ed., Ch. 31: from the fifth queued
# vehicle, uninterrupted discharge). Their mean gives the saturation flow the
# network actually achieves, reported beside the calibrated S. Campaign
# 2026-09-22 reported v/c 0.78-0.91 while serving 58-62 % of its demand:
# the single-lane calibration was not the network's capacity, and nothing
# said so.
_discharge_state = {"last": {}, "headways": []}
NETWORK_S_MIN_SAMPLES = 30
DEMAND_MODEL_SIGNATURE = "per_source_streams_queued_v2"
VEHICLE_COLORS = [(50, 150, 250), (250, 100, 50), (250, 200, 50), (150, 50, 250), (50, 200, 150)]


def _demand_draw_hash_hex(up_to_frame=None):
    """Fingerprint of the offered demand schedule through ``up_to_frame``.

    Keyed by the frame each arrival was offered on, never by "everything
    drawn so far": a running digest read at export made the fingerprint
    depend on the frame the export happened to land on, and a single frame
    of overshoot changed it. Campaign 2026-09-22 split every seed into two
    groups one offered car apart (2803 vs 2802 at seed 234) whose demand was
    in fact identical, and pair_against_baseline refused 13 of 29 arms on
    it. The 5- and 10-minute checkpoints agreed exactly; only the final
    export drifted, which is why the earlier at_export_boundary fix reduced
    the damage without ending it. Hashing by frame makes the fingerprint a
    property of the schedule, so an overshoot cannot change it at all.
    """
    digest = hashlib.sha256()
    for frame, fingerprint in _demand_draw_state["offers"]:
        if up_to_frame is not None and frame > up_to_frame:
            continue
        digest.update(fingerprint)
    return digest.hexdigest()[:16]


def seed_demand_streams(seed):
    """Give every source its own stream, derived from the run seed, so the
    demand a seed produces is independent of every other random consumer
    (other sources, behavioural draws such as lane changes)."""
    for approach_key, state in spawner_states.items():
        state["rng"].seed(None if seed is None else f"{seed}:{approach_key}")


# Desired free-flow speed, px/frame before the speed scale, drawn uniformly
# per arrival. Named because signal timing reads them too: the ITE change
# interval is set from the 85th-percentile approach speed.
CAR_DESIRED_SPEED_RANGE = (1.0, 1.4)
TRUCK_DESIRED_SPEED_RANGE = (0.8, 1.1)


def _draw_arrival(approach_key, state, approach_cfg):
    rng = state["rng"]
    direction = approach_key.rsplit("_", 1)[-1]
    straight_ratio = approach_cfg.get("turn_split", 0.80)
    far_share = approach_cfg.get("left_far_share", 0.0)
    options = control_panel.APPROACH_TURN_OPTIONS[approach_key]
    draw = rng.random()
    if draw < straight_ratio:
        target_turn = "STRAIGHT"
        left_nodes = ()
        lane_idx = rng.choice([0, 1])
    else:
        # Second option only where the approach has one (else it is the
        # first); which option decides the left nodes (control_panel table).
        far = len(options) > 1 and draw >= 1.0 - far_share
        left_nodes = options[1 if far else 0][1]
        first_node = canvas.INT_X[0] if direction == "EB" else canvas.INT_X[-1]
        if direction in ("EB", "WB") and left_nodes[0] != first_node:
            # Turns at the far node: runs with through traffic and merges
            # into the turn lane on the approach to that node.
            target_turn = "STRAIGHT"
            lane_idx = rng.choice([0, 1])
        else:
            target_turn = "LEFT"
            lane_idx = DBL_LANE_INDEX
    is_heavy = rng.random() < approach_cfg.get("heavy_ratio", 0.10)
    base_speed = rng.uniform(*TRUCK_DESIRED_SPEED_RANGE) if is_heavy else rng.uniform(*CAR_DESIRED_SPEED_RANGE)
    color = (120, 120, 140) if is_heavy else rng.choice(VEHICLE_COLORS)
    return {
        "frame": state["clock"], "target_turn": target_turn, "left_nodes": left_nodes,
        "lane_idx": lane_idx, "is_heavy": is_heavy,
        "speed": base_speed * get_active_vehicle_speed_scale(), "color": color,
    }


def _record_demand_offer(approach_key, state, count=1, approach_cfg=None):
    """Draw, count and fingerprint exogenous arrivals before source admission.

    Each arrival is drawn complete here and queued at the source; a blocked
    entry delays it, never loses or redraws it. Offered passengers use the
    drawn vehicle type (car=4, truck=1), independent of source blockage.
    """
    if approach_cfg is None:
        approach_cfg = control_panel.approach_configs.get(approach_key, {})
    for _ in range(max(0, int(count))):
        arrival = _draw_arrival(approach_key, state, approach_cfg)
        state["requested_arrivals"] += 1
        state["offered_passengers"] += 1.0 if arrival["is_heavy"] else 4.0
        arrival["identity"] = f"{approach_key}:{state['requested_arrivals']}"
        _demand_draw_state["offers"].append((
            int(arrival["frame"]),
            f"{approach_key}|{state['requested_arrivals']}|{arrival['frame']}|"
            f"{arrival['target_turn']}|{arrival['lane_idx']}|{int(arrival['is_heavy'])}|"
            f"{arrival['speed']:.4f}|".encode("utf-8"),
        ))
        if state["pending_arrivals"] < MAX_PENDING_ARRIVALS:
            state["pending_arrivals"] += 1
            state["pending_pax"] += 1.0 if arrival["is_heavy"] else 4.0
            state["arrivals"].append(arrival)
        else:
            state["overflow_arrivals"] += 1


# git_sha/git_dirty describe the code the CURRENT process is running, which
# cannot change mid-process, so one lookup is cached for the whole session.
_GIT_INFO_CACHE = None


def _git_info():
    global _GIT_INFO_CACHE
    if _GIT_INFO_CACHE is None:
        try:
            sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=BASE_DIR, stderr=subprocess.DEVNULL
            ).decode().strip()
        except Exception:
            sha = None
        try:
            dirty = bool(
                subprocess.check_output(
                    ["git", "status", "--porcelain"],
                    cwd=BASE_DIR, stderr=subprocess.DEVNULL,
                ).decode().strip()
            )
        except Exception:
            dirty = None
        _GIT_INFO_CACHE = (sha, dirty)
    return _GIT_INFO_CACHE


# Regime fields hashed into config_hash: everything that must stay frozen for
# the whole campaign (demand, geometry/timing-derived state, run length).
# Deliberately excludes per-run/per-arm values (seed, test_model, timestamps)
# and treatment: a route's tsp/dbl flags are what the arm *does*, recorded
# in the model/llm_* columns, never part of the regime. The hash is frozen
# once per run at reset (config["_regime_hash"]); a checkpoint re-hashing
# live config could otherwise describe a different regime mid-run.
_CONFIG_HASH_GLOBAL_KEYS = (
    "vehicle_speed_scale", "priority_eligibility_px", "measured_saturation_flow",
    "warmup_discard_frames", "cycle_time_sec", "test_duration_sim_seconds",
    "signal_change_intervals", "signal_coordination", "coordination_direction",
    "_signal_timing",
)
_ROUTE_TREATMENT_KEYS = ("tsp_enabled", "dbl_enabled", "manual_dispatch")


def _config_regime_hash(config):
    payload = {
        "global": {key: config.get(key) for key in _CONFIG_HASH_GLOBAL_KEYS},
        "approach_configs": control_panel.approach_configs,
        "bus_routes_config": {
            route_id: {k: v for k, v in cfg.items() if k not in _ROUTE_TREATMENT_KEYS}
            for route_id, cfg in control_panel.bus_routes_config.items()
        },
        # Geometry is part of the regime: rows from a different network
        # length must never pair with these.
        "geometry": [canvas.WIDTH, canvas.HEIGHT, canvas.H_Y, list(canvas.INT_X)],
        # So are the movement-model tunables: following law and lane-change
        # incentives change capacity, so rows across a retune never pair.
        "movement": vehicle_module.movement_model_signature(),
        "demand": DEMAND_MODEL_SIGNATURE,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _current_campaign_id(config):
    """One id per frozen-config campaign: the batch runner's id when this run
    is part of a batch, otherwise the run's own id (a campaign of one)."""
    batch_runtime = config.get("batch_runtime") or {}
    campaign_id = batch_runtime.get("campaign_id")
    return campaign_id or config.get("_run_uuid", "")


def _network_demand_model(approach_configs):
    active_models = {
        cfg.get("model", "Poisson")
        for cfg in approach_configs.values()
        if isinstance(cfg, dict) and cfg.get("active", False)
    }
    if not active_models:
        return None
    return next(iter(active_models)) if len(active_models) == 1 else "mixed"


def _record_node_crossings(vehicle):
    seen = getattr(vehicle, "_experiment_nodes_counted", set())
    if not isinstance(seen, set):
        seen = set()
    for node_x in getattr(vehicle, "passed_nodes", set()) - seen:
        key = str(node_x)
        if key in run_metrics["node_throughput_veh"]:
            run_metrics["node_throughput_veh"][key] += 1
            run_metrics["node_completed_stopped_frames"][key] += int(
                getattr(vehicle, "_experiment_node_stopped_frames", {}).get(key, 0)
            )
    vehicle._experiment_nodes_counted = set(getattr(vehicle, "passed_nodes", set()))


def _record_completed_vehicle_delay(vehicle):
    delay_sec = float(getattr(vehicle, "_experiment_stopped_frames", 0)) / 60.0
    steady_delay_sec = (
        float(getattr(vehicle, "_experiment_steady_stopped_frames", 0)) / 60.0
    )
    passengers = max(0, int(getattr(vehicle, "passengers", 0) or 0))
    stopped_frames = int(getattr(vehicle, "_experiment_stopped_frames", 0))
    network_throughput["completed_stopped_vehicle_frames"] += stopped_frames
    network_throughput["completed_control_delay_frames"] += float(
        getattr(vehicle, "_experiment_travel_delay_frames", 0.0)
    )
    network_throughput["completed_entry_delay_frames"] += int(
        getattr(vehicle, "entry_delay_frames", 0) or 0
    )
    network_throughput[
        "completed_bus_passenger_delay_frames" if isinstance(vehicle, Bus)
        else "completed_car_passenger_delay_frames"
    ] += stopped_frames * passengers
    target = (
        run_metrics["bus_delay_samples_sec"]
        if isinstance(vehicle, Bus)
        else run_metrics["car_delay_samples_sec"]
    )
    steady_target = (
        run_metrics["bus_delay_samples_steady_sec"]
        if isinstance(vehicle, Bus)
        else run_metrics["car_delay_samples_steady_sec"]
    )
    if passengers:
        target.append((delay_sec, passengers))
        steady_target.append((steady_delay_sec, passengers))


def accumulate_frame_metrics(vehicles, signal_controller=None):
    """Update network_throughput's per-frame cumulative counters.

    Called once per simulated frame (never per callback, so it stays exact
    regardless of how many frames one Tk callback processes): the stopped-
    vehicle count, that same delay split passenger-weighted by mode (a
    queued bus's whole passenger load counts toward bus delay, a queued
    car/truck's toward car delay -- the standard transit-priority weighting),
    and network occupancy for a run-long mean/max. Every counter here is
    cumulative from t=0 and is zeroed only by perform_full_reset.
    """
    stopped_count = 0
    stopped_bus_pax = 0
    stopped_car_pax = 0
    queue_counts = _empty_approach_metric()
    spillback_now = set()
    cross_requests = {}
    if signal_controller is not None:
        for node_x, node in getattr(signal_controller, "nodes", {}).items():
            request = getattr(node, "active_request", None)
            if request is not None and getattr(request, "tsp_action", "none") != "none":
                cross_requests[int(node_x)] = set(
                    getattr(request, "conflicting_approaches", ())
                )
    travel_bus_pax = 0.0
    travel_car_pax = 0.0
    for v in vehicles:
        _record_node_crossings(v)
        free_flow = float(getattr(v, "max_speed", 0.0) or 0.0)
        if free_flow > 0:
            lost = 1.0 - min(max(float(v.speed), 0.0), free_flow) / free_flow
            v._experiment_travel_delay_frames = (
                float(getattr(v, "_experiment_travel_delay_frames", 0.0)) + lost
            )
            if isinstance(v, Bus):
                travel_bus_pax += int(getattr(v, "passengers", 0)) * lost
            else:
                travel_car_pax += int(getattr(v, "passengers", 0)) * lost
        if v.speed < 0.25:
            stopped_count += 1
            pax = int(getattr(v, "passengers", 0))
            v._experiment_stopped_frames = (
                int(getattr(v, "_experiment_stopped_frames", 0)) + 1
            )
            if network_throughput_at_warmup:
                v._experiment_steady_stopped_frames = (
                    int(getattr(v, "_experiment_steady_stopped_frames", 0)) + 1
                )
            if isinstance(v, Bus):
                stopped_bus_pax += pax
            else:
                stopped_car_pax += pax
            try:
                target_node = v.get_next_target_node(canvas.INT_X)
                if (
                    target_node in canvas.INT_X
                    and v.is_front_bumper_upstream(
                        target_node, canvas.H_Y, canvas.ROAD_W, canvas.STOP
                    )
                ):
                    approach_key = f"node_{target_node}_{v.direction}"
                    if approach_key in queue_counts:
                        queue_counts[approach_key] += 1
                    run_metrics["node_stopped_vehicle_frames"][str(target_node)] += 1
                    per_node = getattr(v, "_experiment_node_stopped_frames", None)
                    if per_node is None:
                        per_node = v._experiment_node_stopped_frames = {}
                    per_node[str(target_node)] = per_node.get(str(target_node), 0) + 1
                    if v.direction in cross_requests.get(target_node, set()):
                        run_metrics["cross_street_vehicle_delay_frames"] += 1
                        run_metrics["cross_street_passenger_delay_frames"] += pax
                        run_metrics["cross_street_vehicle_ids"].add(v.serial)
                        if pax and v.serial not in run_metrics["cross_street_passenger_ids"]:
                            run_metrics["cross_street_passenger_ids"].add(v.serial)
                            run_metrics["cross_street_unique_pax"] += pax
            except Exception:
                pass
        try:
            target_node = v.get_next_target_node(canvas.INT_X)
            upstream = v.is_front_bumper_upstream(
                target_node, canvas.H_Y, canvas.ROAD_W, canvas.STOP
            )
            previous = getattr(v, "_bar_state", None)
            v._bar_state = (target_node, upstream)
            if previous and previous[1] and previous[0] == target_node and not upstream:
                _record_stop_bar_crossing(v, target_node, signal_controller)
            if upstream and v.is_spillback_blocked(
                target_node, canvas.H_Y, canvas.ROAD_W, vehicles
            ):
                spillback_now.add(v.serial)
        except Exception:
            pass
    network_throughput["stopped_vehicle_frames"] += stopped_count
    network_throughput["bus_passenger_delay_frames"] += stopped_bus_pax
    network_throughput["car_passenger_delay_frames"] += stopped_car_pax
    network_throughput["bus_passenger_travel_delay_frames"] += travel_bus_pax
    network_throughput["car_passenger_travel_delay_frames"] += travel_car_pax
    network_throughput["passenger_frames_in_network"] += sum(
        int(getattr(v, "passengers", 0)) for v in vehicles
    )
    network_throughput["vehicles_in_network_frame_sum"] += len(vehicles)
    network_throughput["vehicles_in_network_sample_count"] += 1
    if len(vehicles) > network_throughput["vehicles_in_network_max"]:
        network_throughput["vehicles_in_network_max"] = len(vehicles)
    waiting_pax = 0.0
    waiting_vehicles = 0
    for state in spawner_states.values():
        waiting_pax += float(state.get("pending_pax", 0.0) or 0.0)
        waiting_vehicles += int(state.get("pending_arrivals", 0) or 0)
    pending_trips = sum(len(trips) for trips in bus_pending_trips.values())
    network_throughput["car_passenger_entry_wait_frames"] += waiting_pax
    network_throughput["bus_passenger_entry_wait_frames"] += pending_trips * BUS_PASSENGERS
    network_throughput["vehicles_waiting_entry_frames"] += waiting_vehicles + pending_trips
    for key, value in vehicle_module.SAFETY_COUNTERS.items():
        network_throughput["ssm_" + key] = value
    run_metrics["vehicles_in_network_current"] = len(vehicles)
    if network_throughput_at_warmup:
        run_metrics["vehicles_in_network_steady_sum"] += len(vehicles)
        run_metrics["vehicles_in_network_steady_samples"] += 1
    run_metrics["queue_samples"] += 1
    for key, value in queue_counts.items():
        run_metrics["queue_sum_by_approach"][key] += value
        run_metrics["queue_max_by_approach"][key] = max(
            run_metrics["queue_max_by_approach"][key], value
        )
    for node_x in canvas.INT_X:
        node_total = sum(
            queue_counts[f"node_{node_x}_{approach}"]
            for approach in ("EB", "WB", "NB", "SB")
        )
        key = str(node_x)
        run_metrics["node_max_queue_len"][key] = max(
            run_metrics["node_max_queue_len"][key], node_total
        )
        if signal_controller is not None:
            try:
                phase = signal_controller.nodes[node_x].phase
                previous = run_metrics["last_node_phases"].get(key)
                if previous in (0, 3) and phase in (1, 4):
                    served = ("EB", "WB") if previous == 0 else ("NB", "SB")
                    if any(queue_counts[f"node_{node_x}_{a}"] for a in served):
                        run_metrics["node_phase_failures"][key] += 1
                run_metrics["last_node_phases"][key] = phase
            except Exception:
                pass
    run_metrics["spillback_events"] += len(
        spillback_now - run_metrics["spillback_active_ids"]
    )
    run_metrics["spillback_active_ids"] = spillback_now


BASE_DIR = Path(__file__).resolve().parents[2]
assert (BASE_DIR / "requirements.txt").exists(), (
    f"BASE_DIR does not resolve to the repo root: {BASE_DIR}"
)
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
RESULTS_DIR = BASE_DIR / "results"
TELEMETRY_PATH = DATA_DIR / "traffic_state_telemetry.json"
AGENT_PATH = BASE_DIR / "src" / "agents" / "agent.py"
DECISION_PATH = DATA_DIR / "decision.json"
DECISION_STALE_MULTIPLIER = 3
DECISION_STALE_FLOOR_SEC = 12.0
AGENT_TURN_LOG_PATH = LOGS_DIR / "agent_turn_log.jsonl"
TELEMETRY_LOG_PATH = LOGS_DIR / "telemetry_log.jsonl"
# Samples every exported telemetry payload; its report is written beside
# each run workbook as <workbook>_gridlock.xlsx.
gridlock_monitor = GridlockMonitor()
BUS_EVENTS_LOG_PATH = LOGS_DIR / "bus_events.jsonl"
# One record per vehicle leaving the network (SUMO's tripinfo) and optional
# 1 Hz-style trajectories (SUMO's FCD), both reset per run.
TRIPINFO_LOG_PATH = LOGS_DIR / "tripinfo.jsonl"
FCD_LOG_PATH = LOGS_DIR / "fcd.csv"
EXCEL_EXPORT_DIR = RESULTS_DIR
# The master cross-run comparison dataset: one row per checkpoint of every
# run, appended forever. Never cleared by RESET -- only the operator deleting
# the file starts a new dataset. See append_experiment_summary_row.
EXPERIMENT_SUMMARY_PATH = RESULTS_DIR / "experiment_summary.csv"


def experiment_summary_path():
    """The dated dataset this run's rows go to: experiment_summary_<YYYYMMDD>.csv,
    stamped with the day its campaign started (a batch crossing midnight stays
    in one file; a single run is a campaign of one). One undated file across
    every day mixed regimes that could never pair -- rows from before and
    after a fix share a header but not a git_sha -- so each day is its own
    dataset and the paired-DV file sits beside it."""
    config = control_panel.global_config
    batch_runtime = config.get("batch_runtime") or {}
    stamp = batch_runtime.get("campaign_stamp") if batch_runtime.get("campaign_id") else None
    if not stamp:
        started = config.get("_run_started_wall")
        if not isinstance(started, (int, float)):
            started = time.time()
        stamp = time.strftime("%Y%m%d", time.localtime(started))
    return EXPERIMENT_SUMMARY_PATH.with_name(
        f"{EXPERIMENT_SUMMARY_PATH.stem}_{stamp}{EXPERIMENT_SUMMARY_PATH.suffix}"
    )
TELEMETRY_LOG_INTERVAL = 60
SESSION_ROUTE_IDS = (
    "R1_EB_A_NB",
    "R2_EB_B_NB",
    "R3_EB_ONLY",
    "R4_WB_A_SB",
    "R5_WB_B_SB",
    "R6_WB_ONLY",
)
_last_telemetry_log_frame = None
# Per-bus lifecycle records (spawn, waits, TSP treatment, completion). The
# path is resolved lazily so tests can re-point BUS_EVENTS_LOG_PATH.
bus_event_tracker = BusEventTracker(lambda: BUS_EVENTS_LOG_PATH)
_active_signal_controller = None


def apply_configured_random_seed():
    """Initialize the traffic RNG from the operator's current seed setting."""
    seed = control_panel.global_config.get("random_seed")
    if seed is not None:
        normalized_seed = int(seed)
        random.seed(normalized_seed)
        seed_demand_streams(normalized_seed)
        print(f"[SIM] Deterministic run, seed={normalized_seed}")
        return normalized_seed
    seed_demand_streams(None)
    print("[SIM] Nondeterministic run (no seed set)")
    return None


def apply_configured_vehicle_speed_scale():
    """Snapshot the pending speed scale for the next traffic episode."""
    try:
        scale = float(control_panel.global_config.get("vehicle_speed_scale", 0.5))
    except (TypeError, ValueError, OverflowError):
        scale = 0.5
    scale = max(0.25, min(1.0, scale))
    control_panel.global_config["vehicle_speed_scale"] = scale
    control_panel.global_config["_active_vehicle_speed_scale"] = scale
    return scale


def get_active_vehicle_speed_scale():
    """Return the speed scale frozen at the most recent START/reset."""
    try:
        return float(
            control_panel.global_config.get("_active_vehicle_speed_scale", 0.5)
        )
    except (TypeError, ValueError, OverflowError):
        return 0.5


def calculate_startup_window_layout(screen_width, screen_height):
    """Return screen-aware positions for control, canvas, and telemetry windows."""
    try:
        screen_width = max(1, int(screen_width))
        screen_height = max(1, int(screen_height))
    except (TypeError, ValueError, OverflowError):
        screen_width, screen_height = 1920, 1080

    margin = 10
    gap = 10
    canvas_top = 30
    taskbar_reserve = 40
    usable_bottom = screen_height - taskbar_reserve
    usable_height = usable_bottom - margin
    control_width = min(880, screen_width - 2 * margin)
    control_height = min(1030, usable_height)
    right_x = margin + control_width + gap
    right_width = screen_width - right_x - margin
    telemetry_y = canvas_top + canvas.HEIGHT + gap
    telemetry_height = usable_bottom - telemetry_y

    if right_width >= CANVAS_DISPLAY_WIDTH and telemetry_height >= 400:
        telemetry_width = min(max(900, CANVAS_DISPLAY_WIDTH), right_width)
        return {
            "mode": "tiled",
            "canvas_position": (right_x, canvas_top),
            "control_geometry": (
                f"{control_width}x{control_height}+{margin}+{margin}"
            ),
            "telemetry_geometry": (
                f"{telemetry_width}x{min(430, telemetry_height)}+"
                f"{right_x}+{telemetry_y}"
            ),
        }

    # Small desktops cannot contain all three full interfaces without overlap.
    # Keep every window wholly on-screen and use a predictable cascade instead.
    telemetry_width = min(900, screen_width - 2 * margin)
    telemetry_height = min(430, usable_height)
    canvas_x = max(margin, (screen_width - CANVAS_DISPLAY_WIDTH) // 2)
    control_x = max(margin, screen_width - control_width - margin)
    telemetry_x = max(margin, (screen_width - telemetry_width) // 2)
    return {
        "mode": "cascade",
        "canvas_position": (canvas_x, canvas_top),
        "control_geometry": (
            f"{control_width}x{control_height}+{control_x}+{margin}"
        ),
        "telemetry_geometry": (
            f"{telemetry_width}x{telemetry_height}+{telemetry_x}+30"
        ),
    }


def reset_session_logs():
    """Start a clean set of append-only logs for one simulator run."""
    global _last_telemetry_log_frame
    _last_telemetry_log_frame = None
    bus_event_tracker.reset()
    for path in (TELEMETRY_LOG_PATH, AGENT_TURN_LOG_PATH, BUS_EVENTS_LOG_PATH,
                 TRIPINFO_LOG_PATH, FCD_LOG_PATH):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"Session log reset warning for {path.name}: {exc}")


def log_telemetry_sample(frame_number, payload):
    """Append one schema-derived telemetry sample at roughly one-second gaps."""
    global _last_telemetry_log_frame
    try:
        frame_number = int(frame_number)
    except (TypeError, ValueError, OverflowError):
        return False
    if not isinstance(payload, dict) or frame_number < 0:
        return False
    if _last_telemetry_log_frame is None:
        if frame_number < TELEMETRY_LOG_INTERVAL:
            return False
    elif (
        frame_number <= _last_telemetry_log_frame
        or frame_number - _last_telemetry_log_frame < TELEMETRY_LOG_INTERVAL
    ):
        return False

    network_throughput_state = payload.get("network_throughput", {})
    network_summary_state = payload.get("network_summary", {})
    if not isinstance(network_throughput_state, dict):
        network_throughput_state = {}
    if not isinstance(network_summary_state, dict):
        network_summary_state = {}
    ai_runtime = control_panel.global_config.get("ai_runtime", {})
    if not isinstance(ai_runtime, dict):
        ai_runtime = {}
    record = {
        "frame": frame_number,
        "timestamp": payload.get("timestamp"),
        "sim_time_s": payload.get("simulation_time_seconds"),
        "passengers_served_total": network_throughput_state.get(
            "passengers_served_total"
        ),
        "passengers_served_bus": network_throughput_state.get(
            "passengers_served_bus"
        ),
        "passengers_served_car": network_throughput_state.get(
            "passengers_served_car"
        ),
        "buses_served": network_throughput_state.get("buses_served"),
        "cars_served": network_throughput_state.get("cars_served"),
        "pax_per_min_cumulative": network_throughput_state.get(
            "passengers_per_minute"
        ),
        "pax_per_min_recent": network_throughput_state.get(
            "passengers_per_minute_recent"
        ),
        "queues_vehicles": network_summary_state.get("queues"),
        "queues_passengers_est": network_summary_state.get(
            "queues_passengers_est"
        ),
        "vehicles_in_network": network_summary_state.get("total_vehicles"),
        "ai_armed": bool(ai_runtime.get("armed", False)),
        "ai_last_status": ai_runtime.get("last_status"),
        # Preserve the full observational state for baseline runs and for
        # recovering an audit row when an agent turn was interrupted. The
        # ordinary Telemetry worksheet remains a compact summary.
        "telemetry_snapshot": payload,
    }
    try:
        with TELEMETRY_LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(record) + "\n")
    except (OSError, TypeError, ValueError):
        return False
    _last_telemetry_log_frame = frame_number
    return True


def _read_jsonl_rows(path):
    """Read all complete JSON-object lines and ignore a damaged crash tail."""
    rows = []
    try:
        with Path(path).open("r", encoding="utf-8") as source:
            for line in source:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        pass
    return rows


DECISION_HEADER_BASE = [
    "turn",
    "timestamp",
    "model",
    "status",
    "reason",
    "pax_per_min_at_turn",
]

TELEMETRY_HEADER = [
    "frame",
    "sim_time_s",
    "passengers_served_total",
    "passengers_served_bus",
    "passengers_served_car",
    "buses_served",
    "cars_served",
    "pax_per_min_cumulative",
    "pax_per_min_recent",
    "vehicles_in_network",
    "ai_armed",
    "ai_last_status",
    "queues_vehicles",
    "queues_passengers_est",
]

# Mirrors the dashboard's sheet layout so a timed-test workbook and a manual
# EXPORT ALL workbook can be compared column for column.
LLM_PERFORMANCE_HEADERS = [
    "turn",
    "timestamp",
    "model",
    "status",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "tokens_per_sec",
    "tsp_on_count",
    "dbl_on_count",
    "reason",
    "pax_per_min_recent",
]

LLM_SUMMARY_HEADERS = [
    "model",
    "turns",
    "guard_ok_pct",
    "held_pct",
    "avg_latency_ms",
    "avg_tokens_per_sec",
    "total_output_tokens",
]

AI_DECISION_AUDIT_HEADERS = [
    "turn",
    "decision_timestamp",
    "telemetry_timestamp",
    "simulation_time_s",
    "frame_number",
    "model",
    "guard_status",
    "stale_observation",
    "model_reason",
    "raw_model_output",
    "observation_minimap",
    "action_flags_json",
    "requested_tsp_routes",
    "requested_dbl_routes",
    "observed_tsp_routes",
    "observed_dbl_routes",
    "pending_priority_routes",
    "dbl_queue_ahead_routes",
    "dbl_obstructed_routes",
    "locked_routes",
    "signal_states_json",
    "route_states_json",
    # Per-route load at the decision instant, flattened out of route_states
    # so a decision can be read against it without parsing the full blob.
    "route_vehicles_json",
    "route_passengers_json",
    "network_summary_json",
    "network_throughput_json",
    "demand_generation_json",
    "network_discharge_json",
    "active_buses_json",
    "bus_distribution_json",
    "vehicle_count",
    "vehicle_positions_json",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "tokens_per_sec",
    # Outcome: the state observed at the NEXT decision (or, for the final
    # decision, the nearest later telemetry sample) -- lets a reviewer judge
    # whether this decision's TSP/DBL grants actually helped, without waiting
    # on a live run to see what happened next.
    "outcome_available",
    "outcome_timestamp",
    "outcome_simulation_time_s",
    "outcome_queues_vehicles_json",
    "outcome_queues_passengers_json",
    "outcome_network_throughput_json",
    "outcome_observed_tsp_routes",
    "outcome_observed_dbl_routes",
    # Sim-time control delay, measured here rather than inferred from the
    # wall-clock latency: snapshot frame -> first merge frame.
    "applied_frame",
    "control_delay_sim_s",
]


def _excel_text(value, limit=32767):
    """Return Excel-safe text without allowing one cell to corrupt a save."""
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 15] + "...[truncated]"


def _compact_json(value):
    try:
        return _excel_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
    except (TypeError, ValueError):
        return _excel_text(value)


def _route_list(routes, predicate):
    if not isinstance(routes, dict):
        return ""
    return ",".join(
        sorted(
            str(route_id)
            for route_id, state in routes.items()
            if isinstance(state, dict) and predicate(state)
        )
    )


def _route_counts(routes, field):
    """{route_id: count} for one numeric per-route telemetry field.

    A bus route's "vehicles" are the buses running it, so this reads
    buses_on_route / route_passengers_total straight from the same routes
    block the rest of the row reports.
    """
    if not isinstance(routes, dict):
        return {}
    counts = {}
    for route_id, state in routes.items():
        if not isinstance(state, dict):
            continue
        try:
            counts[str(route_id)] = int(state.get(field) or 0)
        except (TypeError, ValueError):
            counts[str(route_id)] = 0
    return counts


def _decision_flag_routes(flags, flag_name):
    if not isinstance(flags, dict):
        return ""
    return ",".join(
        sorted(
            str(route_id)
            for route_id, route_flags in flags.items()
            if isinstance(route_flags, dict) and route_flags.get(flag_name) is True
        )
    )


def _telemetry_snapshot_from_row(row):
    if not isinstance(row, dict):
        return {}
    snapshot = row.get("telemetry_snapshot")
    return snapshot if isinstance(snapshot, dict) else row


def _nearest_telemetry_snapshot(decision, telemetry_rows):
    exact = decision.get("telemetry_snapshot") if isinstance(decision, dict) else None
    if isinstance(exact, dict) and exact:
        return exact
    candidates = [
        _telemetry_snapshot_from_row(row)
        for row in telemetry_rows or []
        if isinstance(row, dict)
    ]
    candidates = [row for row in candidates if row]
    if not candidates:
        return {}
    target = decision.get("timestamp") if isinstance(decision, dict) else None
    try:
        target = float(target)
    except (TypeError, ValueError, OverflowError):
        return candidates[-1]
    timed = []
    for row in candidates:
        try:
            timed.append((float(row.get("timestamp")), row))
        except (TypeError, ValueError, OverflowError):
            continue
    if not timed:
        return candidates[-1]
    preceding = [item for item in timed if item[0] <= target]
    return max(preceding, key=lambda item: item[0])[1] if preceding else min(
        timed, key=lambda item: abs(item[0] - target)
    )[1]


def _outcome_snapshot(decision, index, decisions, telemetry_rows):
    """Return the telemetry state that followed this decision, if any.

    The natural "what happened next" checkpoint is the state the following
    decision was made from (already logged, so no new instrumentation is
    needed); the final decision in a run instead uses the nearest telemetry
    sample logged strictly after it. Returns {} when nothing later exists.
    """
    if index + 1 < len(decisions):
        return _nearest_telemetry_snapshot(decisions[index + 1], telemetry_rows)
    try:
        target = float(decision.get("timestamp"))
    except (TypeError, ValueError, OverflowError):
        return {}
    later = []
    for row in telemetry_rows or []:
        if not isinstance(row, dict):
            continue
        snapshot = _telemetry_snapshot_from_row(row)
        try:
            timestamp = float(snapshot.get("timestamp"))
        except (TypeError, ValueError, OverflowError):
            continue
        if timestamp > target:
            later.append((timestamp, snapshot))
    return min(later, key=lambda item: item[0])[1] if later else {}


def _audit_row(decision, telemetry, outcome=None):
    decision = decision if isinstance(decision, dict) else {}
    telemetry = telemetry if isinstance(telemetry, dict) else {}
    outcome = outcome if isinstance(outcome, dict) else {}
    flags = decision.get("flags") if isinstance(decision.get("flags"), dict) else {}
    if decision.get("schema") == "signal_plan":
        # AI Configured: the plan is the action; the route columns stay empty.
        flags = {"plan": decision.get("plan")}
    routes = telemetry.get("routes") if isinstance(telemetry.get("routes"), dict) else {}
    signals = telemetry.get("signal_state")
    signal_nodes = signals.get("nodes", {}) if isinstance(signals, dict) else {}
    pending_routes = set()
    for node in signal_nodes.values() if isinstance(signal_nodes, dict) else ():
        if not isinstance(node, dict):
            continue
        active = node.get("active_request")
        if isinstance(active, dict) and active.get("route_id"):
            pending_routes.add(str(active["route_id"]))
        for request in node.get("queued_requests", []) or []:
            if isinstance(request, dict) and request.get("route_id"):
                pending_routes.add(str(request["route_id"]))
    pending_routes.update(
        route_id
        for route_id in _route_list(
            routes, lambda state: bool(state.get("nearest_bus_priority_pending"))
        ).split(",")
        if route_id
    )
    vehicles = telemetry.get("vehicle_positions")
    if not isinstance(vehicles, list):
        vehicles = []
    locked = decision.get("locked_routes", [])
    if not isinstance(locked, (list, tuple, set, frozenset)):
        locked = []
    outcome_summary = outcome.get("network_summary")
    if not isinstance(outcome_summary, dict):
        outcome_summary = {}
    outcome_routes = outcome.get("routes")
    if not isinstance(outcome_routes, dict):
        outcome_routes = {}
    return [
        decision.get("turn"),
        decision.get("timestamp"),
        telemetry.get("timestamp"),
        telemetry.get("simulation_time_seconds", telemetry.get("sim_time_s")),
        telemetry.get("frame_number", telemetry.get("frame")),
        decision.get("model", "None"),
        decision.get("status", "OBSERVATION_ONLY"),
        bool(decision.get("stale", False)),
        _excel_text(decision.get("reason", "")),
        _excel_text(decision.get("raw_output", "")),
        _excel_text(decision.get("minimap", "")),
        _compact_json(flags),
        _decision_flag_routes(flags, "tsp"),
        _decision_flag_routes(flags, "dbl"),
        _route_list(routes, lambda state: bool(state.get("tsp_enabled"))),
        _route_list(routes, lambda state: bool(state.get("dbl_enabled"))),
        ",".join(sorted(pending_routes)),
        _route_list(routes, lambda state: int(state.get("dbl_lane_queue_ahead") or 0) > 0),
        _route_list(routes, lambda state: bool(state.get("dbl_lane_obstructed"))),
        ",".join(sorted(str(route_id) for route_id in locked)),
        _compact_json(signals),
        _compact_json(routes),
        _compact_json(_route_counts(routes, "buses_on_route")),
        _compact_json(_route_counts(routes, "route_passengers_total")),
        _compact_json(telemetry.get("network_summary", {})),
        _compact_json(telemetry.get("network_throughput", {})),
        _compact_json(telemetry.get("demand_generation", {})),
        _compact_json(telemetry.get("network_discharge", {})),
        _compact_json(telemetry.get("active_buses", [])),
        _compact_json(telemetry.get("bus_distribution", {})),
        len(vehicles),
        _compact_json(vehicles),
        decision.get("latency_ms"),
        decision.get("input_tokens"),
        decision.get("output_tokens"),
        decision.get("tokens_per_sec"),
        bool(outcome),
        outcome.get("timestamp"),
        outcome.get("simulation_time_seconds", outcome.get("sim_time_s")),
        _compact_json(outcome_summary.get("queues", {})),
        _compact_json(outcome_summary.get("queues_passengers_est", {})),
        _compact_json(outcome.get("network_throughput", {})),
        _route_list(outcome_routes, lambda state: bool(state.get("tsp_enabled"))),
        _route_list(outcome_routes, lambda state: bool(state.get("dbl_enabled"))),
        *_applied_columns(decision.get("turn")),
    ]


def _applied_columns(turn):
    applied = run_metrics.get("decision_applied", {}).get(turn)
    if not applied:
        return [None, None]
    snapshot_frame, applied_frame = applied
    delay = (applied_frame - snapshot_frame) / 60.0 if snapshot_frame is not None else None
    return [applied_frame, round(delay, 3) if delay is not None else None]


def write_ai_decision_audit_sheet(
    sheet, decisions=None, telemetry_rows=None, samples=None, live_telemetry=None
):
    """Write one machine-readable row per AI turn or observed baseline frame.

    Agent turns use their exact telemetry snapshot. Legacy/incomplete turns
    fall back to the nearest logged telemetry frame; when no decisions exist,
    telemetry and dashboard samples still produce explicit observation-only
    rows instead of an empty or misleading AI trace.
    """
    decisions = [row for row in decisions or [] if isinstance(row, dict)]
    telemetry_rows = [row for row in telemetry_rows or [] if isinstance(row, dict)]
    samples = [row for row in samples or [] if isinstance(row, dict)]
    sheet.append(list(AI_DECISION_AUDIT_HEADERS))
    if decisions:
        for index, decision in enumerate(decisions):
            sheet.append(
                _audit_row(
                    decision,
                    _nearest_telemetry_snapshot(decision, telemetry_rows),
                    _outcome_snapshot(decision, index, decisions, telemetry_rows),
                )
            )
    elif telemetry_rows:
        snapshots = [_telemetry_snapshot_from_row(row) for row in telemetry_rows]
        for index, snapshot in enumerate(snapshots):
            outcome = snapshots[index + 1] if index + 1 < len(snapshots) else {}
            sheet.append(_audit_row({}, snapshot, outcome))
    elif isinstance(live_telemetry, dict) and live_telemetry:
        sheet.append(_audit_row({}, live_telemetry))
    else:
        for index, sample in enumerate(samples):
            outcome = samples[index + 1] if index + 1 < len(samples) else {}
            sheet.append(_audit_row(sample, {}, outcome))

    from openpyxl.styles import Alignment, Font, PatternFill

    header_fill = PatternFill("solid", fgColor="1F4E78")
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    compact_columns = {
        "A": 8, "B": 20, "C": 20, "D": 16, "E": 13, "F": 20,
        "G": 19, "H": 17, "AB": 14, "AD": 14, "AE": 14,
        "AF": 14, "AG": 14,
    }
    for letter, width in compact_columns.items():
        sheet.column_dimensions[letter].width = width
    for letter in ("I", "J", "K", "L", "T", "U", "V", "W", "X", "Y", "Z", "AA", "AC"):
        sheet.column_dimensions[letter].width = 42
    return sheet


def _write_decisions_sheet(sheet, decisions):
    header = list(DECISION_HEADER_BASE)
    for route_id in SESSION_ROUTE_IDS:
        header.extend((f"{route_id}_tsp", f"{route_id}_dbl"))
    header.extend(("locked_routes", "minimap", "raw_output"))
    sheet.append(header)
    for decision in decisions:
        flags = decision.get("flags", {})
        if not isinstance(flags, dict):
            flags = {}
        row = [
            decision.get("turn"),
            decision.get("timestamp"),
            decision.get("model"),
            decision.get("status"),
            decision.get("reason", ""),
            decision.get("pax_per_min_recent"),
        ]
        for route_id in SESSION_ROUTE_IDS:
            route_flags = flags.get(route_id, {})
            if not isinstance(route_flags, dict):
                route_flags = {}
            row.extend(
                (
                    bool(route_flags.get("tsp", False)),
                    bool(route_flags.get("dbl", False)),
                )
            )
        locked_routes = decision.get("locked_routes", [])
        if not isinstance(locked_routes, (list, tuple, set, frozenset)):
            locked_routes = []
        row.extend(
            (
                ",".join(str(route_id) for route_id in locked_routes),
                str(decision.get("minimap", ""))[:32767],
                str(decision.get("raw_output", ""))[:32767],
            )
        )
        sheet.append(row)


def _write_telemetry_sheet(sheet, telemetry_rows):
    sheet.append(list(TELEMETRY_HEADER))
    for telemetry in telemetry_rows:
        sheet.append(
            [
                telemetry.get("frame"),
                telemetry.get("sim_time_s"),
                telemetry.get("passengers_served_total"),
                telemetry.get("passengers_served_bus"),
                telemetry.get("passengers_served_car"),
                telemetry.get("buses_served"),
                telemetry.get("cars_served"),
                telemetry.get("pax_per_min_cumulative"),
                telemetry.get("pax_per_min_recent"),
                telemetry.get("vehicles_in_network"),
                telemetry.get("ai_armed"),
                telemetry.get("ai_last_status"),
                json.dumps(telemetry.get("queues_vehicles")),
                json.dumps(telemetry.get("queues_passengers_est")),
            ]
        )


def _flag_on_counts(decision):
    flags = decision.get("flags", {})
    if not isinstance(flags, dict):
        return 0, 0
    tsp_on = 0
    dbl_on = 0
    for route_flags in flags.values():
        if not isinstance(route_flags, dict):
            continue
        tsp_on += route_flags.get("tsp") is True
        dbl_on += route_flags.get("dbl") is True
    return tsp_on, dbl_on


def _numeric_or_none(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _write_llm_performance_sheet(sheet, decisions):
    """Per-turn model metrics taken straight from the agent turn log."""
    sheet.append(list(LLM_PERFORMANCE_HEADERS))
    for decision in decisions:
        tsp_on, dbl_on = _flag_on_counts(decision)
        sheet.append(
            [
                decision.get("turn"),
                decision.get("timestamp"),
                str(decision.get("model", "None")),
                str(decision.get("status", "UNKNOWN")),
                decision.get("latency_ms"),
                decision.get("input_tokens"),
                decision.get("output_tokens"),
                decision.get("tokens_per_sec"),
                tsp_on,
                dbl_on,
                str(decision.get("reason", "") or "")[:32767],
                decision.get("pax_per_min_recent"),
            ]
        )


def _average_of(values):
    numbers = [
        number
        for number in (_numeric_or_none(value) for value in values)
        if number is not None
    ]
    return round(sum(numbers) / len(numbers), 2) if numbers else None


def summarize_turn_log(decisions):
    """Aggregate guard outcomes and model throughput across logged turns."""
    rows = [row for row in decisions if isinstance(row, dict)]
    turns = len(rows)
    ok_turns = sum(row.get("status") == "OK" for row in rows)
    held_turns = sum(row.get("status") == "HELD_ALL_OFF" for row in rows)
    output_tokens = [
        number
        for number in (
            _numeric_or_none(row.get("output_tokens")) for row in rows
        )
        if number is not None
    ]
    models = [str(row.get("model", "None")) for row in rows]
    return {
        "model": max(set(models), key=models.count) if models else "None",
        "turns": turns,
        "guard_ok_pct": round(ok_turns / turns * 100.0, 2) if turns else 0.0,
        "held_pct": round(held_turns / turns * 100.0, 2) if turns else 0.0,
        "avg_latency_ms": _average_of(row.get("latency_ms") for row in rows),
        "avg_tokens_per_sec": _average_of(
            row.get("tokens_per_sec") for row in rows
        ),
        "total_output_tokens": sum(output_tokens) if output_tokens else 0.0,
    }


def _write_llm_summary_sheet(sheet, decisions):
    sheet.append(list(LLM_SUMMARY_HEADERS))
    rollup = summarize_turn_log(decisions)
    if rollup["turns"]:
        sheet.append(
            [
                rollup["model"],
                rollup["turns"],
                rollup["guard_ok_pct"],
                rollup["held_pct"],
                rollup["avg_latency_ms"],
                rollup["avg_tokens_per_sec"],
                rollup["total_output_tokens"],
            ]
        )


def export_session_excel(output_path=None):
    """Build the session summary workbook from the crash-safe session JSONL logs.

    Alongside Decisions/Telemetry, this always records the operator-configured
    input parameters (seed, speed scale, per-approach demand, per-route
    TSP/DBL/headway, derived Webster timing) so the summary is self-describing
    without the control panel still being open -- the same Control Panel
    Inputs sheet export_test_workbook and the dashboard's Export All write.
    """
    try:
        from openpyxl import Workbook
    except ImportError:
        print("openpyxl not installed; skipping Excel export")
        return None

    decisions = _read_jsonl_rows(AGENT_TURN_LOG_PATH)
    telemetry_rows = _read_jsonl_rows(TELEMETRY_LOG_PATH)
    if not decisions and not telemetry_rows:
        return None

    try:
        workbook = Workbook()
        decisions_sheet = workbook.active
        decisions_sheet.title = "Decisions"
        _write_decisions_sheet(decisions_sheet, decisions)
        _write_telemetry_sheet(
            workbook.create_sheet("Telemetry"), telemetry_rows
        )
        write_ai_decision_audit_sheet(
            workbook.create_sheet("AI Decision Audit"), decisions, telemetry_rows
        )
        write_control_panel_inputs_sheet(
            workbook.create_sheet("Control Panel Inputs")
        )

        destination = (
            Path(output_path)
            if output_path is not None
            else EXCEL_EXPORT_DIR
            / build_excel_export_filename(
                control_panel.global_config.get("ai_runtime", {}).get(
                    "model", "None"
                ),
                max(
                    float(
                        control_panel.global_config.get("sim_time_seconds")
                        or 0.0
                    ),
                    max(
                        (
                            float(row.get("sim_time_s") or 0.0)
                            for row in telemetry_rows
                            if isinstance(row, dict)
                        ),
                        default=0.0,
                    ),
                ),
                control_panel.global_config.get("random_seed"),
                control_mode=control_panel.global_config.get(
                    "ai_runtime", {}
                ).get("control_mode"),
            )
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(destination)
        workbook.close()
        gridlock_monitor.write_workbook(report_path_for(destination))
        print(f"Session exported: {destination}")
        return destination
    except Exception as exc:
        print(f"Session Excel export warning: {exc}")
        return None


CONTROL_INPUT_HEADERS = ["section", "parameter", "value"]


def control_panel_input_rows():
    """Flatten every operator-set input into section/parameter/value rows.

    Kept as a flat table so a new config key never breaks the sheet, and so a
    workbook records exactly which inputs produced it.
    """
    config = control_panel.global_config
    ai_runtime = config.get("ai_runtime", {}) or {}
    rows = [
        ("Global", "random_seed", config.get("random_seed")),
        ("Global", "sim_speed", config.get("sim_speed")),
        ("Global", "test_duration_sim_seconds",
         config.get("test_duration_sim_seconds")),
        ("Global", "warmup_discard_frames", warmup_discard_frames()),
        ("Global", "discharge_selection", config.get("discharge_selection")),
        ("Global", "llm_model", ai_runtime.get("model", "None")),
        ("Global", "llm_tick_seconds", ai_runtime.get("tick_seconds")),
        ("Global", "llm_armed", bool(ai_runtime.get("armed", False))),
        # Signal timing is derived output rather than an operator input. It is
        # recorded here so the exported run remains fully reproducible.
        ("Signal timing", "measured_saturation_flow_veh_per_hr",
         config.get("measured_saturation_flow")),
    ]
    for node_x, split in (config.get("webster_splits") or {}).items():
        section = f"Signal timing: node {node_x}"
        rows.extend(
            (
                (section, "EW_green_sec", split.get("EW_green_sec")),
                (section, "NS_green_sec", split.get("NS_green_sec")),
                (section, "EW_green_frames", split.get("EW_green_frames")),
                (section, "NS_green_frames", split.get("NS_green_frames")),
                (section, "cycle_time_sec", split.get("cycle_time_sec")),
                (section, "cycle_time_frames", split.get("cycle_time_frames")),
                (section, "lost_time_sec", split.get("lost_time_sec")),
                (section, "cycle_source", split.get("cycle_source")),
                (section, "min_cycle_sec", split.get("min_cycle_sec")),
                (section, "EW_critical_lane_flow_veh_hr",
                 split.get("EW_critical_lane_flow_veh_hr")),
                (section, "NS_critical_lane_flow_veh_hr",
                 split.get("NS_critical_lane_flow_veh_hr")),
                (section, "y_ew", split.get("y_ew")),
                (section, "y_ns", split.get("y_ns")),
                (section, "Y", split.get("Y")),
                (section, "oversaturated", split.get("oversaturated")),
                (section, "webster_optimal_cycle_sec",
                 split.get("webster_optimal_cycle_sec")),
            )
        )
    for key, name in control_panel.APPROACH_NAMES.items():
        approach = control_panel.approach_configs.get(key, {})
        section = f"Approach: {name}"
        rows.extend(
            (
                (section, "active", bool(approach.get("active", False))),
                (section, "generation_model", approach.get("model")),
                (section, "inflow_rate_veh_per_min", approach.get("rate")),
                (section, "turn_split", approach.get("turn_split")),
                (section, "left_far_share", approach.get("left_far_share", 0.0)),
                (section, "heavy_ratio", approach.get("heavy_ratio")),
            )
        )
    for route_id, route in control_panel.bus_routes_config.items():
        section = f"Route: {route_id}"
        rows.extend(
            (
                (section, "name", route.get("name")),
                (section, "active", bool(route.get("active", False))),
                (section, "headway_sec", route.get("headway_sec")),
                (section, "tsp_enabled", bool(route.get("tsp_enabled", False))),
                (section, "dbl_enabled", bool(route.get("dbl_enabled", False))),
                (section, "manual_dispatch",
                 bool(route.get("manual_dispatch", False))),
            )
        )
    return rows


def write_control_panel_inputs_sheet(sheet):
    """Write the operator-input table onto an open worksheet, and beside it
    the same table as it stood at run start (snapshot taken by
    perform_full_reset). In an assisted run the per-route tsp/dbl flags on
    this end-of-run sheet are the decider's last decision, not operator
    configuration; the start sheet is the run's verifiable setup."""
    sheet.append(list(CONTROL_INPUT_HEADERS))
    for row in control_panel_input_rows():
        sheet.append(list(row))
    if getattr(sheet, "parent", None) is not None:
        start_sheet = sheet.parent.create_sheet("Control Panel Inputs (start)")
        start_sheet.append(list(CONTROL_INPUT_HEADERS))
        for row in control_panel.global_config.get("_initial_input_rows") or []:
            start_sheet.append(list(row))
    return sheet


def write_unit_conversions_sheet(sheet, telemetry=None):
    """Record how this run's pixel/frame data maps to real-world units.

    The anchor constants and derived scales go in every workbook so each
    figure can cite its conversion basis. Telemetry defaults to the last
    exported snapshot on disk, so the live delay/speed rows are filled too.
    """
    if telemetry is None:
        try:
            telemetry = json.loads(TELEMETRY_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            telemetry = None
    return real_world_units.write_unit_conversions_sheet(
        sheet,
        control_panel.global_config,
        control_panel.approach_configs,
        control_panel.APPROACH_NAMES,
        telemetry,
    )


# Bumped whenever a column is added/removed/redefined. append_experiment_summary_row
# rotates the CSV (never appends ragged) when either this or config_hash changes.
EXPERIMENT_SUMMARY_SCHEMA_VERSION = 9

# PRIMARY DV: total_person_hours_delay_incl_entry_steady, compared as a
# paired per-seed difference against the baseline arm (pair_against_baseline
# -> paired_dv_<campaign>.csv: net_person_hours_saved = baseline - arm). It is
# passenger-weighted time below each vehicle's own free-flow speed after the
# warm-up discard, PLUS the time offered demand waited at the network
# boundary for the entry to clear -- without that term a controller that
# holds traffic outside the model is credited for the delay it exported
# (FHWA Traffic Analysis Toolbox Vol. III: account for demand unable to
# enter). The on-road-only figure stays as
# total_person_hours_travel_delay_steady. Turn/merge slow-downs count as
# delay in every arm alike and cancel in the pairing.
# The stopped-delay triple (speed < 0.25) is the robustness measure. No
# per-run column can be "net saved": the baseline is another run.

EXPERIMENT_SUMMARY_HEADERS = (
    # Identity
    "timestamp", "model", "seed", "checkpoint_min", "test_duration_min",
    # Run identity/provenance (section A): what this row was produced by.
    "run_uuid", "campaign_id", "git_sha", "git_dirty", "config_hash",
    "run_started_utc", "run_ended_utc", "wall_clock_sec", "workbook_filename",
    "schema_version",
    # Regime
    "vehicle_speed_scale", "saturation_flow_veh_hr", "cycle_sec", "vc_ratio",
    "demand_EB_veh_min", "demand_WB_veh_min", "demand_A_NB_veh_min",
    "demand_A_SB_veh_min", "demand_B_NB_veh_min", "demand_B_SB_veh_min",
    "demand_symmetric",
    # Configuration echo (section B): frozen inputs, so cross-run comparison
    # never requires opening a workbook's Control Panel Inputs sheet.
    "decision_interval_sec", "sim_speed", "random_seed", "llm_model", "llm_armed",
    "movement_model", "control_mode",
    "test_duration_sim_sec",
    "priority_eligibility_px", "link_length_px", "tsp_max_adjust_fraction",
    "min_green_frames", "TSP_MAX_ADJUST_FRACTION", "MIN_GREEN_FRAMES",
    "yellow_time_frames", "red_clearance_time_frames",
    "lost_time_sec", "min_cycle_sec", "discharge_selection",
    "measured_saturation_flow_veh_per_hr", "bus_occupancy_pax", "demand_model",
    "demand_draw_hash", "export_frame", "sim_seconds_per_wall_second",
    "sampling_params_json",
    # Throughput
    "passengers_served_total", "passengers_served_bus", "passengers_served_car",
    "buses_served", "cars_served", "pax_per_min",
    # Delay (the headline)
    "total_person_hours_delay", "bus_person_hours_delay", "car_person_hours_delay",
    "total_person_hours_travel_delay", "bus_person_hours_travel_delay",
    "car_person_hours_travel_delay",
    "mean_bus_passenger_delay_sec", "mean_car_passenger_delay_sec",
    # Completed-cohort per-vehicle delay (the LOS input) and the censored
    # exposure of vehicles still in the network at the checkpoint, reported
    # apart so an early checkpoint cannot inflate a mean.
    "mean_stopped_delay_sec_per_vehicle", "mean_control_delay_sec_per_vehicle",
    "passenger_hours_in_network", "unfinished_stopped_person_hours",
    # Congestion
    "mean_vehicles_in_network", "max_vehicles_in_network",
    # Mechanism (the causal proof, from Bus Events)
    "buses_tsp_treated", "buses_untreated",
    "mean_treated_bus_wait_sec", "mean_untreated_bus_wait_sec",
    # LLM meta
    "mean_decision_latency_ms", "guard_reject_rate", "total_decisions",
    "decision_lag_sec_used",
    # Measured on the sim clock: snapshot frame -> frame the decision took
    # effect (see _record_decision_applied).
    "decisions_applied", "control_delay_sim_sec_median", "control_delay_sim_sec_p95",
    # Decision exposure (section C): the fixed-schedule confound fix -- offered
    # opportunities vs. issued decisions, so decision frequency stops being a
    # free variable correlated with model speed.
    "decision_opportunities", "decisions_issued", "decisions_skipped_slow",
    "actual_decision_interval_sec_mean", "actual_decision_interval_sec_median",
    "decisions_effective", "utilisation_rate", "is_noop",
    # Realised demand and service (section D).
    "offered_veh", "offered_pax", "served_veh", "served_pax",
    "served_fraction_veh", "served_fraction_pax",
    "vehicles_still_in_network_at_end", "buses_spawned", "buses_completed",
    "bus_completion_rate", "bus_trips_scheduled", "bus_trips_pending",
    "bus_trips_missed", "bus_source_delay_sec",
    # LOS
    "level_of_service",
    # Steady-state DVs: the same quantities over the post-warm-up window only.
    # The cumulative columns above are kept untouched so old rows stay readable.
    "warmup_discard_sec", "steady_window_sec", "converged", "time_to_converge_sec",
    "passengers_served_total_at_warmup", "passengers_served_bus_at_warmup",
    "passengers_served_car_at_warmup",
    "pax_per_min_steady",
    "pax_per_min_cumulative",
    "total_person_hours_delay_steady", "bus_person_hours_delay_steady",
    "car_person_hours_delay_steady",
    "total_person_hours_travel_delay_steady", "bus_person_hours_travel_delay_steady",
    "car_person_hours_travel_delay_steady",
    # PRIMARY DV since schema 9: on-road travel delay plus the wait of demand
    # held at the network boundary (latent demand).
    "total_person_hours_delay_incl_entry_steady",
    "bus_person_hours_delay_incl_entry_steady", "car_person_hours_delay_incl_entry_steady",
    "person_hours_entry_wait_steady", "vehicles_waiting_entry_at_end",
    # Capacity validation: in-network HCM saturation flow vs the calibrated
    # S, and GEH of entered vs offered volume per source.
    "network_saturation_flow_veh_hr_lane", "network_saturation_headway_samples",
    "network_to_calibrated_s_ratio", "geh_entry_max", "geh_entry_share_below_5",
    # Signal timing actually used (ITE intervals, HCM lost time, coordination).
    "signal_timing_json",
    "latent_demand_share_at_end", "mean_entry_delay_sec_per_vehicle",
    # Surrogate safety (FHWA SSAM TTC threshold) and the braking-bound record.
    "ssm_ttc_conflicts_per_veh_hr_steady", "ssm_emergency_decel_per_veh_hr_steady",
    "ssm_clamp_events_per_veh_hr_steady", "ssm_ttc_conflicts_total",
    "ssm_leader_clamp_frames_total", "ssm_stop_line_clamp_frames_total", "missed_turns_total",
    "mean_bus_passenger_delay_sec_steady", "mean_car_passenger_delay_sec_steady",
    "delay_sec_per_pax_served_steady",
    "bus_passenger_delay_sec_p50", "bus_passenger_delay_sec_p90",
    "car_passenger_delay_sec_p50", "car_passenger_delay_sec_p90",
    "bus_passenger_delay_sec_p50_steady", "bus_passenger_delay_sec_p90_steady",
    "car_passenger_delay_sec_p50_steady", "car_passenger_delay_sec_p90_steady",
    # Priority mechanism exposure (section G).
    "tsp_requests_raised", "tsp_requests_granted", "tsp_requests_denied",
    "tsp_grant_rate", "tsp_denial_reasons_json", "tsp_actions_early_green",
    "tsp_actions_extending", "tsp_total_adjust_frames",
    "dbl_requests_raised", "dbl_requests_granted", "dbl_requests_denied",
    "dbl_grant_rate", "dbl_denial_reasons_json", "dbl_actions_activated",
    "dbl_total_active_frames",
    # Cross-street cost/equity (section H). Descriptive only: counted while a
    # TSP action is active, so it is 0 for the baseline by construction. The
    # network's real cross-street cost is inside car_person_hours_*_delay.
    "cross_street_delay_sec_mean", "cross_street_pax_delayed",
    "tsp_window_cross_street_person_hours",
    "p90_over_p50_pax_delay",
    # Network stability (section I).
    "discharge_activations", "discharge_total_frames",
    "discharge_recovery_failed", "spillback_events",
    "max_queue_len_by_approach_json", "mean_queue_len_by_approach_json",
    "mean_vehicles_in_network_steady", "network_cleared_at_end",
    # Reliability and operating cost (section J).
    "guard_ok_count", "guard_held_all_off_count",
    "guard_observation_only_count", "guard_malformed_count",
    "stale_observation_count", "latency_ms_p50", "latency_ms_p95",
    "latency_ms_max", "latency_to_cycle_ratio_p50",
    "latency_to_cycle_ratio_p95", "input_tokens_total",
    "output_tokens_total", "tokens_per_sec_mean", "cost_usd_estimate",
    # Independent-controller breakdown (section K): node_A_* / node_B_*,
    # named by letter so the header survives a geometry change.
    *(
        f"node_{name}_{metric}"
        for name in ("A", "B")
        for metric in (
            "mean_delay_sec", "throughput_veh", "tsp_grants", "cycle_sec_mean",
            "phase_failures", "max_queue_len",
        )
    ),
    # Data-quality flags (section L): computed by the exporter, not the analyst.
    "qa_null_columns_json", "qa_baseline_contaminated", "qa_unbalanced_seed",
    "qa_duration_deviation_sec", "qa_flags_count",
)


def _node_breakdown_columns(config, controller_metrics):
    """Section K per node: Node A = INT_X[0], Node B = INT_X[1]."""
    out = {}
    cycles = config.get("cycle_time_sec", {}) or {}
    for name, node_x in zip(("A", "B"), canvas.INT_X):
        key = str(node_x)
        served = run_metrics["node_throughput_veh"][key]
        out[f"node_{name}_mean_delay_sec"] = (
            round(run_metrics["node_completed_stopped_frames"][key] / 60.0 / served, 3)
            if served else None
        )
        out[f"node_{name}_throughput_veh"] = served
        out[f"node_{name}_tsp_grants"] = controller_metrics["node_tsp_grants"].get(key, 0)
        out[f"node_{name}_cycle_sec_mean"] = cycles.get(node_x) or cycles.get(key)
        out[f"node_{name}_phase_failures"] = run_metrics["node_phase_failures"][key]
        out[f"node_{name}_max_queue_len"] = run_metrics["node_max_queue_len"][key]
    return out


def _time_to_converge_sec(telemetry_rows, checkpoint_seconds):
    """Earliest sim-time X such that pax_per_min_cumulative stays within 5%
    of its final value for every sample from X through the end -- measured,
    not guessed, so warmup_discard_frames can be set from data instead of a
    single traced estimate."""
    rows = sorted(
        (
            row for row in telemetry_rows
            if isinstance(row, dict)
            and isinstance(row.get("sim_time_s"), (int, float))
            and isinstance(row.get("pax_per_min_cumulative"), (int, float))
            and row["sim_time_s"] <= checkpoint_seconds
        ),
        key=lambda row: row["sim_time_s"],
    )
    if not rows:
        return None
    final_value = rows[-1]["pax_per_min_cumulative"]
    if not final_value:
        return None
    violation_index = None
    for index in range(len(rows) - 1, -1, -1):
        if abs(rows[index]["pax_per_min_cumulative"] - final_value) / final_value >= 0.05:
            violation_index = index
            break
    converge_at = 0 if violation_index is None else violation_index + 1
    if converge_at >= len(rows):
        return None
    return round(rows[converge_at]["sim_time_s"], 1)


def _record_stop_bar_crossing(vehicle, node_x, signal_controller):
    """Log one front-bumper stop-bar crossing for the network S check."""
    if signal_controller is None or getattr(vehicle, "target_turn", "STRAIGHT") != "STRAIGHT":
        return
    node = getattr(signal_controller, "nodes", {}).get(node_x)
    phase = 0 if vehicle.direction in ("EB", "WB") else 3
    if node is None or node.phase != phase:
        return  # not on its own green (a yellow go, a discharge stage)
    frame = int(getattr(signal_controller, "frame_number", 0))
    green_start = frame - int(node.timer)
    key = (node_x, vehicle.direction, vehicle.lane_index)
    last = _discharge_state["last"].get(key)
    if last is None or last[1] != green_start:
        _discharge_state["last"][key] = (frame, green_start, 1)
        return
    position = last[2] + 1
    _discharge_state["last"][key] = (frame, green_start, position)
    s_cal = float(control_panel.global_config.get("measured_saturation_flow") or 0)
    if position >= 5 and s_cal > 0:
        headway = (frame - last[0]) / 60.0
        if 0 < headway <= CALIBRATION_INTERRUPTION_FACTOR * 3600.0 / s_cal:
            _discharge_state["headways"].append(headway)


def _geh(model, count):
    """GEH statistic for hourly flows (FHWA Traffic Analysis Toolbox Vol. III)."""
    if model + count <= 0:
        return 0.0
    return math.sqrt(2.0 * (model - count) ** 2 / (model + count))


def _network_validation_columns(checkpoint_seconds):
    headways = _discharge_state["headways"]
    s_cal = float(control_panel.global_config.get("measured_saturation_flow") or 0)
    s_net = (
        3600.0 / (sum(headways) / len(headways))
        if len(headways) >= NETWORK_S_MIN_SAMPLES else None
    )
    hours = max(checkpoint_seconds, 1e-9) / 3600.0
    gehs = []
    for state in spawner_states.values():
        offered = int(state.get("requested_arrivals", 0) or 0)
        if offered:
            gehs.append(_geh(int(state.get("admitted_arrivals", 0) or 0) / hours, offered / hours))
    return {
        "network_saturation_flow_veh_hr_lane": round(s_net, 1) if s_net else None,
        "network_saturation_headway_samples": len(headways),
        "network_to_calibrated_s_ratio": round(s_net / s_cal, 3) if s_net and s_cal else None,
        "geh_entry_max": round(max(gehs), 2) if gehs else None,
        "geh_entry_share_below_5": (
            round(sum(1 for g in gehs if g < 5.0) / len(gehs), 3) if gehs else None
        ),
    }


TRIPINFO_FIELDS = (
    "vehicle", "type", "route_id", "origin", "credited", "unfinished",
    "depart_s", "arrival_s", "duration_s", "time_loss_s", "waiting_time_s",
    "depart_delay_s", "dwell_s", "passengers", "nodes_passed",
)


def tripinfo_record(vehicle, frame_number, finished):
    """One vehicle's trip, as SUMO's tripinfo reports it: departure (entry to
    the network), arrival, duration, time lost below its own free-flow speed
    (timeLoss), time standing (waitingTime) and the wait before it could
    enter at all (departDelay). ``credited`` is the throughput rule --
    left the network having cleared at least one node's conflict box."""
    depart = getattr(vehicle, "depart_frame", None)
    passed = sorted(getattr(vehicle, "passed_nodes", ()) or ())
    kind = "bus" if isinstance(vehicle, Bus) else ("truck" if vehicle.is_heavy else "car")
    return {
        "vehicle": getattr(vehicle, "serial", None),
        "type": kind,
        "route_id": getattr(vehicle, "route_id", "") or "",
        "origin": getattr(vehicle, "spawn_direction", vehicle.direction),
        "credited": bool(passed) and finished,
        "unfinished": not finished,
        "depart_s": round(depart / 60.0, 2) if depart is not None else None,
        "arrival_s": round(frame_number / 60.0, 2) if finished else None,
        "duration_s": round((frame_number - depart) / 60.0, 2) if depart is not None else None,
        "time_loss_s": round(float(getattr(vehicle, "_experiment_travel_delay_frames", 0.0)) / 60.0, 2),
        "waiting_time_s": round(int(getattr(vehicle, "_experiment_stopped_frames", 0)) / 60.0, 2),
        "depart_delay_s": round(int(getattr(vehicle, "entry_delay_frames", 0) or 0) / 60.0, 2),
        "dwell_s": round(int(getattr(vehicle, "dwell_frames_total", 0) or 0) / 60.0, 2),
        "passengers": int(getattr(vehicle, "passengers", 0) or 0),
        "nodes_passed": len(passed),
    }


def _write_tripinfo(record):
    try:
        TRIPINFO_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with TRIPINFO_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError as exc:
        print(f"Trip info log warning: {exc}")


FCD_FIELDS = ("time_s", "vehicle", "type", "x_m", "y_m", "speed_mps", "direction", "lane")


def _sample_fcd(vehicles, frame_number):
    """Every ``fcd_period_s`` of sim time, every vehicle's position (m) and
    speed (m/s) -- SUMO's floating-car-data output. Off (0) by default; a
    60-minute run at 1 s is ~800k rows."""
    period = float(control_panel.global_config.get("fcd_period_s", 0) or 0)
    if period <= 0 or frame_number % max(1, int(round(period * 60))):
        return
    m = vehicle_module.METERS_PER_PX
    mps = m * vehicle_module.FPS
    try:
        FCD_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        new = not FCD_LOG_PATH.exists()
        with FCD_LOG_PATH.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if new:
                writer.writerow(FCD_FIELDS)
            t = round(frame_number / 60.0, 2)
            for v in vehicles:
                kind = "bus" if isinstance(v, Bus) else ("truck" if v.is_heavy else "car")
                writer.writerow((t, v.serial, kind, round(v.x * m, 2), round(v.y * m, 2),
                                 round(v.speed * mps, 3), v.direction, v.lane_index))
    except OSError as exc:
        print(f"FCD log warning: {exc}")


def write_tripinfo_sheet(sheet, frame_number=None):
    """Finished trips from the log, then every vehicle still on the road as
    an unfinished trip (SUMO's --tripinfo-output.write-unfinished)."""
    sheet.append(list(TRIPINFO_FIELDS))
    for record in _read_jsonl_rows(TRIPINFO_LOG_PATH):
        sheet.append([record.get(key) for key in TRIPINFO_FIELDS])
    frame = _current_frame["n"] if frame_number is None else frame_number
    for vehicle in list(_live_vehicles or []):
        record = tripinfo_record(vehicle, frame, finished=False)
        sheet.append([record.get(key) for key in TRIPINFO_FIELDS])


def _latent_demand_and_safety_columns():
    """End-of-window latent demand and the run's safety totals."""
    waiting = sum(int(st.get("pending_arrivals", 0) or 0) for st in spawner_states.values())
    waiting += sum(len(trips) for trips in bus_pending_trips.values())
    offered = sum(int(st.get("requested_arrivals", 0) or 0) for st in spawner_states.values())
    offered += int(run_metrics.get("bus_trips_scheduled", 0) or 0)
    served = int(network_throughput.get("vehicles_served_total", 0) or 0)
    return {
        "vehicles_waiting_entry_at_end": waiting,
        "latent_demand_share_at_end": round(waiting / offered, 4) if offered else None,
        "mean_entry_delay_sec_per_vehicle": (
            round(network_throughput["completed_entry_delay_frames"] / 60.0 / served, 2)
            if served else None
        ),
        "ssm_ttc_conflicts_total": int(network_throughput.get("ssm_ttc_conflicts", 0)),
        "ssm_leader_clamp_frames_total": int(network_throughput.get("ssm_leader_clamp_frames", 0)),
        "ssm_stop_line_clamp_frames_total": int(network_throughput.get("ssm_stop_line_clamp_frames", 0)),
        "missed_turns_total": int(network_throughput.get("ssm_missed_turns", 0)),
    }


def _steady_state_metrics(checkpoint_seconds, telemetry_rows):
    """Post-warm-up DVs from the warm-up snapshot, plus a convergence flag:
    cumulative pax/min within 5% between 0.8T and T *and* vehicles in the
    network within 10% over the same span. A cumulative mean is stable by
    construction, so alone it flagged runs whose accumulation was still
    climbing monotonically at the horizon (Run 8: 5 of 22 "converged")."""
    warm = network_throughput_at_warmup
    warmup_sec = warmup_discard_frames() / 60.0
    steady_sec = checkpoint_seconds - warmup_sec
    out = {
        "warmup_discard_sec": round(warmup_sec, 1),
        "steady_window_sec": round(max(0.0, steady_sec), 1),
        "converged": None,
        "time_to_converge_sec": _time_to_converge_sec(telemetry_rows, checkpoint_seconds),
        "passengers_served_total_at_warmup": (
            int(warm.get("passengers_served_total", 0) or 0) if warm else None
        ),
        "passengers_served_bus_at_warmup": (
            int(warm.get("passengers_served_bus", 0) or 0) if warm else None
        ),
        "passengers_served_car_at_warmup": (
            int(warm.get("passengers_served_car", 0) or 0) if warm else None
        ),
        "pax_per_min_steady": None,
        "total_person_hours_delay_steady": None,
        "bus_person_hours_delay_steady": None,
        "car_person_hours_delay_steady": None,
        "total_person_hours_travel_delay_steady": None,
        "bus_person_hours_travel_delay_steady": None,
        "car_person_hours_travel_delay_steady": None,
        "person_hours_entry_wait_steady": None,
        "total_person_hours_delay_incl_entry_steady": None,
        "bus_person_hours_delay_incl_entry_steady": None,
        "car_person_hours_delay_incl_entry_steady": None,
        "ssm_ttc_conflicts_per_veh_hr_steady": None,
        "ssm_emergency_decel_per_veh_hr_steady": None,
        "ssm_clamp_events_per_veh_hr_steady": None,
        "mean_bus_passenger_delay_sec_steady": None,
        "mean_car_passenger_delay_sec_steady": None,
        "delay_sec_per_pax_served_steady": None,
    }
    if warm and steady_sec > 0:
        def delta(key):
            return float(network_throughput.get(key, 0) or 0) - float(warm.get(key, 0) or 0)
        bus_pax = int(delta("passengers_served_bus"))
        car_pax = int(delta("passengers_served_car"))
        bus_delay_sec = delta("bus_passenger_delay_frames") / 60.0
        car_delay_sec = delta("car_passenger_delay_frames") / 60.0
        bus_done_sec = delta("completed_bus_passenger_delay_frames") / 60.0
        car_done_sec = delta("completed_car_passenger_delay_frames") / 60.0
        bus_travel_h = delta("bus_passenger_travel_delay_frames") / 60.0 / 3600.0
        car_travel_h = delta("car_passenger_travel_delay_frames") / 60.0 / 3600.0
        bus_entry_h = delta("bus_passenger_entry_wait_frames") / 60.0 / 3600.0
        car_entry_h = delta("car_passenger_entry_wait_frames") / 60.0 / 3600.0
        veh_h = delta("vehicles_in_network_frame_sum") / 60.0 / 3600.0

        def per_veh_hr(count):
            return round(count / veh_h, 3) if veh_h > 0 else None

        out.update({
            "person_hours_entry_wait_steady": round(bus_entry_h + car_entry_h, 4),
            "total_person_hours_delay_incl_entry_steady": round(
                bus_travel_h + car_travel_h + bus_entry_h + car_entry_h, 4
            ),
            "bus_person_hours_delay_incl_entry_steady": round(bus_travel_h + bus_entry_h, 4),
            "car_person_hours_delay_incl_entry_steady": round(car_travel_h + car_entry_h, 4),
            "ssm_ttc_conflicts_per_veh_hr_steady": per_veh_hr(delta("ssm_ttc_conflicts")),
            "ssm_emergency_decel_per_veh_hr_steady": per_veh_hr(delta("ssm_emergency_decel_events")),
            "ssm_clamp_events_per_veh_hr_steady": per_veh_hr(
                delta("ssm_leader_clamp_frames") + delta("ssm_stop_line_clamp_frames")
            ),
        })
        out.update({
            "pax_per_min_steady": round(
                delta("passengers_served_total") / (steady_sec / 60.0), 2
            ),
            "total_person_hours_delay_steady": round(
                (bus_delay_sec + car_delay_sec) / 3600.0, 4
            ),
            "bus_person_hours_delay_steady": round(bus_delay_sec / 3600.0, 4),
            "car_person_hours_delay_steady": round(car_delay_sec / 3600.0, 4),
            "total_person_hours_travel_delay_steady": round(
                round(bus_travel_h, 4) + round(car_travel_h, 4), 4
            ),
            "bus_person_hours_travel_delay_steady": round(bus_travel_h, 4),
            "car_person_hours_travel_delay_steady": round(car_travel_h, 4),
            "mean_bus_passenger_delay_sec_steady": (
                round(bus_done_sec / bus_pax, 2) if bus_pax else None
            ),
            "mean_car_passenger_delay_sec_steady": (
                round(car_done_sec / car_pax, 2) if car_pax else None
            ),
            # Travel delay per passenger actually served in the window: raw
            # person-hours reward serving fewer people, and served_fraction
            # moves with the offered demand, which differs between arms.
            "delay_sec_per_pax_served_steady": (
                round((bus_travel_h + car_travel_h) * 3600.0 / (bus_pax + car_pax), 2)
                if (bus_pax + car_pax) else None
            ),
        })
    served_total = int(network_throughput.get("passengers_served_total", 0) or 0)
    ppm_now = served_total / max(checkpoint_seconds / 60.0, 1e-9)
    earlier = [
        row for row in telemetry_rows
        if isinstance(row, dict)
        and isinstance(row.get("sim_time_s"), (int, float))
        and row["sim_time_s"] <= 0.8 * checkpoint_seconds
        and isinstance(row.get("pax_per_min_cumulative"), (int, float))
    ]
    if earlier and ppm_now > 0:
        then = max(earlier, key=lambda row: row["sim_time_s"])
        ppm_stable = abs(ppm_now - then["pax_per_min_cumulative"]) / ppm_now < 0.05
        latest = [
            row for row in telemetry_rows
            if isinstance(row, dict)
            and isinstance(row.get("sim_time_s"), (int, float))
            and row["sim_time_s"] <= checkpoint_seconds
            and isinstance(row.get("vehicles_in_network"), (int, float))
        ]
        load_stable = True
        if latest and isinstance(then.get("vehicles_in_network"), (int, float)):
            n_now = max(latest, key=lambda row: row["sim_time_s"])["vehicles_in_network"]
            n_then = then["vehicles_in_network"]
            load_stable = abs(n_now - n_then) <= 0.10 * max(n_now, n_then, 1)
        out["converged"] = ppm_stable and load_stable
    return out


def _approach_demand_rates():
    """Configured veh/min per approach, or 0 for an inactive approach."""
    rates = {}
    for key in ("EB", "WB", "A_NB", "A_SB", "B_NB", "B_SB"):
        cfg = control_panel.approach_configs.get(key) or {}
        rates[key] = float(cfg.get("rate") or 0.0) if cfg.get("active", False) else 0.0
    return rates


def _demand_is_symmetric(rates):
    """True when each corridor's two directions carry the same configured
    demand: EB/WB, and each node's own NB/SB pair."""
    pairs = (("EB", "WB"), ("A_NB", "A_SB"), ("B_NB", "B_SB"))
    return all(abs(rates.get(a, 0.0) - rates.get(b, 0.0)) < 1e-9 for a, b in pairs)


def _bus_event_mechanism_summary(bus_events):
    """Treated-vs-untreated bus counts and mean total wait, from Bus Events.

    A bus counts as treated when any node in its lifecycle recorded a real
    TSP action (extension or early green); this is the same tsp_treated_any
    flag the Bus Events sheet exposes.
    """
    treated_wait_sec = []
    untreated_wait_sec = []
    for row in bus_events:
        if not isinstance(row, dict):
            continue
        nodes = row.get("nodes")
        if not isinstance(nodes, list):
            nodes = []
        treated = any(
            isinstance(node, dict) and node.get("tsp_treated")
            for node in nodes
        )
        wait_sec = float(row.get("total_wait_frames", 0) or 0) / 60.0
        (treated_wait_sec if treated else untreated_wait_sec).append(wait_sec)
    return {
        "buses_tsp_treated": len(treated_wait_sec),
        "buses_untreated": len(untreated_wait_sec),
        "mean_treated_bus_wait_sec": (
            round(sum(treated_wait_sec) / len(treated_wait_sec), 2)
            if treated_wait_sec else None
        ),
        "mean_untreated_bus_wait_sec": (
            round(sum(untreated_wait_sec) / len(untreated_wait_sec), 2)
            if untreated_wait_sec else None
        ),
    }


class BaselineContaminationError(RuntimeError):
    """The no-controller baseline arm shows decisions or TSP treatment."""


class ExportAssertionError(RuntimeError):
    """A summary row failed one of its own internal consistency checks
    (section O): fail the export, never emit a row that contradicts itself."""


def is_baseline_run(config=None):
    config = control_panel.global_config if config is None else config
    return str(config.get("test_model", "None")) == "None"


def _decisions_effective_count(decisions):
    """Turns where the merged route-flag state actually changed, counting
    the first turn against the implicit all-off state before it. A model
    that returns 300 no-op turns in a row scores 0 here, however many rows
    total_decisions counts -- the signal a dead/no-op arm needs."""

    def normalize(flags):
        if not isinstance(flags, dict):
            return frozenset()
        return frozenset(
            (route_id, bool(route_flags.get("tsp", False)), bool(route_flags.get("dbl", False)))
            for route_id, route_flags in flags.items()
            if isinstance(route_flags, dict)
            and (route_flags.get("tsp") is True or route_flags.get("dbl") is True)
        )

    previous = frozenset()
    effective = 0
    for decision in decisions:
        if decision.get("schema") == "signal_plan":
            # AI Configured: a turn counts when the plan it wrote differs.
            plan = decision.get("plan")
            flags = frozenset([json.dumps(plan, sort_keys=True)]) if plan else frozenset()
        else:
            flags = normalize(decision.get("flags", {}))
        if flags != previous:
            effective += 1
        previous = flags
    return effective


def _decision_schedule_metrics(
    all_turn_rows, decisions, nominal_interval=None, actual_seconds=None,
    baseline=False,
):
    """Offered vs. issued decisions under the fixed decision schedule
    (section 0/C): every grid point is one opportunity, whether a model
    answered it (an entry in ``decisions``), was skipped because the
    previous turn was still running (SKIPPED_SLOW), or -- rarely, a stale-arm
    straggler right at a model switch -- came back OBSERVATION_ONLY.
    """
    skipped = [row for row in all_turn_rows if row.get("status") == "SKIPPED_SLOW"]
    expected = 0
    if (
        isinstance(nominal_interval, (int, float))
        and nominal_interval > 0
        and isinstance(actual_seconds, (int, float))
    ):
        expected = max(0, int(float(actual_seconds) // float(nominal_interval)))
    opportunities = max(len(all_turn_rows), expected)
    issued = len(decisions)
    # An OBSERVATION_ONLY row with a schedule stamp is a stale-arm straggler
    # (guard_baseline) from the run that just ended, and can carry that
    # run's much later grid point into this log; one such row turns a clean
    # 5 s cadence into a 300 s gap. Keep the cadence measurement to this run.
    scheduled_times = sorted(
        row["scheduled_sim_time"] for row in all_turn_rows
        if isinstance(row.get("scheduled_sim_time"), (int, float))
        and row.get("status") != "OBSERVATION_ONLY"
        and (
            not isinstance(actual_seconds, (int, float))
            or row["scheduled_sim_time"] <= float(actual_seconds) + 1.0
        )
    )
    gaps = [b - a for a, b in zip(scheduled_times, scheduled_times[1:]) if b > a]
    nominal_candidates = [
        row["decision_interval_sec"] for row in all_turn_rows
        if isinstance(row.get("decision_interval_sec"), (int, float))
    ]
    nominal = statistics.mode(nominal_candidates) if nominal_candidates else nominal_interval
    if not gaps and opportunities >= 2 and nominal:
        gaps = [float(nominal)] * (opportunities - 1)
    return {
        "decision_opportunities": opportunities,
        "decisions_issued": issued,
        "decisions_skipped_slow": (
            0 if baseline else max(len(skipped), opportunities - issued)
        ),
        "utilisation_rate": (
            round(issued / opportunities, 4) if opportunities else None
        ),
        "actual_decision_interval_sec_mean": (
            round(sum(gaps) / len(gaps), 3) if gaps else None
        ),
        "actual_decision_interval_sec_median": (
            round(statistics.median(gaps), 3) if gaps else None
        ),
        "_nominal_decision_interval_sec": nominal,
        "_cadence_gap_count": len(gaps),
    }


def _sampling_params_json(config):
    """Temperature/top_p/etc for an LLM arm; null for the non-LLM comparators
    and baseline, which have no sampling to report."""
    model = config.get("test_model", "None")
    if is_baseline_run(config) or model in control_panel.NON_LLM_MODELS:
        return None
    # Every provider call in agent.py (_call_gemini/_call_openai/_call_grok/
    # _call_ollama) is hardcoded to temperature=0.2; update here if that ever
    # becomes per-run configurable.
    return json.dumps({"temperature": 0.2})


# Standard, non-batch text-token rates checked against provider pricing on
# 2026-09-20. Exact per-request cost from a provider response wins over this
# estimate. Unknown/local models intentionally remain null rather than being
# assigned a fabricated price.
MODEL_TOKEN_RATES_USD_PER_MILLION = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-4.1": (2.00, 8.00),
    "grok-4.6": (2.00, 6.00),
    "grok-4": (3.00, 15.00),
    "grok-3-mini": (0.30, 0.50),
}


def _percentile(values, percentile):
    numbers = sorted(
        float(value) for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
    if not numbers:
        return None
    if len(numbers) == 1:
        return round(numbers[0], 3)
    position = (len(numbers) - 1) * float(percentile)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return round(numbers[lower], 3)
    fraction = position - lower
    return round(numbers[lower] * (1 - fraction) + numbers[upper] * fraction, 3)


def _weighted_percentile(samples, percentile):
    weighted = sorted(
        (float(value), int(weight))
        for value, weight in samples
        if isinstance(value, (int, float)) and int(weight) > 0
    )
    total = sum(weight for _value, weight in weighted)
    if not total:
        return None
    threshold = max(1.0, float(percentile) * total)
    cumulative = 0
    for value, weight in weighted:
        cumulative += weight
        if cumulative >= threshold:
            return round(value, 3)
    return round(weighted[-1][0], 3)


def _decision_reliability(all_turn_rows, cycle_sec, model):
    statuses = [str(row.get("status", "")) for row in all_turn_rows]
    latencies = [
        float(row["latency_ms"])
        for row in all_turn_rows
        if isinstance(row.get("latency_ms"), (int, float))
        and not isinstance(row.get("latency_ms"), bool)
    ]
    input_tokens = sum(
        float(row["input_tokens"])
        for row in all_turn_rows
        if isinstance(row.get("input_tokens"), (int, float))
        and not isinstance(row.get("input_tokens"), bool)
    )
    output_tokens = sum(
        float(row["output_tokens"])
        for row in all_turn_rows
        if isinstance(row.get("output_tokens"), (int, float))
        and not isinstance(row.get("output_tokens"), bool)
    )
    exact_costs = [
        float(row["cost_usd"])
        for row in all_turn_rows
        if isinstance(row.get("cost_usd"), (int, float))
        and not isinstance(row.get("cost_usd"), bool)
    ]
    if exact_costs:
        cost = round(sum(exact_costs), 8)
    elif model in MODEL_TOKEN_RATES_USD_PER_MILLION:
        input_rate, output_rate = MODEL_TOKEN_RATES_USD_PER_MILLION[model]
        cost = round(
            (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000.0,
            8,
        )
    else:
        cost = None
    ratios = (
        [latency / 1000.0 / cycle_sec for latency in latencies]
        if isinstance(cycle_sec, (int, float)) and cycle_sec > 0
        else []
    )
    token_rates = [
        float(row["tokens_per_sec"])
        for row in all_turn_rows
        if isinstance(row.get("tokens_per_sec"), (int, float))
        and not isinstance(row.get("tokens_per_sec"), bool)
    ]
    return {
        "guard_ok_count": statuses.count("OK"),
        "guard_held_all_off_count": statuses.count("HELD_ALL_OFF"),
        "guard_observation_only_count": statuses.count("OBSERVATION_ONLY"),
        "guard_malformed_count": statuses.count("INVALID"),
        "stale_observation_count": sum(bool(row.get("stale")) for row in all_turn_rows),
        "latency_ms_p50": _percentile(latencies, 0.50),
        "latency_ms_p95": _percentile(latencies, 0.95),
        "latency_ms_max": round(max(latencies), 3) if latencies else None,
        "latency_to_cycle_ratio_p50": _percentile(ratios, 0.50),
        "latency_to_cycle_ratio_p95": _percentile(ratios, 0.95),
        "input_tokens_total": int(input_tokens),
        "output_tokens_total": int(output_tokens),
        "tokens_per_sec_mean": (
            round(sum(token_rates) / len(token_rates), 3) if token_rates else None
        ),
        "cost_usd_estimate": cost,
    }


def _controller_experiment_metrics():
    controller = _active_signal_controller
    if controller is not None and hasattr(controller, "get_experiment_metrics"):
        try:
            return controller.get_experiment_metrics()
        except Exception:
            pass
    return {
        "tsp_requests_raised": 0, "tsp_requests_granted": 0,
        "tsp_requests_denied": 0, "tsp_denial_reasons": {},
        "tsp_actions_early_green": 0, "tsp_actions_extending": 0,
        "tsp_total_adjust_frames": 0, "dbl_requests_raised": 0,
        "dbl_requests_granted": 0, "dbl_requests_denied": 0,
        "dbl_denial_reasons": {}, "dbl_actions_activated": 0,
        "dbl_total_active_frames": 0, "discharge_activations": 0,
        "discharge_total_frames": 0, "discharge_recovery_failed": False,
        "node_tsp_grants": {str(node_x): 0 for node_x in canvas.INT_X},
    }


def _qa_unbalanced_seed(campaign_id, model, seed):
    """Best-effort, using only rows already on disk: True when this arm's
    seed set (so far) does not match the union of seeds seen anywhere in the
    campaign. Necessarily partial mid-campaign; accurate once the campaign's
    last (seed, model) combination has been appended."""
    summary_path = experiment_summary_path()
    if not campaign_id or not summary_path.exists():
        return False
    try:
        seeds_by_model = {}
        with summary_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "campaign_id" not in reader.fieldnames:
                return False
            for existing in reader:
                if existing.get("campaign_id") != campaign_id:
                    continue
                seeds_by_model.setdefault(existing.get("model"), set()).add(
                    existing.get("seed")
                )
    except (OSError, csv.Error):
        return False
    seeds_by_model.setdefault(str(model), set()).add(str(seed))
    all_seeds = set().union(*seeds_by_model.values())
    return seeds_by_model.get(str(model), set()) != all_seeds


def _run_export_assertions(row, schedule):
    """Section O: fail the export rather than emit a row that contradicts
    its own inputs. Every check here is cheap arithmetic on values already
    in ``row`` -- this is a regression guard, not new measurement."""
    problems = []
    expected_window = row["checkpoint_min"] * 60.0 - row["warmup_discard_sec"]
    # A checkpoint inside the warm-up has no steady window by design (clamped
    # to 0); only a positive window has to match exactly.
    if expected_window > 0 and abs(row["steady_window_sec"] - expected_window) > 0.02:
        problems.append(
            f"steady_window_sec={row['steady_window_sec']} does not equal "
            f"checkpoint-warmup ({row['checkpoint_min'] * 60.0 - row['warmup_discard_sec']})"
        )
    if row["passengers_served_total"] != row["passengers_served_bus"] + row["passengers_served_car"]:
        problems.append("passengers_served_total != bus + car")
    if row["buses_served"] != row["buses_tsp_treated"] + row["buses_untreated"]:
        problems.append("buses_served != buses_tsp_treated + buses_untreated")
    if abs(
        row["total_person_hours_delay"]
        - (row["bus_person_hours_delay"] + row["car_person_hours_delay"])
    ) > 1e-4:
        problems.append("total_person_hours_delay != bus + car person-hours")
    if abs(
        row["total_person_hours_travel_delay"]
        - (row["bus_person_hours_travel_delay"] + row["car_person_hours_travel_delay"])
    ) > 1e-4:
        problems.append("total_person_hours_travel_delay != bus + car person-hours")
    # Median, not mean: this checks that the schedule's grid points are
    # spaced at the nominal interval, and a single out-of-run row must not be
    # able to fail an otherwise clean run.
    nominal = schedule.get("_nominal_decision_interval_sec")
    realised = row["actual_decision_interval_sec_median"]
    if nominal and schedule.get("_cadence_gap_count", 0) >= 3 and realised is not None:
        deviation = abs(realised - nominal) / nominal
        if deviation > 0.2:
            problems.append(
                f"realised decision cadence (median) {realised}s deviates "
                f"{deviation:.0%} from nominal {nominal}s (fixed-schedule bug?)"
            )
    if problems:
        raise ExportAssertionError("; ".join(problems))


# How far the achieved pace may drift from 1.0 before an arm's wall-clock
# decision latency stops describing its sim-time control delay.
PACE_TOLERANCE = 0.02


def _achieved_pace(sim_seconds):
    """Sim seconds advanced per wall second of running, or None headless."""
    wall = float(_pace_state.get("wall_seconds", 0.0) or 0.0)
    if wall <= 0.0 or sim_seconds <= 0.0:
        return None
    return round(sim_seconds / wall, 4)


def build_experiment_summary_row(
    checkpoint_sim_seconds, workbook_filename=None, actual_sim_seconds=None
):
    """One cumulative-to-date row for the cross-run comparison dataset.

    Reads the same cumulative sources a checkpoint workbook reads --
    network_throughput's running counters, the Bus Events and Decisions
    JSONL logs, the Webster/saturation-flow config -- so the row and the
    workbook it accompanies always describe the same instant.
    """
    config = control_panel.global_config
    throughput = network_throughput
    checkpoint_seconds = float(checkpoint_sim_seconds or 0.0)
    actual_seconds = (
        float(actual_sim_seconds) if actual_sim_seconds is not None else checkpoint_seconds
    )

    bus_pax_served = int(throughput.get("passengers_served_bus", 0) or 0)
    car_pax_served = int(throughput.get("passengers_served_car", 0) or 0)
    bus_delay_sec = int(throughput.get("bus_passenger_delay_frames", 0) or 0) / 60.0
    car_delay_sec = int(throughput.get("car_passenger_delay_frames", 0) or 0) / 60.0
    bus_travel_h = float(throughput.get("bus_passenger_travel_delay_frames", 0) or 0) / 60.0 / 3600.0
    car_travel_h = float(throughput.get("car_passenger_travel_delay_frames", 0) or 0) / 60.0 / 3600.0
    bus_done_sec = int(throughput.get("completed_bus_passenger_delay_frames", 0) or 0) / 60.0
    car_done_sec = int(throughput.get("completed_car_passenger_delay_frames", 0) or 0) / 60.0
    vehicles_served_total = int(throughput.get("vehicles_served_total", 0) or 0)
    mean_stopped_delay_sec_per_vehicle = mean_control_delay_sec_per_vehicle = None
    if vehicles_served_total:
        mean_stopped_delay_sec_per_vehicle = round(
            int(throughput.get("completed_stopped_vehicle_frames", 0) or 0)
            / 60.0 / vehicles_served_total, 2
        )
        mean_control_delay_sec_per_vehicle = round(
            float(throughput.get("completed_control_delay_frames", 0.0) or 0.0)
            / 60.0 / vehicles_served_total, 2
        )
    sample_count = int(throughput.get("vehicles_in_network_sample_count", 0) or 0)
    frame_sum = int(throughput.get("vehicles_in_network_frame_sum", 0) or 0)
    total_served = int(throughput.get("passengers_served_total", 0) or 0)
    buses_served = int(throughput.get("buses_served", 0) or 0)
    cars_served = int(throughput.get("cars_served", 0) or 0)

    all_turn_rows = _read_jsonl_rows(AGENT_TURN_LOG_PATH)
    # Observation-only turns are the agent watching a baseline (or a stale-arm
    # straggler), and SKIPPED_SLOW ticks never reached a model: neither is a
    # decision.
    decisions = [
        row for row in all_turn_rows
        if row.get("status") not in ("OBSERVATION_ONLY", "SKIPPED_SLOW")
    ]
    decision_rollup = summarize_turn_log(decisions)
    nominal_interval = config.get("ai_runtime", {}).get("tick_seconds")
    if config.get("test_running") or config.get("test_duration_sim_seconds"):
        nominal_interval = (config.get("batch_runtime") or {}).get(
            "tick_seconds", nominal_interval
        )
    schedule = _decision_schedule_metrics(
        all_turn_rows,
        decisions,
        nominal_interval=nominal_interval,
        actual_seconds=actual_seconds,
        baseline=is_baseline_run(config),
    )
    bus_events = _read_jsonl_rows(BUS_EVENTS_LOG_PATH)
    mechanism = _bus_event_mechanism_summary(bus_events)
    controller_metrics = _controller_experiment_metrics()
    if is_baseline_run(config) and (
        decision_rollup.get("turns", 0)
        or mechanism["buses_tsp_treated"]
        or controller_metrics.get("tsp_requests_granted", 0)
    ):
        # A reference row with a controller's fingerprints on it is worse
        # than no row: refuse to export it.
        raise BaselineContaminationError(
            f"baseline run logged {decision_rollup.get('turns', 0)} decision(s) "
            f"and {mechanism['buses_tsp_treated']} TSP-treated bus(es)"
        )
    steady = _steady_state_metrics(
        checkpoint_seconds, _read_jsonl_rows(TELEMETRY_LOG_PATH)
    )

    rates = _approach_demand_rates()
    cycle_splits = config.get("cycle_time_sec") or {}
    cycle_sec = cycle_splits.get(canvas.INT_X[0], cycle_splits.get(str(canvas.INT_X[0])))
    mean_latency_ms = decision_rollup.get("avg_latency_ms")
    webster_splits = config.get("webster_splits") or {}
    node_a_split = webster_splits.get(canvas.INT_X[0], webster_splits.get(str(canvas.INT_X[0]))) or {}
    decisions_effective = _decisions_effective_count(decisions)
    model = config.get("test_model", "None")
    campaign_id = _current_campaign_id(config)
    git_sha, git_dirty = _git_info()
    run_started_wall = config.get("_run_started_wall")
    now_wall = time.time()
    qa_duration_deviation_sec = round(actual_seconds - checkpoint_seconds, 4)
    demand_offered_veh = sum(
        int(state.get("requested_arrivals", 0) or 0)
        for state in spawner_states.values()
    ) + int(run_metrics["buses_spawned"])
    demand_offered_pax = sum(
        float(state.get("offered_passengers", 0.0) or 0.0)
        for state in spawner_states.values()
    ) + int(run_metrics["buses_spawned"]) * BUS_PASSENGERS
    reliability = _decision_reliability(all_turn_rows, cycle_sec, model)
    queue_samples = int(run_metrics["queue_samples"] or 0)
    mean_queues = {
        key: round(value / queue_samples, 4) if queue_samples else 0.0
        for key, value in run_metrics["queue_sum_by_approach"].items()
    }
    max_queues = dict(run_metrics["queue_max_by_approach"])
    bus_p50 = _weighted_percentile(run_metrics["bus_delay_samples_sec"], 0.50)
    bus_p90 = _weighted_percentile(run_metrics["bus_delay_samples_sec"], 0.90)
    car_p50 = _weighted_percentile(run_metrics["car_delay_samples_sec"], 0.50)
    car_p90 = _weighted_percentile(run_metrics["car_delay_samples_sec"], 0.90)
    steady_bus_p50 = _weighted_percentile(
        run_metrics["bus_delay_samples_steady_sec"], 0.50
    )
    steady_bus_p90 = _weighted_percentile(
        run_metrics["bus_delay_samples_steady_sec"], 0.90
    )
    steady_car_p50 = _weighted_percentile(
        run_metrics["car_delay_samples_steady_sec"], 0.50
    )
    steady_car_p90 = _weighted_percentile(
        run_metrics["car_delay_samples_steady_sec"], 0.90
    )
    combined_samples = (
        run_metrics["bus_delay_samples_sec"] + run_metrics["car_delay_samples_sec"]
    )
    all_p50 = _weighted_percentile(combined_samples, 0.50)
    all_p90 = _weighted_percentile(combined_samples, 0.90)
    cross_vehicle_count = len(run_metrics["cross_street_vehicle_ids"])
    cross_person_hours = (
        run_metrics["cross_street_passenger_delay_frames"] / 60.0 / 3600.0
    )

    row = {
        "timestamp": round(now_wall, 3),
        "model": model,
        "seed": config.get("test_seed"),
        "checkpoint_min": round(checkpoint_seconds / 60.0, 3),
        "test_duration_min": round(
            float(config.get("test_duration_sim_seconds") or 0.0) / 60.0, 3
        ),
        "run_uuid": config.get("_run_uuid", ""),
        "campaign_id": campaign_id,
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "config_hash": config.get("_regime_hash") or _config_regime_hash(config),
        "run_started_utc": (
            datetime.datetime.fromtimestamp(run_started_wall, datetime.timezone.utc).isoformat()
            if run_started_wall else None
        ),
        "run_ended_utc": datetime.datetime.fromtimestamp(now_wall, datetime.timezone.utc).isoformat(),
        "wall_clock_sec": (
            round(now_wall - run_started_wall, 3) if run_started_wall else None
        ),
        "workbook_filename": workbook_filename or "",
        "schema_version": EXPERIMENT_SUMMARY_SCHEMA_VERSION,
        "movement_model": vehicle_module.movement_model(),
        "control_mode": str(config.get("test_control_mode", control_panel.CONTROL_MODE_ASSISTED)),
        "vehicle_speed_scale": config.get(
            "_active_vehicle_speed_scale", config.get("vehicle_speed_scale")
        ),
        "saturation_flow_veh_hr": config.get("measured_saturation_flow"),
        "cycle_sec": cycle_sec,
        "vc_ratio": real_world_units.network_vc_ratio(
            config, control_panel.approach_configs
        ),
        "demand_EB_veh_min": rates["EB"],
        "demand_WB_veh_min": rates["WB"],
        "demand_A_NB_veh_min": rates["A_NB"],
        "demand_A_SB_veh_min": rates["A_SB"],
        "demand_B_NB_veh_min": rates["B_NB"],
        "demand_B_SB_veh_min": rates["B_SB"],
        "demand_symmetric": _demand_is_symmetric(rates),
        "decision_interval_sec": nominal_interval,
        "sim_speed": config.get("sim_speed"),
        "random_seed": config.get("random_seed"),
        "llm_model": model,
        "llm_armed": bool(config.get("ai_runtime", {}).get("armed", False)),
        "test_duration_sim_sec": config.get("test_duration_sim_seconds"),
        "priority_eligibility_px": config.get("priority_eligibility_px"),
        "link_length_px": canvas.INT_X[-1] - canvas.INT_X[0],
        # yellow/red-clearance are constructed as literal 60/60 at the one
        # SignalController(...) call site (main.py's build_main_window); kept
        # here as constants instead of threading the live instance through
        # the checkpoint/export call chain. Keep in sync if that call site
        # ever parameterizes them.
        "tsp_max_adjust_fraction": TSP_MAX_ADJUST_FRACTION,
        "min_green_frames": MIN_GREEN_FRAMES,
        "TSP_MAX_ADJUST_FRACTION": TSP_MAX_ADJUST_FRACTION,
        "MIN_GREEN_FRAMES": MIN_GREEN_FRAMES,
        "yellow_time_frames": getattr(_active_signal_controller, "yellow_time", 60),
        "red_clearance_time_frames": getattr(
            _active_signal_controller, "red_clearance_time", 60
        ),
        "lost_time_sec": node_a_split.get("lost_time_sec"),
        "min_cycle_sec": node_a_split.get("min_cycle_sec"),
        "discharge_selection": config.get("discharge_selection"),
        "measured_saturation_flow_veh_per_hr": config.get("measured_saturation_flow"),
        "bus_occupancy_pax": BUS_PASSENGERS,
        "demand_model": _network_demand_model(control_panel.approach_configs),
        "demand_draw_hash": _demand_draw_hash_hex(
            up_to_frame=int(round(checkpoint_seconds * 60.0))
        ),
        # The frame the export actually landed on. Equal to the mark when
        # the loop breaks on the boundary; recorded so an overshoot is
        # visible instead of silently moving a fingerprint again.
        "export_frame": int(round(float(actual_sim_seconds or checkpoint_seconds) * 60.0)),
        # Achieved pace. Must be 1.0 for the arm's decision latency to mean
        # what the agent assumes it means (see _pace_state). Empty headless.
        "sim_seconds_per_wall_second": _achieved_pace(
            float(actual_sim_seconds or checkpoint_seconds)
        ),
        "sampling_params_json": _sampling_params_json(config),
        "passengers_served_total": total_served,
        "passengers_served_bus": bus_pax_served,
        "passengers_served_car": car_pax_served,
        "buses_served": buses_served,
        "cars_served": cars_served,
        "pax_per_min": round(total_served / max(checkpoint_seconds / 60.0, 1e-9), 2),
        # Totals are the sum of the rounded parts, so the total == bus + car
        # export assertion holds exactly: rounding the raw sum instead lands
        # 1e-4 off the parts' sum about one run in seven (7 of 20 runs failed
        # this way in the 2026-09-21 batch).
        "total_person_hours_delay": round(
            round(bus_delay_sec / 3600.0, 4) + round(car_delay_sec / 3600.0, 4), 4
        ),
        "bus_person_hours_delay": round(bus_delay_sec / 3600.0, 4),
        "car_person_hours_delay": round(car_delay_sec / 3600.0, 4),
        "total_person_hours_travel_delay": round(
            round(bus_travel_h, 4) + round(car_travel_h, 4), 4
        ),
        "bus_person_hours_travel_delay": round(bus_travel_h, 4),
        "car_person_hours_travel_delay": round(car_travel_h, 4),
        "mean_bus_passenger_delay_sec": (
            round(bus_done_sec / bus_pax_served, 2) if bus_pax_served else None
        ),
        "mean_car_passenger_delay_sec": (
            round(car_done_sec / car_pax_served, 2) if car_pax_served else None
        ),
        "mean_stopped_delay_sec_per_vehicle": mean_stopped_delay_sec_per_vehicle,
        "mean_control_delay_sec_per_vehicle": mean_control_delay_sec_per_vehicle,
        "passenger_hours_in_network": round(
            int(throughput.get("passenger_frames_in_network", 0) or 0) / 60.0 / 3600.0, 4
        ),
        "unfinished_stopped_person_hours": round(
            (bus_delay_sec + car_delay_sec - bus_done_sec - car_done_sec) / 3600.0, 4
        ),
        "mean_vehicles_in_network": (
            round(frame_sum / sample_count, 2) if sample_count else None
        ),
        "max_vehicles_in_network": int(
            throughput.get("vehicles_in_network_max", 0) or 0
        ),
        "buses_tsp_treated": mechanism["buses_tsp_treated"],
        "buses_untreated": mechanism["buses_untreated"],
        "mean_treated_bus_wait_sec": mechanism["mean_treated_bus_wait_sec"],
        "mean_untreated_bus_wait_sec": mechanism["mean_untreated_bus_wait_sec"],
        "mean_decision_latency_ms": mean_latency_ms,
        # The turn log does not distinguish a guard-rejected malformed
        # decision from an agent hold on stale telemetry; both are logged as
        # HELD_ALL_OFF, so this is that combined rate, not guard.py's
        # rejection log alone.
        "guard_reject_rate": (
            round(decision_rollup["held_pct"] / 100.0, 4)
            if decision_rollup.get("turns") else None
        ),
        "total_decisions": decision_rollup.get("turns", 0),
        # The actual lag this run's decisions carried, from their own
        # measured latency (0 for the rule-based comparator, ~model latency
        # for an LLM) -- not the agent's a-priori default.
        "decision_lag_sec_used": (
            round(mean_latency_ms / 1000.0, 3) if mean_latency_ms is not None else None
        ),
        **_control_delay_columns(),
        "decision_opportunities": schedule["decision_opportunities"],
        "decisions_issued": schedule["decisions_issued"],
        "decisions_skipped_slow": schedule["decisions_skipped_slow"],
        "actual_decision_interval_sec_mean": schedule["actual_decision_interval_sec_mean"],
        "actual_decision_interval_sec_median": schedule["actual_decision_interval_sec_median"],
        "decisions_effective": decisions_effective,
        "utilisation_rate": schedule["utilisation_rate"],
        "is_noop": (not is_baseline_run(config)) and decisions_effective == 0,
        "offered_veh": demand_offered_veh,
        "offered_pax": round(demand_offered_pax, 3),
        "served_veh": vehicles_served_total,
        "served_pax": total_served,
        "served_fraction_veh": (
            round(vehicles_served_total / demand_offered_veh, 6)
            if demand_offered_veh else None
        ),
        "served_fraction_pax": (
            round(total_served / demand_offered_pax, 6)
            if demand_offered_pax else None
        ),
        "vehicles_still_in_network_at_end": int(
            run_metrics["vehicles_in_network_current"]
        ),
        "buses_spawned": int(run_metrics["buses_spawned"]),
        "bus_trips_scheduled": int(run_metrics["bus_trips_scheduled"]),
        "bus_trips_pending": sum(len(q) for q in bus_pending_trips.values()),
        "bus_trips_missed": int(run_metrics["bus_trips_missed"]),
        "bus_source_delay_sec": round(run_metrics["bus_source_delay_sec"], 3),
        "buses_completed": buses_served,
        "bus_completion_rate": (
            round(buses_served / run_metrics["buses_spawned"], 6)
            if run_metrics["buses_spawned"] else None
        ),
        "level_of_service": real_world_units.hcm_level_of_service(
            mean_control_delay_sec_per_vehicle
        ),
        **steady,
        **_latent_demand_and_safety_columns(),
        **_network_validation_columns(checkpoint_seconds),
        "signal_timing_json": json.dumps(config.get("_signal_timing") or {}, sort_keys=True),
        "pax_per_min_cumulative": round(
            total_served / max(checkpoint_seconds / 60.0, 1e-9), 2
        ),
        "bus_passenger_delay_sec_p50": bus_p50,
        "bus_passenger_delay_sec_p90": bus_p90,
        "car_passenger_delay_sec_p50": car_p50,
        "car_passenger_delay_sec_p90": car_p90,
        "bus_passenger_delay_sec_p50_steady": steady_bus_p50,
        "bus_passenger_delay_sec_p90_steady": steady_bus_p90,
        "car_passenger_delay_sec_p50_steady": steady_car_p50,
        "car_passenger_delay_sec_p90_steady": steady_car_p90,
        "tsp_requests_raised": controller_metrics["tsp_requests_raised"],
        "tsp_requests_granted": controller_metrics["tsp_requests_granted"],
        "tsp_requests_denied": controller_metrics["tsp_requests_denied"],
        "tsp_grant_rate": (
            round(
                controller_metrics["tsp_requests_granted"]
                / controller_metrics["tsp_requests_raised"], 6
            ) if controller_metrics["tsp_requests_raised"] else None
        ),
        "tsp_denial_reasons_json": json.dumps(
            controller_metrics["tsp_denial_reasons"], sort_keys=True
        ),
        "tsp_actions_early_green": controller_metrics["tsp_actions_early_green"],
        "tsp_actions_extending": controller_metrics["tsp_actions_extending"],
        "tsp_total_adjust_frames": controller_metrics["tsp_total_adjust_frames"],
        "dbl_requests_raised": controller_metrics["dbl_requests_raised"],
        "dbl_requests_granted": controller_metrics["dbl_requests_granted"],
        "dbl_requests_denied": controller_metrics["dbl_requests_denied"],
        "dbl_grant_rate": (
            round(
                controller_metrics["dbl_requests_granted"]
                / controller_metrics["dbl_requests_raised"], 6
            ) if controller_metrics["dbl_requests_raised"] else None
        ),
        "dbl_denial_reasons_json": json.dumps(
            controller_metrics["dbl_denial_reasons"], sort_keys=True
        ),
        "dbl_actions_activated": controller_metrics["dbl_actions_activated"],
        "dbl_total_active_frames": controller_metrics["dbl_total_active_frames"],
        "cross_street_delay_sec_mean": (
            round(
                run_metrics["cross_street_vehicle_delay_frames"]
                / 60.0 / cross_vehicle_count, 3
            ) if cross_vehicle_count else None
        ),
        "cross_street_pax_delayed": int(run_metrics["cross_street_unique_pax"]),
        "tsp_window_cross_street_person_hours": round(cross_person_hours, 6),
        "p90_over_p50_pax_delay": (
            round(all_p90 / all_p50, 6) if all_p50 not in (None, 0) else None
        ),
        "discharge_activations": controller_metrics["discharge_activations"],
        "discharge_total_frames": controller_metrics["discharge_total_frames"],
        "discharge_recovery_failed": bool(
            controller_metrics["discharge_recovery_failed"]
        ),
        "spillback_events": int(run_metrics["spillback_events"]),
        "max_queue_len_by_approach_json": json.dumps(max_queues, sort_keys=True),
        "mean_queue_len_by_approach_json": json.dumps(mean_queues, sort_keys=True),
        "mean_vehicles_in_network_steady": (
            round(
                run_metrics["vehicles_in_network_steady_sum"]
                / run_metrics["vehicles_in_network_steady_samples"], 4
            ) if run_metrics["vehicles_in_network_steady_samples"] else None
        ),
        "network_cleared_at_end": bool(
            run_metrics["vehicles_in_network_current"] == 0
            and all(
                int(state.get("pending_arrivals", 0) or 0) == 0
                for state in spawner_states.values()
            )
        ),
        **reliability,
        **_node_breakdown_columns(config, controller_metrics),
        "qa_baseline_contaminated": False,  # would have raised above otherwise
        "qa_unbalanced_seed": _qa_unbalanced_seed(
            campaign_id, model, config.get("test_seed")
        ),
        "qa_duration_deviation_sec": qa_duration_deviation_sec,
    }
    null_columns = sorted(key for key, value in row.items() if value is None)
    row["qa_null_columns_json"] = json.dumps(null_columns)
    row["qa_flags_count"] = (
        int(row["qa_baseline_contaminated"])
        + int(row["qa_unbalanced_seed"])
        + int(abs(qa_duration_deviation_sec) > 1.0)
        + int(bool(null_columns))
    )
    _run_export_assertions(row, schedule)
    return row


# Summary rows of the CURRENT run, one per checkpoint fired so far, in order.
# Every checkpoint workbook carries them as its "Experiment Summary" sheet, so
# the final workbook of a 10-min run holds the 5-min and 10-min rows side by
# side. perform_full_reset clears it.
_run_summary_rows = []


def build_experiment_summary_row_safely(
    checkpoint_sim_seconds, workbook_filename=None, actual_sim_seconds=None
):
    """The summary row for this checkpoint, or None with the run marked
    failed. Never raises: this runs inside the fixed-step Tk callback, and
    an exception escaping there stops the callback from rescheduling -- the
    sim freezes at the checkpoint and the batch runner, polled from the same
    callback, never advances (2026-09-20 incident)."""
    try:
        return build_experiment_summary_row(
            checkpoint_sim_seconds, workbook_filename, actual_sim_seconds
        )
    except BaselineContaminationError as exc:
        control_panel.global_config["test_failed_reason"] = f"baseline_contaminated: {exc}"
        print(f"\n!!! BASELINE CONTAMINATED -- summary row NOT written: {exc}\n")
    except ExportAssertionError as exc:
        control_panel.global_config["test_failed_reason"] = f"export_assertion: {exc}"
        print(f"\n!!! EXPORT ASSERTION FAILED -- summary row NOT written: {exc}\n")
    except Exception as exc:
        control_panel.global_config["test_failed_reason"] = f"summary_row_error: {exc!r}"
        print(f"\n!!! SUMMARY ROW ERROR -- summary row NOT written: {exc!r}\n")
    return None


def write_experiment_summary_sheet(sheet, rows=None):
    """One row per checkpoint of this run, in EXPERIMENT_SUMMARY_HEADERS order
    -- the same row the CSV gets, so a workbook is self-describing."""
    sheet.append(list(EXPERIMENT_SUMMARY_HEADERS))
    for row in (_run_summary_rows if rows is None else rows):
        sheet.append([_excel_text(row.get(key)) if isinstance(row.get(key), (dict, list))
                      else row.get(key) for key in EXPERIMENT_SUMMARY_HEADERS])


def append_experiment_summary_row(
    checkpoint_sim_seconds, workbook_filename=None, actual_sim_seconds=None, row=None
):
    """Append one row to the accumulating experiment_summary.csv.

    Crash-safe and intentionally outside reset_session_logs: this file is
    the master dataset across the whole experiment (every run, every
    checkpoint), so RESET must never touch it -- only manual deletion does.
    ``row`` lets a caller that already built (and exported) the row reuse
    it; otherwise it is built here. Returns False when no row was written.
    """
    if row is None:
        row = build_experiment_summary_row_safely(
            checkpoint_sim_seconds, workbook_filename, actual_sim_seconds
        )
    if row is None:
        return False
    try:
        summary_path = experiment_summary_path()
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not summary_path.exists()
        if not is_new:
            with summary_path.open("r", encoding="utf-8") as handle:
                header = handle.readline().strip().split(",")
            if header != list(EXPERIMENT_SUMMARY_HEADERS):
                # A schema change never rewrites or misaligns old rows: the
                # old dataset is set aside under a time-stamped name and a
                # new one begins with the current header.
                stamp = time.strftime("%Y%m%d_%H%M%S")
                summary_path.rename(
                    summary_path.with_name(
                        f"{summary_path.stem}_schema_{stamp}{summary_path.suffix}"
                    )
                )
                is_new = True
        with summary_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=EXPERIMENT_SUMMARY_HEADERS)
            if is_new:
                writer.writeheader()
            writer.writerow(row)
        return True
    except Exception as exc:
        print(f"Experiment summary append warning: {exc}")
        return False


def build_test_export_filename(
    model, duration_sim_seconds, seed, timestamp=None, checkpoint_sim_seconds=None,
    control_mode=None,
):
    """Return ``model[-mode]_runtime_seed_DDMMYYYY_HHMMSS.xlsx``.

    For a checkpoint workbook, runtime is the cumulative checkpoint horizon.
    That keeps a 30-minute test's 5/10/15/30-minute exports distinct while
    retaining the exact field order used by every other Excel export. An LLM
    arm also carries its control mode, since that is the one decider that can
    run either way; ``control_mode`` defaults to the mode captured for this
    test at start (``test_control_mode``), which is what the row exports.
    """
    runtime_seconds = (
        duration_sim_seconds if checkpoint_sim_seconds is None else checkpoint_sim_seconds
    )
    if control_mode is None:
        control_mode = control_panel.global_config.get("test_control_mode")
    return build_excel_export_filename(
        model,
        runtime_seconds,
        seed,
        timestamp,
        control_mode=control_mode,
    )


def export_test_workbook(
    model, duration_sim_seconds, seed, destination=None, checkpoint_sim_seconds=None
):
    """Write the combined benchmark workbook for one timed-test checkpoint.

    Runs entirely inside the simulator process: every sheet is built from the
    crash-safe JSONL logs on disk, which are cumulative from run start, so a
    checkpoint mid-run and the final export at duration are both automatic
    cumulative snapshots -- just a read of the logs' current state, not an
    interval since the last checkpoint. ``checkpoint_sim_seconds`` is only
    used to name the file when ``destination`` is not given explicitly.
    """
    try:
        from openpyxl import Workbook
    except ImportError:
        print("openpyxl not installed; skipping test export")
        return None

    decisions = _read_jsonl_rows(AGENT_TURN_LOG_PATH)
    telemetry_rows = _read_jsonl_rows(TELEMETRY_LOG_PATH)
    bus_events = _read_jsonl_rows(BUS_EVENTS_LOG_PATH)

    try:
        workbook = Workbook()
        decisions_sheet = workbook.active
        decisions_sheet.title = "Decisions"
        _write_decisions_sheet(decisions_sheet, decisions)
        _write_telemetry_sheet(
            workbook.create_sheet("Telemetry"), telemetry_rows
        )
        write_ai_decision_audit_sheet(
            workbook.create_sheet("AI Decision Audit"), decisions, telemetry_rows
        )
        _write_llm_performance_sheet(
            workbook.create_sheet("LLM Performance"), decisions
        )
        _write_llm_summary_sheet(
            workbook.create_sheet("LLM Summary"), decisions
        )
        write_control_panel_inputs_sheet(
            workbook.create_sheet("Control Panel Inputs")
        )
        write_bus_events_sheet(workbook.create_sheet("Bus Events"), bus_events)
        write_unit_conversions_sheet(workbook.create_sheet("Unit Conversions"))
        write_experiment_summary_sheet(workbook.create_sheet("Experiment Summary"))
        write_tripinfo_sheet(workbook.create_sheet("Trip Info"))

        if destination is None:
            destination = EXCEL_EXPORT_DIR / build_test_export_filename(
                model, duration_sim_seconds, seed,
                checkpoint_sim_seconds=checkpoint_sim_seconds,
            )
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(destination)
        workbook.close()
        gridlock_monitor.write_workbook(report_path_for(destination))
        if FCD_LOG_PATH.exists():
            shutil.copyfile(FCD_LOG_PATH, destination.with_name(destination.stem + "_fcd.csv"))
        global _run_exported_workbook
        _run_exported_workbook = True
        print(f"Timed test exported: {destination}")
        return destination
    except Exception as exc:
        print(f"Timed test export warning: {exc}")
        return None


# Standard checkpoint marks, in sim-seconds, that a timed test auto-exports
# at (in addition to its own duration). Cumulative from run start every time:
# the "10-minute checkpoint" is everything from t=0 to 10 minutes, not the
# interval since the 5-minute mark, since every source it reads (the JSONL
# logs, network_throughput) already accumulates from t=0.
CHECKPOINT_MARKS_SEC = [300, 600, 900, 1800, 3600, 7200]
# Marks still to fire for the CURRENTLY running test, ascending, populated by
# perform_full_reset and drained by fire_due_checkpoints. The test's own
# duration is deliberately excluded here -- that mark is always the last one
# and is exported and the run stopped by the existing finish_timed_test path,
# so a duration that happens to equal a standard mark (e.g. a 5-minute test)
# fires exactly once instead of twice.
pending_checkpoints_sec = []
# True once this run has written a workbook of its own (a timed test's
# checkpoint or final export). The session workbook built at exit carries a
# strict subset of that workbook's sheets, all from the same JSONL logs, so
# writing it too would just leave a second, thinner copy beside the full one.
# perform_full_reset clears this, so each run decides for itself.
_run_exported_workbook = False


def compute_checkpoint_marks(duration_sim_seconds):
    """Standard marks strictly before ``duration_sim_seconds``, ascending.

    The duration itself is not included: finish_timed_test exports and stops
    the run at that mark, so callers that also want it should test for it
    separately (as fire_due_checkpoints does not).
    """
    if not duration_sim_seconds:
        return []
    duration = float(duration_sim_seconds)
    return sorted(mark for mark in CHECKPOINT_MARKS_SEC if mark < duration)


def at_export_boundary(master_frame_count):
    """True once this frame has reached the next checkpoint or the duration.

    The fixed-step loop breaks here so an export always lands on exactly its
    mark. It used to run its whole catch-up burst first and only then look,
    overshooting by up to ``max_steps_per_callback`` frames -- a per-run
    amount that varied with render speed. ``demand_draw_hash`` is a running
    digest read at export, so an arm that overshot by 3 frames hashed in one
    more arrival than one that overshot by 2 and ``pair_against_baseline``
    refused the pair (campaign 2026-09-22: 29 of 33 arms refused, on demand
    that was in fact identical). Cheap enough to call every frame.
    """
    config = control_panel.global_config
    if not config.get("test_running", False):
        return False
    seconds = master_frame_count / 60.0
    if pending_checkpoints_sec and seconds >= pending_checkpoints_sec[0]:
        return True
    duration = config.get("test_duration_sim_seconds")
    return bool(duration) and seconds >= float(duration)


def fire_due_checkpoints(master_frame_count):
    """Export one cumulative, non-stopping workbook per standard mark reached.

    Called every callback alongside the duration check; the run keeps going
    after each of these, only the final (duration) mark stops it. A summary
    row is appended for each fired mark, exactly as for the final one.
    """
    if not pending_checkpoints_sec:
        return
    config = control_panel.global_config
    if not config.get("test_running", False):
        return
    simulation_time_seconds = master_frame_count / 60.0
    while (
        pending_checkpoints_sec
        and simulation_time_seconds >= pending_checkpoints_sec[0]
    ):
        mark = pending_checkpoints_sec.pop(0)
        _export_checkpoint(config, mark, simulation_time_seconds)


def _export_checkpoint(config, mark_sim_seconds, actual_sim_seconds):
    """Summary row first (so this checkpoint's workbook can carry it), then
    the workbook, then the CSV row. Returns the workbook path or None."""
    model = config.get("test_model", "None")
    duration = config.get("test_duration_sim_seconds")
    seed = config.get("test_seed")
    destination = EXCEL_EXPORT_DIR / build_test_export_filename(
        model, duration, seed, checkpoint_sim_seconds=mark_sim_seconds,
        control_mode=config.get("test_control_mode"),
    )
    row = build_experiment_summary_row_safely(
        mark_sim_seconds, destination.name, actual_sim_seconds
    )
    if row is not None:
        _run_summary_rows.append(row)
    written = export_test_workbook(
        model, duration, seed, destination=destination,
        checkpoint_sim_seconds=mark_sim_seconds,
    )
    if row is not None:
        append_experiment_summary_row(mark_sim_seconds, row=row)
    return written


def timed_test_is_complete(master_frame_count):
    """True once a running timed test has reached its sim-time duration.

    The comparison is on simulation_time_seconds (frames / 60), which is the
    same clock telemetry reports, so render speed cannot change run length.
    """
    config = control_panel.global_config
    if not config.get("test_running", False):
        return False
    duration = config.get("test_duration_sim_seconds")
    if not duration:
        return False
    simulation_time_seconds = master_frame_count / 60.0
    return simulation_time_seconds >= float(duration)


def finish_timed_test(master_frame_count):
    """Stop the run at its configured sim-time and export the final,
    duration-mark checkpoint workbook (cp<duration>min)."""
    config = control_panel.global_config
    config["is_running"] = False
    config["test_running"] = False
    duration = config.get("test_duration_sim_seconds")
    destination = _export_checkpoint(config, duration, master_frame_count / 60.0)
    config["test_last_export"] = destination.name if destination else ""
    control_panel.write_ai_control()
    return destination


# ==========================================================
# BATCH BENCHMARK RUNNER (chains request_start_test, never reimplements it)
# ==========================================================
# A run that overruns this multiple of its own expected wall-clock length
# (duration / sim_speed) -- plus a fixed buffer generous enough for LLM
# inference latency -- is presumed hung (gridlock, a stalled model call,
# repeated timeouts) and is force-stopped and recorded as a FAILED run so
# the batch can move on unattended.
BATCH_TIMEOUT_MULTIPLIER = 3.0
BATCH_TIMEOUT_FIXED_BUFFER_SEC = 120.0
# Substrings looked for (case-insensitively) in a run's agent_turn_log.jsonl
# raw_output/reason text to recognize an API provider's rate-limit/quota
# error distinctly from any other failure.
BATCH_RATE_LIMIT_MARKERS = (
    "429", "rate limit", "ratelimit", "quota", "resourceexhausted",
    "too many requests",
)

# Mutable engine state for the currently active batch, if any. Kept at
# module scope (like pending_checkpoints_sec) because it is driven from
# inside the fixed-step simulation loop, one poll per tick.
_batch_engine = {
    "runner": None,
    "phase": "IDLE",          # IDLE (about to launch) | AWAITING_START | RUN_ACTIVE
    "run_started_wall": None,
}


def _is_batch_api_model(model):
    return model in control_panel.get_api_models()


def _arm_uses_a_model(arm_label):
    """True when this batch arm is served by an LLM, so its turn can block
    for AGENT_CALL_TIMEOUT_CEILING_SEC. The baseline and the deterministic
    comparators answer on the merge tick and never skip a grid point."""
    model = control_panel.split_arm_label(arm_label)[0]
    return not (
        model in ("None", control_panel.BATCH_BASELINE_LABEL)
        or rule_controller.is_rule_model(model)
    )


def _batch_run_hit_rate_limit(model):
    """Scan the just-finished run's own turn log (perform_full_reset clears
    it every run) for this model's rate-limit/quota signature."""
    for row in _read_jsonl_rows(AGENT_TURN_LOG_PATH):
        if str(row.get("model", "")) != model:
            continue
        haystack = f"{row.get('raw_output', '')} {row.get('reason', '')}".lower()
        if any(marker in haystack for marker in BATCH_RATE_LIMIT_MARKERS):
            return True
    return False


def build_batch_runner():
    """Wire a fresh batch_runner.BatchRunner to the real single-run path.

    Every callback is a thin lambda around a control_panel function looked
    up by attribute at call time (not bound eagerly), so tests can
    monkeypatch e.g. control_panel.request_start_test and see the batch
    engine call it -- proving the batch chains the existing single-run path
    instead of reimplementing it.
    """
    def set_seed(seed):
        control_panel.global_config["random_seed"] = seed

    def set_model(model):
        resolved = (
            "None" if model == control_panel.BATCH_BASELINE_LABEL else model
        )
        control_panel.set_active_ai_model(resolved, persist=False)
        control_panel.global_config["ai_runtime"]["armed"] = resolved != "None"
        control_panel.write_ai_control()

    def start_run():
        return control_panel.request_start_test()

    return batch_runner.BatchRunner(
        set_seed=set_seed, set_model=set_model, start_run=start_run
    )


# Two-sided 95 % Student t quantiles, df 1-30 (beyond: 1.96).
_T95 = (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
        2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
        2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042)
# Tolerable error on a model's mean paired DV, as a fraction of that mean.
REPLICATION_TOLERANCE_FRACTION = 0.10


def required_replications(values, tolerance_fraction=REPLICATION_TOLERANCE_FRACTION):
    """Runs needed so the mean is within +/- tolerance of itself at 95 %:
    N = (t_{0.975, n-1} * s / e)^2 (FHWA, Traffic Analysis Toolbox Vol. III,
    Guidelines for Applying Traffic Microsimulation Modeling Software, on the
    number of simulation runs). None when fewer than two values or a zero
    mean make it undefined."""
    values = [float(v) for v in values if v is not None]
    if len(values) < 2:
        return None
    mean = statistics.fmean(values)
    tolerance = abs(mean) * tolerance_fraction
    if tolerance <= 0:
        return None
    t = _T95[len(values) - 2] if len(values) - 1 <= len(_T95) else 1.96
    return max(len(values), math.ceil((t * statistics.stdev(values) / tolerance) ** 2))


def print_campaign_summary(campaign_id):
    """End-of-batch readout from the CSV's final-checkpoint rows of this
    campaign: what a reviewer would otherwise have to compute by hand
    before trusting the numbers."""
    summary_path = experiment_summary_path()
    if not campaign_id or not summary_path.exists():
        return
    try:
        with summary_path.open("r", encoding="utf-8", newline="") as handle:
            rows = [
                row for row in csv.DictReader(handle)
                if row.get("campaign_id") == campaign_id
                and row.get("checkpoint_min") == row.get("test_duration_min")
            ]
    except (OSError, csv.Error):
        return
    if not rows:
        print("\n=== BATCH DONE: no summary rows were written for this campaign ===\n")
        return
    hashes = {row.get("config_hash") for row in rows}
    not_converged = [f"{r['model']}/{r['seed']}" for r in rows if r.get("converged") != "True"]
    noop = [f"{r['model']}/{r['seed']}" for r in rows if r.get("is_noop") == "True"]
    paces = [
        (f"{r['model']}/{r['seed']}", float(r["sim_seconds_per_wall_second"]))
        for r in rows
        if str(r.get("sim_seconds_per_wall_second") or "").strip()
        not in ("", "None")
    ]
    off_pace = [
        f"{name} {pace:.3f}" for name, pace in paces
        if abs(pace - 1.0) > PACE_TOLERANCE
    ]
    lines = [
        f"=== BATCH DONE: campaign {campaign_id} -- {len(rows)} run(s) ===",
        f"config_hash: {'ONE (' + hashes.pop() + ')' if len(hashes) == 1 else 'MULTIPLE ' + str(sorted(hashes)) + ' -- NOT one campaign'}",
        f"not converged: {len(not_converged)} {not_converged if not_converged else ''}",
        f"no-op arms (decisions_effective == 0): {len(noop)} {noop if noop else ''}",
        f"git_dirty rows: {sum(1 for r in rows if r.get('git_dirty') == 'True')}",
        # A run that could not hold 1.0 did not measure the control delay
        # its latency columns claim: the agent times a model call on the
        # wall clock and spends it against the simulation clock.
        (
            f"achieved pace off 1.0 by >{PACE_TOLERANCE:.0%}: {len(off_pace)} {off_pace}"
            if off_pace else
            f"achieved pace: all {len(paces)} run(s) within {PACE_TOLERANCE:.0%} of 1.0"
        ),
    ]
    lines.extend(_campaign_validity_flags(rows))
    pairs, unpaired, mismatched = pair_against_baseline(rows)
    if pairs:
        path = write_paired_dv_csv(campaign_id, pairs)
        lines.append(f"primary DV (net_person_hours_saved vs baseline, same seed) -> {path}")
        by_model = {}
        for pair in pairs:
            by_model.setdefault(pair["model"], []).append(pair["net_person_hours_saved"])
        for model_name, values in sorted(by_model.items()):
            sd = statistics.pstdev(values) if len(values) > 1 else 0.0
            needed = required_replications(values)
            adequacy = (
                "" if needed is None else
                f"  runs needed for +/-{REPLICATION_TOLERANCE_FRACTION:.0%} at 95 %: {needed}"
                + ("" if needed <= len(values) else "  <-- UNDER-REPLICATED")
            )
            lines.append(
                f"  {model_name}: mean {statistics.fmean(values):+.4f} h  sd {sd:.4f}  n={len(values)}{adequacy}"
            )
    if unpaired:
        lines.append(f"seeds without a baseline row (not paired): {unpaired}")
    if mismatched:
        lines.append(f"REFUSED (differs from baseline, not paired): {mismatched}")
    print("\n" + "\n".join(lines) + "\n")


# Latent demand above this share of the offered vehicles means the network
# boundary is truncating queues: FHWA Traffic Analysis Toolbox Vol. III asks
# that demand unable to enter be accounted for, and the model extended until
# it is negligible. GEH < 5 on at least 85 % of counts is the same guide's
# volume acceptance target.
LATENT_DEMAND_WARN_SHARE = 0.05
NETWORK_S_WARN_RATIO = 0.9


def _campaign_validity_flags(rows):
    def floats(key):
        out = []
        for row in rows:
            try:
                out.append((f"{row['model']}/{row['seed']}", float(row.get(key))))
            except (TypeError, ValueError):
                pass
        return out
    lines = []
    latent = [f"{n} {v:.0%}" for n, v in floats("latent_demand_share_at_end") if v > LATENT_DEMAND_WARN_SHARE]
    lines.append(
        f"latent demand > {LATENT_DEMAND_WARN_SHARE:.0%} of offered (queues cut off at the boundary): "
        f"{len(latent)} {latent if latent else ''}"
    )
    s_low = [f"{n} {v:.2f}" for n, v in floats("network_to_calibrated_s_ratio") if v < NETWORK_S_WARN_RATIO]
    lines.append(
        f"in-network saturation flow < {NETWORK_S_WARN_RATIO:.0%} of calibrated S: "
        f"{len(s_low)} {s_low if s_low else ''}"
    )
    geh = [f"{n} {v:.0%}" for n, v in floats("geh_entry_share_below_5") if v < 0.85]
    lines.append(f"GEH<5 on fewer than 85 % of sources: {len(geh)} {geh if geh else ''}")
    return lines


# Columns a baseline and an arm row must agree on before their difference
# can be attributed to the controller.
PAIRING_MUST_MATCH = (
    "config_hash", "git_sha", "demand_draw_hash", "checkpoint_min", "test_duration_min",
)
PAIRED_DV_HEADERS = (
    "campaign_id", "seed", "model", "baseline_run_uuid", "arm_run_uuid",
    "net_person_hours_saved", "bus_person_hours_saved", "car_person_hours_saved",
    "net_person_hours_saved_on_road", "net_person_hours_saved_stopped", "converged_both",
)


def pair_against_baseline(rows):
    """The primary DV. For every non-baseline final-checkpoint row that has a
    baseline (model == "None") row on the same seed, return
    ``baseline âˆ’ arm`` of the steady travel-delay person-hours (positive =
    the arm saved person-hours), its bus/car split, and the same difference
    on the stopped-delay measure. Returns ``(pairs, seeds_without_baseline,
    mismatches)``: a pair is refused, and listed with the offending columns,
    unless everything but the treatment matches (PAIRING_MUST_MATCH: campaign,
    frozen regime hash, code revision, offered-demand schedule, duration and
    measurement window), since the difference would otherwise not be the
    controller's. Pure: takes CSV DictReader rows, so it can be re-run on
    any dataset."""
    def num(row, key):
        try:
            return float(row.get(key))
        except (TypeError, ValueError):
            return None

    baselines = {
        (row.get("campaign_id"), row.get("seed")): row
        for row in rows if row.get("model") == "None"
    }
    pairs, unpaired, mismatched = [], set(), []
    for row in rows:
        if row.get("model") == "None":
            continue
        base = baselines.get((row.get("campaign_id"), row.get("seed")))
        if base is None:
            unpaired.add(row.get("seed"))
            continue
        differing = [key for key in PAIRING_MUST_MATCH if base.get(key) != row.get(key)]
        if differing:
            mismatched.append(f"{row.get('model')}/{row.get('seed')}: {', '.join(differing)}")
            continue

        def diff(key):
            b, a = num(base, key), num(row, key)
            return round(b - a, 4) if b is not None and a is not None else None

        pairs.append({
            "campaign_id": row.get("campaign_id"),
            "seed": row.get("seed"),
            "model": row.get("model"),
            "baseline_run_uuid": base.get("run_uuid"),
            "arm_run_uuid": row.get("run_uuid"),
            "net_person_hours_saved": diff("total_person_hours_delay_incl_entry_steady"),
            "bus_person_hours_saved": diff("bus_person_hours_delay_incl_entry_steady"),
            "car_person_hours_saved": diff("car_person_hours_delay_incl_entry_steady"),
            "net_person_hours_saved_on_road": diff("total_person_hours_travel_delay_steady"),
            "net_person_hours_saved_stopped": diff("total_person_hours_delay_steady"),
            "converged_both": (
                base.get("converged") == "True" and row.get("converged") == "True"
            ),
        })
    return pairs, sorted(unpaired, key=str), mismatched


def write_paired_dv_csv(campaign_id, pairs):
    """Atomically write paired_dv_<campaign>.csv beside experiment_summary.csv."""
    path = experiment_summary_path().with_name(f"paired_dv_{campaign_id}.csv")
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PAIRED_DV_HEADERS)
        writer.writeheader()
        writer.writerows(pairs)
    tmp.replace(path)  # atomic, same as os.replace
    return path


def poll_batch_runner():
    """Drive the Batch Benchmark Runner one tick at a time from inside the
    fixed simulation loop: batch progress can only be observed through the
    same test_running/start_requested flags perform_full_reset flips, so it
    is polled here rather than on any independent timer.
    """
    config = control_panel.global_config
    runtime = config.setdefault(
        "batch_runtime", dict(control_panel.DEFAULT_BATCH_RUNTIME)
    )

    if config.get("batch_start_requested", False):
        config["batch_start_requested"] = False
        runner = build_batch_runner()
        _batch_engine["runner"] = runner
        _batch_engine["phase"] = "IDLE"
        # One campaign id for every run of this batch: what the summary
        # rows, manifests and the end-of-batch checks group by.
        runtime["campaign_id"] = str(uuid.uuid4())
        runtime["campaign_stamp"] = time.strftime("%Y%m%d")
        _, dirty = _git_info()
        if dirty:
            print(
                "\n!!! BATCH STARTED WITH UNCOMMITTED CHANGES -- git_dirty=True will be "
                "recorded on every row; commit first if this is a campaign.\n"
            )
        if (config.get("test_duration_sim_seconds") or 0) < 1800:
            print(
                "\n!!! BATCH DURATION < 30 min: steady-state DVs rarely converge this "
                "short (see converged / time_to_converge_sec).\n"
            )
        queued_models = list(runtime.get("models", []))
        tick = int(runtime.get("tick_seconds", control_panel.DEFAULT_TICK_SECONDS))
        floor = control_panel.MIN_UNSKIPPED_TICK_SECONDS
        if tick < floor and any(_arm_uses_a_model(model) for model in queued_models):
            print(
                f"\n!!! DECISION TICK {tick}s IS BELOW {floor}s: a model call can run for "
                f"{control_panel.AGENT_CALL_TIMEOUT_CEILING_SEC}s, so a slow arm spends the run "
                "skipping grid points (decisions_skipped_slow) instead of sharing one decision "
                "schedule with the other arms. Raise the batch Decision interval.\n"
            )
        runner.start(queued_models, list(runtime.get("seeds", [])))
        runtime["current"] = dict(runner.current) if runner.current else None

    runner = _batch_engine.get("runner")
    if runner is None or not runner.is_active():
        if runner is not None:
            runtime["active"] = False
            runtime["current"] = None
            runtime["results"] = list(runner.results)
            _batch_engine["runner"] = None
            print_campaign_summary(runtime.get("campaign_id"))
            runtime["campaign_id"] = None
            runtime["campaign_stamp"] = None
        return

    if config.get("batch_end_requested", False):
        # END: drop the run in flight and the queue, then the same full
        # reset RESET performs (consumed by the loop on its next tick).
        # Nothing is exported: a partial run has no summary row to pair.
        config["batch_end_requested"] = False
        config["batch_stop_requested"] = False
        config["batch_resume_requested"] = False
        _batch_engine["runner"] = None
        runtime.update({
            "active": False, "paused": False, "current": None,
            "results": [], "total": 0, "campaign_id": None,
        })
        config.update({
            "is_running": False, "test_running": False, "is_paused": False,
            "start_requested": False, "reset_triggered": True,
        })
        control_panel.set_active_ai_model("None", persist=False)
        config["ai_runtime"]["armed"] = False
        control_panel.write_ai_control()
        return

    if config.get("batch_stop_requested", False):
        config["batch_stop_requested"] = False
        if not runtime.get("paused", False):
            runtime["paused"] = True
            _batch_engine["paused_wall"] = time.monotonic()
    if config.get("batch_resume_requested", False):
        config["batch_resume_requested"] = False
        if runtime.get("paused", False):
            runtime["paused"] = False
            config["is_paused"] = False
            # The run's wall-clock timeout budget must not count the pause.
            _batch_engine["run_started_wall"] = (
                _batch_engine.get("run_started_wall") or time.monotonic()
            ) + time.monotonic() - _batch_engine.get("paused_wall", time.monotonic())
    if runtime.get("paused", False):
        # Frozen in place: hold the run in flight (or one that started on
        # the tick the stop landed) and start no other.
        if config.get("is_running", False):
            config["is_paused"] = True
        return

    phase = _batch_engine["phase"]
    if phase == "IDLE":
        # A run was just queued (by start() or the previous outcome's
        # _advance()); wait a tick for perform_full_reset to pick it up.
        _batch_engine["run_started_wall"] = time.monotonic()
        _batch_engine["phase"] = "AWAITING_START"
    elif phase == "AWAITING_START":
        if config.get("test_running", False):
            _batch_engine["phase"] = "RUN_ACTIVE"
    elif phase == "RUN_ACTIVE":
        model = runner.current["model"]
        elapsed = time.monotonic() - _batch_engine["run_started_wall"]
        duration = config.get("test_duration_sim_seconds") or 0
        sim_speed = max(0.1, config.get("sim_speed", 1.0))
        budget = (
            (duration / sim_speed) * BATCH_TIMEOUT_MULTIPLIER
            + BATCH_TIMEOUT_FIXED_BUFFER_SEC
        )
        still_running = config.get("test_running", False)
        timed_out = still_running and elapsed > budget

        if timed_out:
            control_panel.request_start_stop()
            still_running = False

        if still_running:
            pass  # keep waiting; nothing to report yet
        else:
            is_api_model = _is_batch_api_model(model)
            rate_limited = is_api_model and _batch_run_hit_rate_limit(model)
            failed_reason = config.get("test_failed_reason")
            if failed_reason:
                runner.report_run_outcome("FAILED", reason=str(failed_reason))
            elif timed_out:
                runner.report_run_outcome(
                    "FAILED", reason="timeout",
                    rate_limited=rate_limited, is_api_model=is_api_model,
                )
            elif rate_limited:
                runner.report_run_outcome(
                    "FAILED", reason="rate_limited",
                    rate_limited=True, is_api_model=is_api_model,
                )
            else:
                runner.report_run_outcome("COMPLETED")

            runtime["results"] = list(runner.results)
            runtime["current"] = dict(runner.current) if runner.current else None
            _batch_engine["phase"] = "IDLE"
            if not runner.is_active():
                runtime["active"] = False
                _batch_engine["runner"] = None

    if runner.current is not None:
        runtime["current"] = dict(runner.current)


def _set_ai_flags(flags, signals=None):
    """Apply decided route flags. A route with a live request in ``signals``
    keeps its current flags (see SignalController.routes_with_live_requests):
    the lock agent.check_locked applies from a telemetry snapshot is enforced
    here from the live controller, so neither a stale/held all-off nor a
    request raised after the snapshot can cancel a committed treatment."""
    held = signals.routes_with_live_requests() if signals is not None else set()
    for route_id, route_flags in flags.items():
        if route_id in held:
            continue
        route_config = control_panel.bus_routes_config[route_id]
        route_config["tsp_enabled"] = route_flags["tsp"]
        route_config["dbl_enabled"] = route_flags["dbl"]


# The frame step_simulation is on, and its vehicle list: trip records stamp
# departures with the first, and an export writes unfinished trips from the
# second without the export path having to be handed the list.
_current_frame = {"n": 0}
_live_vehicles = None

# The controller step_simulation last drove: merge_live_decision needs it to
# apply (or withdraw) an AI Configured plan without changing the decide()
# contract headless_run and the trainer share.
_live_signals = None


def _hold_all_off(signals):
    """Fail closed in both modes: no new route-flag treatment, plan withdrawn
    (Webster). A request already live finishes under the safety authority."""
    _set_ai_flags(guard.all_off_flags(), signals)
    if signals is not None:
        signals.clear_plan()


def _record_decision_applied(decision, turn):
    """First frame a turn's decision took effect, against the frame of the
    snapshot it was made from. Their difference is the control delay in sim
    time -- detection of the grid point, the model call and the 30-frame
    merge -- measured on the sim clock, so it holds whatever the achieved
    pace, where wall latency x pace is only an estimate."""
    applied = run_metrics.setdefault("decision_applied", {})
    if turn <= 0 or turn in applied:
        return
    try:
        snapshot_frame = int(decision.get("telemetry_frame"))
    except (TypeError, ValueError):
        snapshot_frame = None
    applied[turn] = (snapshot_frame, int(_current_frame["n"]))


def _control_delay_columns():
    delays = sorted(
        (applied - snapshot) / 60.0
        for snapshot, applied in run_metrics.get("decision_applied", {}).values()
        if snapshot is not None and applied >= snapshot
    )
    if not delays:
        return {"decisions_applied": 0, "control_delay_sim_sec_median": None,
                "control_delay_sim_sec_p95": None}
    return {
        "decisions_applied": len(delays),
        "control_delay_sim_sec_median": round(statistics.median(delays), 3),
        "control_delay_sim_sec_p95": round(delays[min(len(delays) - 1, int(0.95 * len(delays)))], 3),
    }


def merge_ai_decision(path=None, signals=None):
    """Validate and merge one file-based AI decision: route flags (AI
    Assisted) into live route config, or a timing plan (AI Configured) into
    ``signals``. Every refusal holds all-off / Webster."""
    runtime = control_panel.global_config.setdefault("ai_runtime", {})
    configured = runtime.get("control_mode") == control_panel.CONTROL_MODE_CONFIGURED
    # Only an explicitly recorded "None" selection means a baseline run. An
    # absent key leaves the generic merge path alone, so callers that drive
    # decisions without a model selector keep working.
    if "model" in runtime and str(runtime["model"]) == "None":
        # There is no decision to wait for, and a decision file left over from
        # an earlier model must not leak into the unaided run.
        runtime["last_status"] = control_panel.BASELINE_NO_MODEL
        return False
    decision_path = DECISION_PATH if path is None else Path(path)
    try:
        with decision_path.open("r", encoding="utf-8") as decision_file:
            decision = json.load(decision_file)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        runtime["last_status"] = "WAITING_FOR_DECISION"
        return False

    tick_seconds = runtime.get("tick_seconds", 5)
    try:
        tick_seconds = float(tick_seconds or 5)
    except (TypeError, ValueError, OverflowError):
        tick_seconds = 5.0
    if not math.isfinite(tick_seconds) or tick_seconds <= 0:
        tick_seconds = 5.0
    stale_after = max(
        tick_seconds * DECISION_STALE_MULTIPLIER,
        DECISION_STALE_FLOOR_SEC,
    )
    timestamp = decision.get("timestamp") if isinstance(decision, dict) else None
    try:
        age = time.time() - float(timestamp)
    except (TypeError, ValueError, OverflowError):
        age = None
    if age is None or not math.isfinite(age) or age > stale_after:
        _hold_all_off(signals)
        runtime["last_status"] = "STALE_DECISION"
        return False

    # Identity: the decision must have been made for this run and by the
    # selected model. A file left by an earlier run, arm or model (or one
    # written by another simulator sharing data/) is rejected, all-off.
    run_uuid = control_panel.global_config.get("_run_uuid")
    if (run_uuid and decision.get("run_uuid") != run_uuid) or (
        "model" in runtime and str(decision.get("model")) != str(runtime["model"])
    ) or configured != (decision.get("schema") == "signal_plan"):
        # A decision of the other mode is a straggler from before the
        # mode changed: never a plan for this run.
        _hold_all_off(signals)
        runtime["last_status"] = "FOREIGN_DECISION"
        return False

    try:
        turn = int(decision.get("turn", 0))
    except (AttributeError, TypeError, ValueError, OverflowError):
        turn = 0
    last_turn = int(runtime.get("last_turn", 0) or 0)
    if turn < last_turn:
        # Ordering: an older turn than the one already merged is a
        # regression (an out-of-order or replayed file), never a new decision.
        _hold_all_off(signals)
        runtime["last_status"] = "STALE_DECISION"
        return False

    if configured:
        plan = guard.validate_signal_plan(decision)
        if plan is None and decision.get("status") != "HELD_WEBSTER":
            _hold_all_off(signals)
            runtime["last_status"] = "INVALID_DECISION"
            return False
        _set_ai_flags(guard.all_off_flags(), signals)   # no route-flag TSP under a plan
        if signals is not None:
            if plan is None:
                signals.clear_plan()
            elif turn != runtime.get("last_turn") or not signals.plan_active:
                # Once per decision: end_current_green_now must not re-fire
                # on every 30-frame merge of the same file.
                signals.apply_plan(plan)
        runtime["last_turn"] = turn
        _record_decision_applied(decision, turn)
        runtime["last_status"] = "OK" if plan is not None else "HELD_WEBSTER"
        return plan is not None

    flags = guard.validate_flags(decision)
    if flags is None:
        _hold_all_off(signals)
        runtime["last_status"] = "INVALID_DECISION"
        return False

    runtime["last_turn"] = turn
    _record_decision_applied(decision, turn)
    _set_ai_flags(flags, signals)
    decision_status = decision.get("status", "OK")
    runtime["last_status"] = (
        decision_status
        if decision_status in ("OK", "HELD_ALL_OFF")
        else "OK"
    )
    return True


def reset_all_spawner_states():
    """Clear vehicle and bus source state for a fresh traffic episode."""
    global post_discharge_meter_frames_remaining, bus_sequence_counter, bus_schedule_time_s
    for state in spawner_states.values():
        state.clear()
        state.update(_new_spawner_state())
    for route_id in bus_dispatch_counters:
        bus_dispatch_counters[route_id] = 0.0
        bus_pending_trips[route_id].clear()
    bus_schedule_time_s = 0.0
    bus_sequence_counter = 0
    post_discharge_meter_frames_remaining = 0
    _route_trip_index.clear()


def reset_traffic_generation():
    """Reset every traffic source, its RNG sequence and its vehicle numbering.

    Clears the offered-demand fingerprint too. perform_full_reset calls this
    AFTER calibrate_and_apply_webster, and it has to: the calibrator spawns
    through the same sources, so a fingerprint cleared before it ran
    described "calibration draws plus the run" rather than the run.
    """
    _demand_draw_state["offers"].clear()
    _pace_state["wall_seconds"] = 0.0
    _discharge_state["last"].clear()
    _discharge_state["headways"].clear()
    vehicle_module.reset_vehicle_serials()
    vehicle_module.reset_safety_counters()
    reset_all_spawner_states()
    # Freeze motion tuning for the episode. Slider changes made during a run
    # therefore cannot change the speed of only later arrivals.
    apply_configured_vehicle_speed_scale()
    return apply_configured_random_seed()


# --- Webster saturation-flow calibration ------------------------------------
# Capacity here depends on the configured speed scale and vehicle mix, so the
# saturation flow S that Webster needs is measured fresh at every run start
# rather than hardcoded. The method is the HCM queue discharge: build a
# standing queue behind red, release it with no new arrivals, discard the
# startup vehicles and take the mean headway of the rest.
CALIBRATION_APPROACH = "EB"
CALIBRATION_SPAWN_X = -900          # Upstream of the canvas: real queue storage
CALIBRATION_TARGET_QUEUE = 30       # Measured: 12 gives sd +/-920 veh/hr
                                    # (+/-27%), 30 lands within ~3% of a
                                    # careful 5-seed reference for ~1-2s
CALIBRATION_BUILD_LIMIT_SEC = 180.0
CALIBRATION_DISCHARGE_LIMIT_SEC = 120.0
CALIBRATION_STARTUP_VEHICLES = 4    # Startup lost time, excluded per HCM
# A discharge headway above this multiple of the median is a stalled queue,
# not a saturation headway, and is left out of S (see calibrate_saturation_flow).
CALIBRATION_INTERRUPTION_FACTOR = 2.5
CALIBRATION_BUILD_VEH_PER_MIN = 120
CALIBRATION_SEED_FALLBACK = 20260909
DEFAULT_SATURATION_FLOW = 1366.0    # Only used if a calibration run yields none


def _calibration_signals(green):
    """Force the measured approach red or green, with node B always green."""
    measured = "GREEN" if green else "RED"
    return {
        canvas.INT_X[0]: {
            "EB": measured, "WB": "RED", "NB": "RED", "SB": "RED",
        },
        canvas.INT_X[1]: {
            "EB": "GREEN", "WB": "RED", "NB": "RED", "SB": "RED",
        },
    }


def calibrate_saturation_flow(
    speed_scale, heavy_ratio, seed=None, target_queue=CALIBRATION_TARGET_QUEUE,
    details=False,
):
    """Measure saturation flow (veh/hr/lane) by discharging a standing queue.

    Deterministic for a given seed. Runs headless and leaves no vehicles
    behind; the caller re-seeds traffic generation afterwards so the measured
    run itself starts from a clean RNG.
    """
    from src.core.signal_controller import SignalController

    approach_cfg = dict(control_panel.approach_configs[CALIBRATION_APPROACH])
    approach_cfg.update(
        {
            "active": True,
            "model": "Poisson",
            "rate": CALIBRATION_BUILD_VEH_PER_MIN,
            "turn_split": 1.0,          # all straight: a single-lane measure
            "left_far_share": 0.0,
            "heavy_ratio": heavy_ratio,
        }
    )
    lane_y = canvas.H_Y - 1.5 * canvas.LANE
    lane_coords = [lane_y, lane_y, lane_y]

    previous_scale = control_panel.global_config.get(
        "_active_vehicle_speed_scale", speed_scale
    )
    control_panel.global_config["_active_vehicle_speed_scale"] = speed_scale
    # Always the fixed calibration seed, never the run seed: S describes the
    # regime (speed scale, vehicle mix, movement engine), not which seed
    # happens to be running. Seeding it from the run seed made S a function
    # of the seed -- 1887 veh/hr at seed 234 against 2338 at seed 764, a 24%
    # swing that drove the Webster cycle to 81.6 s against 40.0 s (its floor)
    # and put two config_hash values inside one campaign, so the two halves
    # of the sweep were benchmarking different signal plans.
    random.seed(CALIBRATION_SEED_FALLBACK)
    reset_all_spawner_states()
    seed_demand_streams(CALIBRATION_SEED_FALLBACK)

    controller = SignalController(
        {"green_time": 240, "is_running": True},
        yellow_time=60,
        red_clearance_time=60,
    )
    vehicles = []
    upstream = {}

    def queued():
        return [
            vehicle
            for vehicle in vehicles
            if vehicle.is_front_bumper_upstream(
                canvas.INT_X[0], canvas.H_Y, canvas.ROAD_W, canvas.STOP
            )
        ]

    def step(signals, spawning):
        if spawning:
            try_spawn_vehicle(
                vehicles, CALIBRATION_APPROACH, CALIBRATION_APPROACH,
                CALIBRATION_SPAWN_X, lane_coords, approach_cfg, min_gap=40,
            )
        crossings = 0
        for vehicle in list(vehicles):
            key = id(vehicle)
            if key not in upstream:
                upstream[key] = vehicle.is_front_bumper_upstream(
                    canvas.INT_X[0], canvas.H_Y, canvas.ROAD_W, canvas.STOP
                )
            vehicle.update(
                signals, canvas.INT_X, canvas.H_Y,
                road_w=canvas.ROAD_W, stop_offset=canvas.STOP,
                lane_w=canvas.LANE, all_vehicles=vehicles,
                signal_controller=controller,
            )
            after = vehicle.is_front_bumper_upstream(
                canvas.INT_X[0], canvas.H_Y, canvas.ROAD_W, canvas.STOP
            )
            if upstream[key] and not after:
                crossings += 1
            upstream[key] = after
        controller.update(vehicles)
        vehicles[:] = [v for v in vehicles if v.x <= canvas.WIDTH + 150]
        return crossings

    red = _calibration_signals(False)
    green = _calibration_signals(True)
    crossing_frames = []
    with lane_changes_suspended():
        for _ in range(int(CALIBRATION_BUILD_LIMIT_SEC * 60)):
            step(red, spawning=True)
            if len(queued()) >= target_queue:
                break

        for frame in range(int(CALIBRATION_DISCHARGE_LIMIT_SEC * 60)):
            for _ in range(step(green, spawning=False)):
                crossing_frames.append(frame)
            if not queued():
                break

    control_panel.global_config["_active_vehicle_speed_scale"] = previous_scale

    headways = [
        (later - earlier) / 60.0
        for earlier, later in zip(crossing_frames, crossing_frames[1:])
    ][CALIBRATION_STARTUP_VEHICLES:]
    excluded = 0
    startup_source = "hcm_default"
    startup_lost = webster.HCM_STARTUP_LOST_TIME_SEC
    if not headways or sum(headways) <= 0:
        saturation = DEFAULT_SATURATION_FLOW
    else:
        # Saturation headways come from an uninterrupted queue discharge
        # (HCM 7th ed., Ch. 31 field procedure). A gap several times the
        # median is the queue having stopped discharging, not a headway:
        # the legacy engine's absolute 0.5 px/frame "standing" threshold
        # makes a slow truck ahead read as spillback and stalls the queue
        # behind it for up to 20 s, which averaged into S and made l1 14 s.
        median = statistics.median(headways)
        clean = [h for h in headways if h <= CALIBRATION_INTERRUPTION_FACTOR * median]
        excluded = len(headways) - len(clean)
        mean_headway = sum(clean) / len(clean)
        saturation = 3600.0 / mean_headway
        # HCM start-up lost time: the time the first four queued vehicles
        # take beyond four saturation headways, measured from green onset
        # (HCM 7th ed., Ch. 19; field method in Ch. 31). Measured on the
        # engine that drives, like S, so Webster's lost time describes it --
        # unless those first four were themselves interrupted, in which case
        # the HCM default is used and the source says so.
        n = CALIBRATION_STARTUP_VEHICLES
        if len(crossing_frames) >= n:
            first = [crossing_frames[0] / 60.0] + [
                (later - earlier) / 60.0
                for earlier, later in zip(crossing_frames[: n - 1], crossing_frames[1:n])
            ]
            if max(first) <= CALIBRATION_INTERRUPTION_FACTOR * max(median, mean_headway):
                startup_lost = max(0.0, crossing_frames[n - 1] / 60.0 - n * mean_headway)
                startup_source = "measured"
    if details:
        return {
            "saturation_flow": saturation,
            "startup_lost_sec": round(startup_lost, 2),
            "startup_lost_source": startup_source,
            "excluded_headways": excluded,
        }
    return saturation


def representative_heavy_ratio():
    """Mean heavy-vehicle share across the configured approaches."""
    ratios = [
        float(cfg.get("heavy_ratio", 0.1))
        for cfg in control_panel.approach_configs.values()
    ]
    return sum(ratios) / len(ratios) if ratios else 0.1


# Measured S per regime key, so one sweep measures each regime once. The key
# is everything calibrate_saturation_flow's answer depends on.
_saturation_cache = {}


def _calibration_key(config):
    return (
        round(float(config.get("vehicle_speed_scale", 0.5) or 0.5), 6),
        round(float(representative_heavy_ratio()), 6),
        str(config.get("movement_model", vehicle_module.MOVEMENT_MODEL_LEGACY)),
        json.dumps(vehicle_module.movement_model_signature(), sort_keys=True, default=str),
    )


def signal_change_intervals(config):
    """(yellow_frames, all_red_frames) for this run.

    "ite": ITE (2020) kinematic change and clearance intervals at the
    85th-percentile desired speed of the car stream, over the stop line to
    far-side box edge, bounded by MUTCD guidance (webster.ite_change_intervals).
    "legacy": the former fixed 1 s + 1 s, kept so pinned results reproduce.
    """
    if config.get("signal_change_intervals", "ite") == "legacy":
        return 60, 60
    lo, hi = CAR_DESIRED_SPEED_RANGE
    v85_px = (lo + 0.85 * (hi - lo)) * float(config.get("vehicle_speed_scale", 0.5) or 0.5)
    v85_mps = v85_px * vehicle_module.METERS_PER_PX * vehicle_module.FPS
    width_m = (canvas.ROAD_W + canvas.STOP) * vehicle_module.METERS_PER_PX
    yellow, all_red = webster.ite_change_intervals(v85_mps, width_m)
    return int(round(yellow * 60)), int(round(all_red * 60))


def coordination_offsets_frames(config, splits):
    """Offset of each node's EW-green start against the master cycle, frames.

    The first node the progression direction reaches is the reference
    (offset 0); the next starts one link travel time later at the mean car
    desired speed, so a platoon released on its green arrives on green
    (NCHRP Report 812, time-space / offset design).
    """
    lo, hi = CAR_DESIRED_SPEED_RANGE
    speed_px = (lo + hi) / 2.0 * float(config.get("vehicle_speed_scale", 0.5) or 0.5)
    link_px = abs(canvas.INT_X[1] - canvas.INT_X[0])
    travel = int(round(link_px / max(speed_px, 1e-6)))
    cycle = max(split["cycle_time_frames"] for split in splits.values())
    first, second = (canvas.INT_X[0], canvas.INT_X[1])
    if config.get("coordination_direction", "EB") == "WB":
        first, second = second, first
    return {first: 0, second: travel % cycle}, cycle


def calibrate_and_apply_webster(signals, force=False):
    """Measure S and start-up lost time, set the change intervals, and publish
    Webster splits (and the coordination plan) before the run clock starts.

    S is cached per regime: it is a property of the speed scale, vehicle mix
    and movement engine, none of which vary within a sweep, so re-measuring
    it per run only added setup time and a chance to disagree with itself.
    """
    config = control_panel.global_config
    config["calibrating"] = True
    try:
        key = _calibration_key(config)
        if force or key not in _saturation_cache:
            _saturation_cache[key] = calibrate_saturation_flow(
                config.get("vehicle_speed_scale", 0.5),
                representative_heavy_ratio(),
                None,
                details=True,
            )
        measured = _saturation_cache[key]
        if isinstance(measured, dict):
            saturation = float(measured["saturation_flow"])
            startup_lost = float(measured["startup_lost_sec"])
            startup_source = measured.get("startup_lost_source", "measured")
            excluded = int(measured.get("excluded_headways", 0))
        else:  # a stubbed calibrator returns S alone
            saturation = float(measured)
            startup_lost = webster.HCM_STARTUP_LOST_TIME_SEC
            startup_source, excluded = "hcm_default", 0
        config["measured_saturation_flow"] = round(saturation, 0)

        yellow, all_red = signal_change_intervals(config)
        # Tolerate controllers that do not expose the interlock timings.
        if hasattr(signals, "yellow_time"):
            signals.yellow_time = yellow
        if hasattr(signals, "red_clearance_time"):
            signals.red_clearance_time = all_red
        extension = webster.HCM_EFFECTIVE_GREEN_EXTENSION_SEC
        lost_time = webster.lost_time_seconds(
            yellow, all_red, startup_lost_sec=startup_lost, extension_sec=extension
        )
        coordinated = config.get("signal_coordination", "coordinated") == "coordinated"
        splits = webster.compute_all_nodes(
            control_panel.approach_configs,
            saturation,
            lost_time_sec=lost_time,
            displayed_green_offset_sec=startup_lost - extension,
            common_cycle=coordinated,
        )
        config["webster_splits"] = splits
        config["cycle_time_sec"] = {
            node_x: split["cycle_time_sec"] for node_x, split in splits.items()
        }
        offsets, cycle_frames = (
            coordination_offsets_frames(config, splits) if coordinated else ({}, None)
        )
        if hasattr(signals, "set_coordination"):
            signals.set_coordination(cycle_frames, offsets)
        config["_signal_timing"] = {
            "yellow_sec": round(yellow / 60.0, 2),
            "all_red_sec": round(all_red / 60.0, 2),
            "startup_lost_sec": round(startup_lost, 2),
            "startup_lost_source": startup_source,
            "calibration_excluded_headways": excluded,
            "lost_time_sec": round(lost_time, 2),
            "coordination": "coordinated" if coordinated else "independent",
            "common_cycle_sec": round(cycle_frames / 60.0, 2) if cycle_frames else None,
            "offsets_sec": {str(k): round(v / 60.0, 2) for k, v in offsets.items()},
        }
        cycle_summary = ", ".join(
            f"node {node_x}={split['cycle_time_sec']:.1f}s"
            for node_x, split in splits.items()
        )
        print(
            f"[WEBSTER] S={saturation:.0f} veh/hr  l1={startup_lost:.2f}s  "
            f"Y={yellow / 60:.1f}s AR={all_red / 60:.1f}s  lost={lost_time:.1f}s  "
            f"cycles=({cycle_summary})  offsets={config['_signal_timing']['offsets_sec']}"
        )
        return saturation, splits
    finally:
        config["calibrating"] = False


def perform_full_reset(vehicles, signals, telemetry=None):
    """Restore all per-run simulation state and return the frame-zero value."""
    global _run_exported_workbook
    _run_exported_workbook = False
    gridlock_monitor.reset()
    vehicles.clear()
    signals.reset_all_state()
    if telemetry is not None:
        telemetry.reset_session()
    reset_session_logs()
    network_throughput.update({key: 0 for key in network_throughput})
    network_throughput_at_warmup.clear()
    reset_run_metrics()
    _run_summary_rows.clear()
    control_panel.global_config["_run_uuid"] = str(uuid.uuid4())
    control_panel.global_config["_run_started_wall"] = time.time()
    control_panel.global_config["test_failed_reason"] = ""
    if control_panel.global_config.get("test_running", False):
        # A run's flags come only from decisions made for it: those left on
        # by the previous run's last decision must not carry into it.
        _set_ai_flags(guard.all_off_flags())
    runtime = control_panel.global_config.setdefault("ai_runtime", {})
    runtime["last_turn"] = 0
    # The previous run's decision file would only be refused as
    # FOREIGN_DECISION until this run's first turn lands; remove it so the
    # status honestly reads WAITING_FOR_DECISION, and drop the old status.
    runtime["last_status"] = None
    try:
        DECISION_PATH.unlink()
    except OSError:
        pass
    # A fresh run gets a fresh checkpoint schedule -- only when this reset is
    # starting a timed test; a plain START or a mid-test manual RESET both
    # restart the sim clock at frame zero, so the marks must restart too.
    pending_checkpoints_sec.clear()
    if control_panel.global_config.get("test_running", False):
        pending_checkpoints_sec.extend(
            compute_checkpoint_marks(
                control_panel.global_config.get("test_duration_sim_seconds")
            )
        )
    # The movement engine is fixed for the run before saturation flow is
    # measured, so S (and Webster) describe the engine that will drive.
    vehicle_module.set_movement_model(
        control_panel.global_config.get("movement_model", vehicle_module.MOVEMENT_MODEL_LEGACY)
    )
    # Calibration consumes its own seeded RNG, so traffic generation is
    # reset afterwards and the measured run still starts from the
    # configured seed.
    calibrate_and_apply_webster(signals)
    reset_traffic_generation()
    # The regime is frozen here, after calibration: every checkpoint row of
    # this run reports the same config_hash whatever the panel does later.
    control_panel.global_config["_regime_hash"] = _config_regime_hash(
        control_panel.global_config
    )
    # The setup this run starts from, for the workbook's start sheet: the
    # end-of-run table shows the decider's terminal route flags, not these.
    control_panel.global_config["_initial_input_rows"] = [
        list(row) for row in control_panel_input_rows()
    ]
    global _discharge_was_active
    _discharge_was_active = False
    # Frame zero is returned only after S is locked: calibration is
    # setup, not part of the measured run.
    return 0


LANE_OPTIONS = {
    "EB": [canvas.H_Y - (i + 0.5) * canvas.LANE for i in range(3)],
    "WB": [canvas.H_Y + (i + 0.5) * canvas.LANE for i in range(3)],
    "A_NB": [canvas.INT_X[0] - (i + 0.5) * canvas.LANE for i in range(3)],
    "A_SB": [canvas.INT_X[0] + (i + 0.5) * canvas.LANE for i in range(3)],
    "B_NB": [canvas.INT_X[1] - (i + 0.5) * canvas.LANE for i in range(3)],
    "B_SB": [canvas.INT_X[1] + (i + 0.5) * canvas.LANE for i in range(3)],
}
SPAWN_AT = {
    "EB": ("EB", -20), "WB": ("WB", canvas.WIDTH + 20),
    "A_NB": ("NB", canvas.HEIGHT + 20), "A_SB": ("SB", -20),
    "B_NB": ("NB", canvas.HEIGHT + 20), "B_SB": ("SB", -20),
}
# Discharge edge detection between frames; reset with the run.
_discharge_was_active = False


def merge_live_decision(frame_number):
    """Production's decision source: the guarded decision.json, every 30
    frames while the panel has the agent armed; disarmed, any AI Configured
    plan is withdrawn so the signals fall back to Webster."""
    if frame_number % 30 == 0:
        if control_panel.global_config.get("ai_runtime", {}).get("armed", False):
            merge_ai_decision(signals=_live_signals)
        elif _live_signals is not None and _live_signals.plan_active:
            _live_signals.clear_plan()


def step_simulation(vehicles, signals, frame_number, decide=merge_live_decision):
    """One 60 Hz frame of the simulation -- the single authoritative step,
    run identically by the Tk loop and by headless_run. Order: spawn and
    dispatch, decision merge, signals, vehicles (crossings, completions,
    throughput, bus events), per-frame metrics, warm-up snapshot.

    ``decide(frame_number)`` is the decision source; production reads the
    guarded decision file, a trainer or replay supplies its own. It is
    called at the same point of the frame either way, so a headless
    experiment sees exactly what a windowed run would.
    """
    global _discharge_was_active, _live_signals, _live_vehicles
    _live_signals = signals
    _live_vehicles = vehicles
    _current_frame["n"] = frame_number
    # Last frame's neighbour index is further out of date than its staleness
    # margin covers, and spawning is about to change the list anyway.
    vehicle_module.index_frame(None)
    cfgs = control_panel.approach_configs
    if not is_discharge_demand_suspended(signals):
        if post_discharge_admission_allowed():
            for key, (direction, spawn_coord) in SPAWN_AT.items():
                if cfgs[key]["active"]:
                    try_spawn_vehicle(
                        vehicles, key, direction, spawn_coord, LANE_OPTIONS[key],
                        cfgs[key], signal_controller=signals,
                    )
        if post_discharge_meter_frames_remaining <= 0:
            check_and_dispatch_buses(vehicles, LANE_OPTIONS, 1.0 / 60.0)
    # Every spawn and dispatch for this frame is in, so the neighbour index
    # can be built once and shared by the controller and every vehicle.
    vehicle_module.index_frame(vehicles)
    if decide is not None:
        decide(frame_number)
    signals.update(vehicles=vehicles)
    discharge_is_active = signals.is_discharge_active()
    if _discharge_was_active and not discharge_is_active:
        begin_post_discharge_metering()
    if not discharge_is_active:
        advance_post_discharge_metering()
    _discharge_was_active = discharge_is_active

    active_signal_data = signals.get_all_signals(canvas.INT_X)
    for v in vehicles[:]:
        v.update(
            signal_data=active_signal_data,
            int_x_list=canvas.INT_X,
            h_y=canvas.H_Y,
            road_w=canvas.ROAD_W,
            stop_offset=canvas.STOP,
            lane_w=canvas.LANE,
            all_vehicles=vehicles,
            signal_controller=signals,
        )
        _record_node_crossings(v)
        if v.x < -60 or v.x > canvas.WIDTH + 60 or v.y < -60 or v.y > canvas.HEIGHT + 60:
            if len(getattr(v, "passed_nodes", set())) > 0:
                _record_completed_vehicle_delay(v)
                passengers = int(getattr(v, "passengers", 0))
                network_throughput["passengers_served_total"] += passengers
                network_throughput["vehicles_served_total"] += 1
                if isinstance(v, Bus):
                    network_throughput["passengers_served_bus"] += passengers
                    network_throughput["buses_served"] += 1
                    bus_event_tracker.complete(v, frame_number, signals)
                else:
                    network_throughput["passengers_served_car"] += passengers
                    network_throughput["cars_served"] += 1
            _write_tripinfo(tripinfo_record(v, frame_number, finished=True))
            vehicles.remove(v)
            vehicle_module.drop_from_index(v)
    # Instrumentation only: samples post-update bus state and the
    # controller's live priority requests for this frame.
    bus_event_tracker.observe(vehicles, frame_number, signals)
    accumulate_frame_metrics(vehicles, signals)
    _sample_fcd(vehicles, frame_number)
    snapshot_warmup_baseline(frame_number)


def begin_post_discharge_metering():
    """Start a bounded low-rate admission period after recovery."""
    global post_discharge_meter_frames_remaining
    post_discharge_meter_frames_remaining = POST_DISCHARGE_METER_SECONDS * 60


def post_discharge_admission_allowed():
    """Permit at most one source admission attempt per simulated second."""
    if post_discharge_meter_frames_remaining <= 0:
        return True
    return (
        post_discharge_meter_frames_remaining
        % POST_DISCHARGE_RELEASE_GAP_FRAMES
        == 0
    )


def advance_post_discharge_metering():
    global post_discharge_meter_frames_remaining
    if post_discharge_meter_frames_remaining > 0:
        post_discharge_meter_frames_remaining -= 1


def _reset_state_for_model_change(state, model_type):
    if state["last_model"] == model_type:
        return
    rng = state["rng"]
    state.clear()
    state.update(_new_spawner_state(), rng=rng, last_model=model_type)


def get_demand_telemetry():
    """Return a JSON-safe snapshot of configured and queued source demand."""
    result = {}
    discharge_runtime = control_panel.global_config.get("discharge_runtime", {})
    admission_suspended = bool(discharge_runtime.get("active", False))
    for approach_key, state in spawner_states.items():
        config = control_panel.approach_configs.get(approach_key, {})
        result[approach_key] = {
            "active": bool(config.get("active", False)),
            "model": config.get("model", "Poisson"),
            "configured_rate_vpm": int(config.get("rate", 0)),
            "effective_rate_vpm": round(state["effective_rate_vpm"], 1),
            "peak_active": bool(state["peak_active"]),
            "pending_arrivals": int(state["pending_arrivals"]),
            "requested_arrivals": int(state["requested_arrivals"]),
            "offered_passengers": round(float(state["offered_passengers"]), 3),
            "admitted_arrivals": int(state["admitted_arrivals"]),
            "overflow_arrivals": int(state["overflow_arrivals"]),
            "admission_suspended": admission_suspended,
            "post_recovery_metering": post_discharge_meter_frames_remaining > 0,
        }
    return result


def is_discharge_demand_suspended(signal_controller):
    """Whether recovery owns the network and new demand must remain frozen."""
    return bool(
        signal_controller.is_discharge_active()
        or control_panel.global_config.get("discharge_start_requested", False)
    )

def should_spawn_vehicle(approach_key, model_type, rate_v_m, approach_cfg=None):
    """Advance the source one frame: draw this frame's offers into the
    source queue, then say whether an arrival may enter now."""
    state = spawner_states[approach_key]
    _reset_state_for_model_change(state, model_type)
    state["clock"] += 1
    rng = state["rng"]
    if approach_cfg is None:
        approach_cfg = control_panel.approach_configs.get(approach_key, {})

    if rate_v_m <= 0:
        state["effective_rate_vpm"] = 0.0
        state["peak_active"] = False
        return False

    state["frames_since_spawn"] += 1
    lam_frame = rate_v_m / 3600.0  # Target arrival rate per 1/60s tick

    if model_type == CONGESTION_MODEL:
        peak_frames = CONGESTION_PEAK_SECONDS * 60
        cycle_frames = peak_frames + CONGESTION_RECOVERY_SECONDS * 60
        cycle_position = state["congestion_cycle_frame"] % cycle_frames
        state["peak_active"] = cycle_position < peak_frames
        state["congestion_cycle_frame"] += 1

        effective_rate = (
            max(
                float(rate_v_m) * CONGESTION_RATE_MULTIPLIER,
                CONGESTION_MIN_PEAK_RATE_VPM,
            )
            if state["peak_active"]
            else float(rate_v_m)
        )
        state["effective_rate_vpm"] = effective_rate
        arrival_probability = 1.0 - math.exp(-effective_rate / 3600.0)
        if rng.random() < arrival_probability:
            _record_demand_offer(approach_key, state, 1, approach_cfg)

    elif model_type == "Neg Binomial":
        state["effective_rate_vpm"] = float(rate_v_m)
        state["peak_active"] = False
        MEAN_BURST_SIZE = 2.0
        p_burst_start = lam_frame / MEAN_BURST_SIZE

        if rng.random() < p_burst_start:
            _record_demand_offer(approach_key, state, rng.randint(1, 3), approach_cfg)

        # Bursts release one vehicle every 18 frames.
        if state["arrivals"] and state["frames_since_spawn"] >= 18:
            state["frames_since_spawn"] = 0
            return True
        return False

    elif model_type == "Binomial":
        state["effective_rate_vpm"] = float(rate_v_m)
        state["peak_active"] = False
        min_gap_frames = int(1800.0 / rate_v_m) if rate_v_m > 0 else 999
        p_metered = lam_frame * 2.0
        if state["frames_since_spawn"] >= min_gap_frames and rng.random() < p_metered:
            _record_demand_offer(approach_key, state, 1, approach_cfg)
            state["frames_since_spawn"] = 0

    elif model_type == "Poisson":
        state["effective_rate_vpm"] = float(rate_v_m)
        state["peak_active"] = False
        prob = 1.0 - math.exp(-lam_frame)
        if rng.random() < prob:
            _record_demand_offer(approach_key, state, 1, approach_cfg)
            state["frames_since_spawn"] = 0

    else:
        state["effective_rate_vpm"] = float(rate_v_m)
        state["peak_active"] = False
        if rng.random() < lam_frame:
            _record_demand_offer(approach_key, state, 1, approach_cfg)
            state["frames_since_spawn"] = 0

    # A blocked entry does not consume the arrival: it waits in the source
    # queue and try_spawn_vehicle() dequeues it only after adding a vehicle.
    return bool(state["arrivals"])


def try_spawn_vehicle(
    vehicles,
    approach_key,
    direction,
    spawn_coord,
    lane_coords,
    approach_cfg,
    min_gap=40,
    signal_controller=None,
):
    model_type = approach_cfg.get("model", "Poisson")
    rate_v_m = approach_cfg.get("rate", 12)

    if not should_spawn_vehicle(approach_key, model_type, rate_v_m, approach_cfg):
        return

    state = spawner_states[approach_key]

    # Each lane admits its own arrivals, in order. One queue for the whole
    # approach let the head arrival's blocked lane hold back arrivals bound
    # for free lanes: at 0.6x demand the head was blocked on 100 % of pending
    # frames and a free lane was wanted behind it 29-99 % of that time
    # (2026-09-23 audit). Arrival attributes are fixed at offer time, so the
    # offered schedule and common random numbers are untouched.
    # Entry clearance: the lateral window is a vehicle width, not a
    # lane-centre tolerance, so a vehicle mid-slide between lanes still
    # blocks the lane it is straddling.
    blocked = set()
    for index, lane_coord in enumerate(lane_coords):
        for v in vehicles:
            if v.direction != direction:
                continue
            along, cross = (v.x, v.y) if direction in ("EB", "WB") else (v.y, v.x)
            if abs(along - spawn_coord) < min_gap and abs(cross - lane_coord) < SPAWN_LATERAL_BLOCK_PX:
                blocked.add(index)
                break
    if len(blocked) >= len(lane_coords):
        return
    # The outer horizontal lane is the general left-turn lane and the DBL
    # lane. Once DBL is active it is reserved for the priority bus: retain a
    # new left-turn arrival at the source instead of immediately refilling a
    # lane that the controller is unconditionally clearing.
    hold_lefts = False
    if direction in ("EB", "WB") and signal_controller:
        target_node_x = canvas.INT_X[0] if direction == "EB" else canvas.INT_X[-1]
        hold_lefts = signal_controller.dbl_excludes_left_turns(target_node_x, direction)
    # ponytail: linear scan of the source queue; long only when demand is
    # already held at the boundary, and it stops at the first admissible one.
    pick = next(
        (
            index for index, candidate in enumerate(state["arrivals"])
            if candidate["lane_idx"] not in blocked
            and not (hold_lefts and candidate["target_turn"] == "LEFT")
        ),
        None,
    )
    if pick is None:
        return
    arrival = state["arrivals"][pick]
    del state["arrivals"][pick]
    target_turn, left_nodes, lane_idx = (
        arrival["target_turn"], arrival["left_nodes"], arrival["lane_idx"]
    )
    target_lane_coord = lane_coords[lane_idx]

    state["pending_arrivals"] = max(0, state["pending_arrivals"] - 1)
    state["pending_pax"] = max(0.0, state["pending_pax"] - (1.0 if arrival["is_heavy"] else 4.0))
    if model_type == CONGESTION_MODEL:
        state["frames_since_spawn"] = 0
    is_heavy, speed, color = arrival["is_heavy"], arrival["speed"], arrival["color"]

    if direction in ("EB", "WB"):
        vehicles.append(Vehicle(
            x=spawn_coord, y=target_lane_coord, direction=direction,
            max_speed=speed, color=color, is_heavy=is_heavy,
            target_turn=target_turn, lane_index=lane_idx, left_nodes=left_nodes,
        ))
    else:
        assigned_node_x = min(canvas.INT_X, key=lambda node_x: abs(node_x - target_lane_coord))
        vehicles.append(Vehicle(
            x=target_lane_coord, y=spawn_coord, direction=direction,
            max_speed=speed, color=color, is_heavy=is_heavy,
            target_turn=target_turn, lane_index=lane_idx,
            assigned_node_x=assigned_node_x, left_nodes=left_nodes,
        ))
    admitted = vehicles[-1]
    admitted.depart_frame = _current_frame["n"]
    admitted.behaviour_rng = behaviour_stream(arrival.get("identity", ""))
    # Frames this vehicle waited at the source because the entry was full --
    # SUMO's departDelay, reported per trip and in the delay DV.
    admitted.entry_delay_frames = max(0, int(state["clock"]) - int(arrival["frame"]))
    _settle_insertion_speed(admitted, vehicles)
    state["admitted_arrivals"] += 1


def _settle_insertion_speed(vehicle, vehicles):
    """Enter at the safe speed for the gap ahead, never full desired speed
    into a queue (vehicle.safe_insertion_speed)."""
    gap, leader = vehicle.get_lead_vehicle(vehicles)
    vehicle.speed = min(vehicle.speed, vehicle_module.safe_insertion_speed(vehicle, gap, leader))


# Trips dispatched per route this run. The k-th bus of a route is the k-th
# scheduled trip whatever the controller did (trips leave in order), so its
# dwell draw is keyed on it and every arm of a seed sees identical dwells.
_route_trip_index = {}


def _poisson(rng, mean):
    """Knuth's Poisson sampler; small means only (boardings per stop)."""
    limit, count, product = math.exp(-max(0.0, float(mean))), 0, rng.random()
    while product > limit:
        count += 1
        product *= rng.random()
    return count


def dwell_frames_for(rng, params):
    """TCQSM dwell: door time + passenger service (Ch. 6). With two or more
    doors boarding and alighting run in parallel and the longer governs."""
    boardings = _poisson(rng, params.get("mean_boardings", 4.0))
    alightings = _poisson(rng, params.get("mean_alightings", 4.0))
    board = boardings * float(params.get("board_sec_per_pax", 3.0))
    alight = alightings * float(params.get("alight_sec_per_pax", 2.0))
    service = max(board, alight) if int(params.get("doors", 2)) >= 2 else board + alight
    return int(round((float(params.get("door_time_sec", 4.0)) + service) * 60))


def behaviour_stream(identity):
    """A vehicle's own behavioural random stream, keyed on (run seed,
    exogenous identity) -- see Vehicle.behaviour_rng."""
    seed = control_panel.global_config.get("random_seed")
    return random.Random(f"{seed}:{identity}:behaviour")


def draw_stop_plan(route_id, route_cfg):
    """The dwell at every stop of this trip, drawn from a stream keyed on
    (run seed, route, trip index): exogenous, like the demand streams."""
    index = _route_trip_index.get(route_id, 0)
    _route_trip_index[route_id] = index + 1
    seed = control_panel.global_config.get("random_seed")
    rng = random.Random(f"{seed}:{route_id}:{index}:dwell")
    params = control_panel.global_config.get("bus_dwell") or {}
    return [
        {"node": int(stop["node"]), "side": stop.get("side", "far"),
         "dwell_frames": dwell_frames_for(rng, params), "served": False, "dwelt": 0}
        for stop in route_cfg.get("stops", []) or []
    ]


def check_and_dispatch_buses(vehicles, lane_options, dt):
    global bus_sequence_counter, bus_schedule_time_s

    bus_schedule_time_s += max(0.0, dt)

    for r_id, r_cfg in control_panel.bus_routes_config.items():
        if not r_cfg["active"]:
            r_cfg["manual_dispatch"] = False
            bus_dispatch_counters[r_id] = 0.0  # Reset headway retention when inactive
            bus_pending_trips[r_id].clear()
            continue

        pending = bus_pending_trips[r_id]
        if r_cfg["manual_dispatch"]:
            pending.append({"scheduled_time_s": bus_schedule_time_s, "manual": True, "missed": False})
            run_metrics["bus_trips_scheduled"] += 1
            r_cfg["manual_dispatch"] = False
            # Manual departures restart the automatic headway clock.
            bus_dispatch_counters[r_id] = 0.0
        if r_cfg["headway_sec"] > 0:
            bus_dispatch_counters[r_id] += dt
            headway = float(r_cfg["headway_sec"])
            while bus_dispatch_counters[r_id] + 1e-9 >= headway:
                bus_dispatch_counters[r_id] -= headway
                pending.append({
                    "scheduled_time_s": bus_schedule_time_s - bus_dispatch_counters[r_id],
                    "manual": False, "missed": False,
                })
                run_metrics["bus_trips_scheduled"] += 1
        else:
            # Headway OFF means a fresh interval begins when re-enabled.
            bus_dispatch_counters[r_id] = 0.0

        if pending:
            direction = r_cfg["origin"]
            spawn_coord = -40 if direction == "EB" else canvas.WIDTH + 40
            
            waypoints = r_cfg.get("waypoints", {})
            first_node_x = canvas.INT_X[0] if direction == "EB" else canvas.INT_X[-1]
            first_node_turn = waypoints.get(first_node_x, "STRAIGHT")
            lane_idx = 2 if first_node_turn == "LEFT" else 1

            target_lane_coord = lane_options[direction][lane_idx]

            # ENTRY CLEARANCE CHECK
            entry_blocked = False
            for v in vehicles:
                if v.direction == direction:
                    same_lane = (
                        abs(v.y - target_lane_coord) < SPAWN_LATERAL_BLOCK_PX
                        if direction in ("EB", "WB")
                        else abs(v.x - target_lane_coord) < SPAWN_LATERAL_BLOCK_PX
                    )
                    longitudinal_gap = (
                        abs(v.x - spawn_coord)
                        if direction in ("EB", "WB")
                        else abs(v.y - spawn_coord)
                    )
                    if same_lane and longitudinal_gap < 60:
                        entry_blocked = True
                        break

            if entry_blocked:
                for trip in pending:
                    if not trip["missed"]:
                        trip["missed"] = True
                        run_metrics["bus_trips_missed"] += 1
                continue

            trip = pending.popleft()
            source_delay = max(0.0, bus_schedule_time_s - trip["scheduled_time_s"])
            run_metrics["bus_source_delay_sec"] += source_delay
            bus_sequence_counter += 1
            unique_bus_id = f"BUS_{r_id}_{bus_sequence_counter:03d}"

            route_info = {
                "route_id": r_id,
                "origin": direction,
                "destination": r_cfg["destination"],
                "waypoints": waypoints,
                "lanes": dict(r_cfg.get("lanes", {})),
                "scheduled_departure_s": trip["scheduled_time_s"],
                "actual_departure_s": bus_schedule_time_s,
                "source_delay_s": source_delay,
                "pending_trips_at_departure": len(pending),
                "missed_trips_to_date": run_metrics["bus_trips_missed"],
                "trip_identity": f"{r_id}:{_route_trip_index.get(r_id, 0)}",
                "stops": draw_stop_plan(r_id, r_cfg),
            }

            vehicles.append(Bus(
                x=spawn_coord, y=target_lane_coord, direction=direction,
                route_info=route_info, bus_id=unique_bus_id,
                max_speed=1.0 * get_active_vehicle_speed_scale(),
            ))
            vehicles[-1].entry_delay_frames = int(round(source_delay * 60))
            vehicles[-1].depart_frame = _current_frame["n"]
            vehicles[-1].behaviour_rng = behaviour_stream(route_info["trip_identity"])
            _settle_insertion_speed(vehicles[-1], vehicles)
            run_metrics["buses_spawned"] += 1


# --- Unified single-window shell --------------------------------------------
# One process now owns exactly one tk.Tk(): the control panel and telemetry
# dashboard, previously each a standalone window (the latter in its own OS
# process), mount into panes of this same root instead of creating their own.
MAIN_WINDOW_TITLE = "Traffic Simulator"
# Portrait side panes, each a fraction of the screen width bounded so it is
# never crushed on a small screen or absurdly wide on a huge one. The two
# are sized separately: the control panel is a single column of stacked
# controls and needs only enough width for one slider or one button row,
# while the telemetry dashboard carries a 3 x 3 KPI grid, the phase-cycle
# strip and two node diagrams side by side, so it keeps ~30% more.
CONTROL_PANE_WIDTH_FRACTION = 0.10
MIN_CONTROL_PANE_WIDTH = 256
MAX_CONTROL_PANE_WIDTH = 320
TELEMETRY_PANE_WIDTH_FRACTION = 0.13
MIN_TELEMETRY_PANE_WIDTH = 332
MAX_TELEMETRY_PANE_WIDTH = 420
# Breathing room between the simulation canvas's hairline frame and the
# sashes either side of it. The window is sized so the center pane is exactly
# the canvas plus this gutter -- any wider and the fixed-size network just
# floats in empty background.
SIMULATION_PANE_GUTTER = control_panel.SPACE_MD
# The window is laid out for this display width of the physics surface; a
# wider surface is smoothscaled down into it (see build_simulation_canvas)
# rather than forcing a wider window.
CANVAS_DISPLAY_WIDTH = min(canvas.WIDTH, 1000)


def control_pane_width_for(screen_width):
    """Control-panel column width for this screen, bounded by the constants above."""
    return max(
        MIN_CONTROL_PANE_WIDTH,
        min(MAX_CONTROL_PANE_WIDTH, round(screen_width * CONTROL_PANE_WIDTH_FRACTION)),
    )


def telemetry_pane_width_for(screen_width):
    """Telemetry-dashboard column width for this screen, bounded likewise."""
    return max(
        MIN_TELEMETRY_PANE_WIDTH,
        min(MAX_TELEMETRY_PANE_WIDTH, round(screen_width * TELEMETRY_PANE_WIDTH_FRACTION)),
    )


def main_window_width_for(screen_width):
    """Window width that fits both side panes and the canvas with no slack.

    Bounded by the screen: on a display too narrow for all three, the
    center pane simply gets less and clips the canvas, as before.
    """
    fitted = (
        control_pane_width_for(screen_width)
        + telemetry_pane_width_for(screen_width)
        + CANVAS_DISPLAY_WIDTH + 2 * SIMULATION_PANE_GUTTER
    )
    return max(900, min(screen_width - 60, fitted))


def main_window_height_for(screen_height):
    """Window height that frames the canvas with the same gutter on every side.

    The canvas is the one fixed-size element; the two side columns scroll,
    and the telemetry dashboard lays itself out to the viewport it gets, so
    nothing else needs the window taller than the network plus its gutter.
    """
    return min(screen_height - 100, canvas.HEIGHT + 2 * SIMULATION_PANE_GUTTER)


# --- Window shapes -----------------------------------------------------------
# Three sizes the operator cycles through from the control-panel header (the
# OS maximize/restore buttons land on the same three). "compact" is the
# fitted window above; "large" is a centred window of ~75% of the screen;
# "maximized" is the OS zoomed state. In the two bigger shapes the side
# panes widen to their maxima. In every shape the whole network is scaled
# (aspect kept) to fill the centre pane minus the same gutter; the wheel
# zoom looks closer.
WINDOW_SHAPES = ("compact", "large", "maximized")
LARGE_WINDOW_SCREEN_FRACTION = 0.75
# A window this close to the compact size (after the OS restores it, say)
# still counts as compact.
COMPACT_SHAPE_TOLERANCE_PX = 8
# Below the taskbar and title bar the OS keeps for itself.
LARGE_WINDOW_SCREEN_MARGIN_Y = 80
# A pushed frame costs Tk about 14 ms per megapixel to paint (PhotoImage
# blit, measured), so frames above this many pixels are pushed every other
# tick (~30 Hz) to leave the 60 Hz physics its budget; the compact and
# large shapes (0.25-0.45 MP) stay at the full rate, maximized on a 1080p
# screen (~0.9 MP) halves. See build_simulation_canvas.
FULL_RATE_PUSH_MAX_PIXELS = 600_000
# Ticks per painted frame while an unattended batch sweep is running: at
# TICK_MS this is ~4 fps, enough to watch progress, and it hands the render
# side's share of the wall clock back to the fixed-step loop.
BATCH_RENDER_EVERY = 15
# Simulation tick period (physics catch-up, draw, push), measured from the
# tick's start so the work inside it does not lengthen the period.
TICK_MS = 16
# Mouse-wheel view zoom ceiling: 6x shows a 400x100 px window of the world,
# one node and its approaches at ~3x display size.
MAX_VIEW_ZOOM = 6.0


def side_pane_widths_for(shape, screen_width):
    """(control, telemetry) pane widths for a shape on this screen."""
    if shape == "compact":
        return control_pane_width_for(screen_width), telemetry_pane_width_for(screen_width)
    return MAX_CONTROL_PANE_WIDTH, MAX_TELEMETRY_PANE_WIDTH


def large_window_geometry_for(screen_width, screen_height):
    """(width, height, x, y) of the Large shape: ~75% of the screen, centred.

    Never narrower than the widened side panes plus the native canvas, so
    on a screen where 75% is less than that (1080p) Large is at least as
    wide as compact and only gains height.
    """
    min_width = (
        MAX_CONTROL_PANE_WIDTH + MAX_TELEMETRY_PANE_WIDTH
        + CANVAS_DISPLAY_WIDTH + 2 * SIMULATION_PANE_GUTTER
    )
    width = min(
        screen_width - 60,
        max(round(screen_width * LARGE_WINDOW_SCREEN_FRACTION), min_width),
    )
    usable_height = screen_height - LARGE_WINDOW_SCREEN_MARGIN_Y
    height = min(
        usable_height,
        max(
            round(screen_height * LARGE_WINDOW_SCREEN_FRACTION),
            canvas.HEIGHT + 2 * SIMULATION_PANE_GUTTER,
        ),
    )
    x = max(0, (screen_width - width) // 2)
    y = max(0, (usable_height - height) // 2)
    return width, height, x, y


# Width step that keeps the scaled height an exact integer at the surface's
# aspect ratio (5 for 1000x600, 4 for 2400x600).
CANVAS_ASPECT_STEP = canvas.WIDTH // math.gcd(canvas.WIDTH, canvas.HEIGHT)


def fit_canvas_size(available_width, available_height):
    """Largest integer canvas size at the surface's aspect ratio that fits
    the available area.

    The width is a multiple of CANVAS_ASPECT_STEP so the height is an exact
    integer, and never below a quarter of native so a transiently tiny pane
    (mid-resize) cannot request a degenerate image.
    """
    step = CANVAS_ASPECT_STEP
    floor_width = canvas.WIDTH // 4 - (canvas.WIDTH // 4) % step
    scale = min(
        max(0.0, float(available_width)) / canvas.WIDTH,
        max(0.0, float(available_height)) / canvas.HEIGHT,
    )
    width = int(canvas.WIDTH * scale)
    width -= width % step
    width = max(floor_width, width)
    return width, width * canvas.HEIGHT // canvas.WIDTH


def classify_window_shape(state, width, height, compact_size):
    """Which shape a live window is in, from its OS state and size.

    Used after any resize so an OS maximize/restore (or a drag) is reflected
    in the shape state and the header button, not only the panel's own
    cycle button.
    """
    if state == "zoomed":
        return "maximized"
    compact_width, compact_height = compact_size
    if (
        abs(int(width) - compact_width) <= COMPACT_SHAPE_TOLERANCE_PX
        and abs(int(height) - compact_height) <= COMPACT_SHAPE_TOLERANCE_PX
    ):
        return "compact"
    return "large"


class WindowShapeController:
    """Owns the window's shape: size, side-pane widths and canvas scale.

    ``apply``/``cycle`` change the OS window; everything that depends on the
    resulting size (sash positions, canvas target size) happens later in
    ``_sync``, which the root's ``<Configure>`` event schedules once the
    window has actually been resized -- calling ``sashpos`` before that
    would act on the old geometry. ``_sync`` also runs for resizes the
    operator makes with the OS buttons or by dragging, so the canvas always
    fits whatever the centre pane currently is.
    """

    SYNC_DEBOUNCE_MS = 60

    def __init__(
        self, root, paned, control_outer, telemetry_outer, simulation_pane,
        screen_width, screen_height,
    ):
        self.root = root
        self.paned = paned
        self.control_outer = control_outer
        self.telemetry_outer = telemetry_outer
        self.simulation_pane = simulation_pane
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.shape = "compact"
        self._compact_size = (
            main_window_width_for(screen_width), main_window_height_for(screen_height)
        )
        self._pending_pane_widths = None
        self._fit_canvas = None
        self._after_id = None
        root.bind("<Configure>", self._on_root_configure, add="+")
        simulation_pane.bind("<Configure>", self._on_pane_configure, add="+")
        root.bind("<Destroy>", self._on_root_destroy, add="+")

    # --- operator-facing -------------------------------------------------
    def cycle(self):
        index = WINDOW_SHAPES.index(self.shape)
        self.apply(WINDOW_SHAPES[(index + 1) % len(WINDOW_SHAPES)])

    def apply(self, shape):
        if shape not in WINDOW_SHAPES:
            raise ValueError(f"unknown window shape {shape!r}")
        self.shape = shape
        control_panel.global_config["window_shape"] = shape
        self._pending_pane_widths = side_pane_widths_for(shape, self.screen_width)
        if shape == "maximized":
            self.root.state("zoomed")
            return
        if self.root.state() == "zoomed":
            self.root.state("normal")
        if shape == "compact":
            width, height = self._compact_size
            x = max(0, (self.screen_width - width) // 2)
            y = max(0, (self.screen_height - LARGE_WINDOW_SCREEN_MARGIN_Y - height) // 2)
        else:
            width, height, x, y = large_window_geometry_for(
                self.screen_width, self.screen_height
            )
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def attach_canvas(self, set_target_size):
        """Register the canvas resizer (build_simulation_canvas's) and fit it."""
        self._fit_canvas = set_target_size
        self._schedule_sync()

    # --- resize plumbing ---------------------------------------------------
    def _on_root_configure(self, event):
        # A root binding receives every descendant's Configure too.
        if event.widget is not self.root:
            return
        self._schedule_sync()

    def _on_pane_configure(self, _event):
        self._schedule_sync()

    def _on_root_destroy(self, event):
        """Drop the debounced resize timer with the window it describes.

        A queued `after` outlives the widget that registered its callback, so
        without this the timer fires into a torn-down window and Tcl reports
        an invalid command name from its background error handler.
        """
        if event.widget is not self.root:
            return
        self._cancel_pending_sync()

    def _cancel_pending_sync(self):
        if self._after_id is None:
            return
        try:
            self.root.after_cancel(self._after_id)
        except Exception:
            # The interpreter is already gone; the timer went with it.
            pass
        self._after_id = None

    def _schedule_sync(self):
        self._cancel_pending_sync()
        self._after_id = self.root.after(self.SYNC_DEBOUNCE_MS, self._sync)

    def _sync(self):
        self._after_id = None
        state = self.root.state()
        width, height = self.root.winfo_width(), self.root.winfo_height()
        if state == "iconic" or width < 100:
            return
        detected = classify_window_shape(state, width, height, self._compact_size)
        if detected != self.shape:
            # The OS (or a drag) changed the window under us. Pane widths
            # follow a maximize/restore; a free drag keeps whatever sashes
            # the operator has, it only refits the canvas.
            if detected == "maximized" or self.shape == "maximized":
                self._pending_pane_widths = side_pane_widths_for(
                    detected, self.screen_width
                )
            self.shape = detected
            control_panel.global_config["window_shape"] = detected
        if self._pending_pane_widths is not None:
            pending, self._pending_pane_widths = self._pending_pane_widths, None
            self._apply_pane_widths(*pending)
        self._fit()

    def _apply_pane_widths(self, control_width, telemetry_width):
        """Move both sashes so the side panes take exactly these widths.

        ttk sashes keep their absolute positions across window resizes, so
        restoring from maximized would otherwise leave the widened panes in
        place and squeeze the canvas.
        """
        self.control_outer.configure(width=control_width)
        self.telemetry_outer.configure(width=telemetry_width)
        self.root.update_idletasks()
        paned_width = self.paned.winfo_width()
        if paned_width <= 1:
            return
        # Sash thickness as Tk actually draws it, not a guess.
        sash_width = max(
            0, paned_width - self.paned.sashpos(1) - self.telemetry_outer.winfo_width()
        )
        self.paned.sashpos(0, control_width)
        self.paned.sashpos(1, paned_width - telemetry_width - sash_width)

    def _fit(self):
        if self._fit_canvas is None:
            return
        # Every shape, compact included, shows the whole network scaled to
        # the centre pane (compact is laid out for CANVAS_DISPLAY_WIDTH, less
        # than the 2400 px surface); the wheel zoom is how to look closer.
        # Hairline frame (1px each side) plus the gutter all round.
        available_width = self.simulation_pane.winfo_width() - 2 * SIMULATION_PANE_GUTTER - 2
        available_height = self.simulation_pane.winfo_height() - 2 * SIMULATION_PANE_GUTTER - 2
        self._fit_canvas(*fit_canvas_size(available_width, available_height))


def build_scrollable_pane(parent, width):
    """A fixed-width, vertically scrollable container for one side pane.

    Mirrors the Canvas + Scrollbar + inner-frame pattern
    TelemetryDashboard.create_scrollable_tab already uses for its own tabs,
    generalized here to host an entire mounted component (the control panel
    or the telemetry dashboard) instead of one dashboard tab.

    Returns (outer_frame, content_frame, scroll_canvas). Pack widgets into
    `content_frame`; `outer_frame` is what the caller adds to the
    PanedWindow.
    """
    outer = tk.Frame(parent, width=width)
    outer.pack_propagate(False)

    scroll_canvas = tk.Canvas(
        outer, width=width, highlightthickness=0, bg=control_panel.COLOR_BG,
    )
    scrollbar = ttk.Scrollbar(
        outer, orient="vertical", command=scroll_canvas.yview
    )
    scroll_canvas.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    scroll_canvas.pack(side="left", fill="both", expand=True)

    content = tk.Frame(scroll_canvas, bg=control_panel.COLOR_BG)
    content_window = scroll_canvas.create_window(
        (0, 0), window=content, anchor="nw"
    )

    # A 1px-wide spacer whose height follows the viewport gives the content
    # frame a natural height of at least the visible pane, so a mounted
    # component that packs with expand=True (the telemetry notebook) fills
    # the pane top to bottom. Natural sizing is untouched: taller content
    # still fires <Configure> and scrolls.
    min_height_spacer = tk.Frame(content, width=1, height=1, bg=control_panel.COLOR_BG)
    min_height_spacer.pack(side="right", fill="y")

    def refresh_scroll_region(_event=None):
        scroll_canvas.configure(scrollregion=scroll_canvas.bbox("all"))

    def fit_content_width(event):
        scroll_canvas.itemconfigure(content_window, width=max(1, event.width))
        min_height_spacer.configure(height=max(1, event.height))

    content.bind("<Configure>", refresh_scroll_region, add="+")
    scroll_canvas.bind("<Configure>", fit_content_width, add="+")

    return outer, content, scroll_canvas


def bind_pane_mousewheel(widget, canvas_widget):
    """Recursively bind wheel scrolling to a pane and every widget inside it.

    Tk delivers <MouseWheel> to whichever widget is directly under the
    pointer, not to an ancestor, so binding only the outer scroll canvas
    would silently miss every scroll gesture made over a button, label, or
    slider sitting on top of it. This walks the already-built widget tree and
    binds each one directly, the same recursive approach
    TelemetryDashboard.bind_tab_mousewheel uses for its own tabs -- as
    opposed to a global bind_all, which would make two independently
    scrolling panes fight over one shared binding.
    """
    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        widget.bind(
            sequence,
            lambda event, c=canvas_widget: TelemetryDashboard.scroll_tab_with_wheel(
                event, c
            ),
            add="+",
        )
    for child in widget.winfo_children():
        bind_pane_mousewheel(child, canvas_widget)


def build_main_window():
    """Construct the single Tk root and its three-pane layout.

    Left = control panel, center = simulation canvas, right = telemetry.
    The center pane claims any extra resize space (weight=1); the side panes
    hold their configured width (weight=0) and scroll vertically for
    whatever overflows it. This commit builds only the empty shell -- the
    control panel and telemetry dashboard mount into their panes in later
    checkpoints, and the simulation canvas gets its pygame-fed image later
    still.
    """
    root = tk.Tk()
    root.title(MAIN_WINDOW_TITLE)
    root.configure(bg=control_panel.COLOR_BG)

    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    # Both dimensions are fitted to the canvas (plus the side panes across,
    # plus one gutter all round) rather than to the screen, so the fixed-size
    # network is framed by the same margin on every side instead of floating
    # in empty background on a large monitor.
    window_width = main_window_width_for(screen_width)
    window_height = main_window_height_for(screen_height)
    root.geometry(f"{window_width}x{window_height}")

    paned = ttk.PanedWindow(root, orient="horizontal")
    paned.pack(fill="both", expand=True)

    control_outer, control_pane, control_scroll = build_scrollable_pane(
        paned, control_pane_width_for(screen_width)
    )
    simulation_pane = tk.Frame(paned, bg=control_panel.COLOR_BG)
    telemetry_outer, telemetry_pane, telemetry_scroll = build_scrollable_pane(
        paned, telemetry_pane_width_for(screen_width)
    )
    # Stashed on the pane itself rather than widening this function's return
    # signature: whoever later mounts real content into control_pane or
    # telemetry_pane needs this canvas to wire up bind_pane_mousewheel, but
    # nothing in this checkpoint's empty-shell commit does yet.
    control_pane.scroll_canvas = control_scroll
    telemetry_pane.scroll_canvas = telemetry_scroll

    # Each side pane's own requested width (frozen at its *_pane_width_for by
    # pack_propagate(False) above) is what the PanedWindow sizes it to
    # automatically once mapped; weight=1 on the center pane then gives it
    # everything left over. No sashpos() call is made here -- one made before
    # the window is first mapped collapses the side panes to near zero width.
    # WindowShapeController moves the sashes only from its post-map,
    # <Configure>-driven sync when a shape change needs different widths.
    paned.add(control_outer, weight=0)
    paned.add(simulation_pane, weight=1)
    paned.add(telemetry_outer, weight=0)

    # Extra handles ride on the root so the 4-tuple return (which tests and
    # main() depend on) stays as it is.
    root.paned = paned
    root.window_shape = WindowShapeController(
        root, paned, control_outer, telemetry_outer, simulation_pane,
        screen_width, screen_height,
    )

    return root, control_pane, simulation_pane, telemetry_pane


def build_simulation_canvas(parent):
    """A Tk Canvas fed by one PhotoImage, mutated every push.

    The offscreen surface blit method (task-specified): one PhotoImage is
    created once and itemconfig'd back onto the same canvas image item on
    every push -- never recreated -- so Tk redraws in place without ever
    losing the reference mid-frame. Created at the network's native
    canvas.WIDTH x canvas.HEIGHT; ``canvas_widget.set_target_size(w, h)``
    (used by WindowShapeController) resizes the canvas and PhotoImage in
    place and makes every later push scale the 1000x600 surface to that
    size with pygame's own smoothscale -- the physics surface and geometry
    constants never change, only the pixels handed to Tk.

    Returns (canvas_widget, push_frame), where push_frame(surface) converts
    one pygame Surface to PPM bytes and writes it into the same PhotoImage.
    """
    # The pane shares the side panes' background; the canvas sits in a
    # hairline frame so the network reads as one framed view rather than a
    # black rectangle floating on black.
    frame = tk.Frame(
        parent, bg=control_panel.COLOR_BG,
        highlightthickness=1, highlightbackground=control_panel.COLOR_CARD_BORDER,
    )
    # expand=True without fill: the fixed-size canvas sits centered in the
    # pane. The window is sized so that centering leaves exactly one gutter
    # on every side (build_main_window); if the operator drags the window
    # larger the margins simply grow equally.
    frame.pack(expand=True)
    simulation_canvas = tk.Canvas(
        frame, width=canvas.WIDTH, height=canvas.HEIGHT,
        bg="black", highlightthickness=0,
    )
    simulation_canvas.pack()
    photo = tk.PhotoImage(width=canvas.WIDTH, height=canvas.HEIGHT)
    image_item = simulation_canvas.create_image(0, 0, anchor="nw", image=photo)
    native_size = (canvas.WIDTH, canvas.HEIGHT)

    # Per-target state, rebuilt only when the target size changes: PPM header,
    # a reused destination surface for smoothscale (None at native size), and
    # how many pushes to skip. A push costs roughly its pixel count (Tk's
    # paint dominates), so past FULL_RATE_PUSH_MAX_PIXELS only every other
    # push is written, keeping the 60Hz physics loop and the Tk thread
    # responsive.
    state = {
        "target": native_size,
        "header": f"P6 {canvas.WIDTH} {canvas.HEIGHT} 255 ".encode("ascii"),
        "scaled": None,
        "push_every": 1,
        "pushes": 0,
        # View zoom: display only. A zoomed push crops a WIDTH/zoom x
        # HEIGHT/zoom window of the physics surface around (cx, cy) before
        # scaling, so the world, the telemetry and the agent never see it.
        "zoom": 1.0,
        "cx": canvas.WIDTH / 2.0,
        "cy": canvas.HEIGHT / 2.0,
        "drag": None,
    }

    def view_rect():
        zoom = state["zoom"]
        w, h = canvas.WIDTH / zoom, canvas.HEIGHT / zoom
        state["cx"] = min(max(state["cx"], w / 2.0), canvas.WIDTH - w / 2.0)
        state["cy"] = min(max(state["cy"], h / 2.0), canvas.HEIGHT - h / 2.0)
        return pygame.Rect(
            int(state["cx"] - w / 2.0), int(state["cy"] - h / 2.0), int(w), int(h)
        )

    def set_zoom(zoom, at=None):
        """Zoom the view; ``at`` is a (canvas_x, canvas_y) pixel to keep fixed."""
        zoom = min(max(float(zoom), 1.0), MAX_VIEW_ZOOM)
        if at is not None:
            # World point under the cursor stays under the cursor.
            rect = view_rect()
            fx, fy = at[0] / state["target"][0], at[1] / state["target"][1]
            wx, wy = rect.x + fx * rect.w, rect.y + fy * rect.h
            w, h = canvas.WIDTH / zoom, canvas.HEIGHT / zoom
            state["cx"], state["cy"] = wx - (fx - 0.5) * w, wy - (fy - 0.5) * h
        state["zoom"] = zoom
        if zoom == 1.0:
            state["cx"], state["cy"] = canvas.WIDTH / 2.0, canvas.HEIGHT / 2.0
        state["scaled"] = (
            None if zoom == 1.0 and state["target"] == native_size
            else pygame.Surface(state["target"])
        )

    def on_wheel(event):
        step = 1.25 if event.delta > 0 else 1 / 1.25
        set_zoom(state["zoom"] * step, at=(event.x, event.y))

    def on_drag(event):
        if state["drag"] is not None:
            dx, dy = event.x - state["drag"][0], event.y - state["drag"][1]
            px = canvas.WIDTH / state["zoom"] / state["target"][0]
            state["cx"] -= dx * px
            state["cy"] -= dy * px
        state["drag"] = (event.x, event.y)

    # Bound on the canvas widget itself, so they hold in every window shape
    # (compact, large, maximized): the shape controller only resizes it.
    # Left or middle button pans.
    simulation_canvas.bind("<MouseWheel>", on_wheel)
    for button in (1, 2):
        simulation_canvas.bind(f"<ButtonPress-{button}>", on_drag)
        simulation_canvas.bind(f"<B{button}-Motion>", on_drag)
        simulation_canvas.bind(
            f"<ButtonRelease-{button}>", lambda e: state.update(drag=None)
        )
    simulation_canvas.bind("<Double-Button-1>", lambda e: set_zoom(1.0))

    def set_target_size(width, height):
        target = (int(width), int(height))
        if target == state["target"]:
            return
        state["target"] = target
        simulation_canvas.configure(width=target[0], height=target[1])
        # A PhotoImage keeps its creation size and silently clips larger
        # data until it is resized explicitly.
        photo.configure(width=target[0], height=target[1])
        state["header"] = f"P6 {target[0]} {target[1]} 255 ".encode("ascii")
        state["scaled"] = (
            None if target == native_size and state["zoom"] == 1.0
            else pygame.Surface(target)
        )
        state["push_every"] = 2 if target[0] * target[1] > FULL_RATE_PUSH_MAX_PIXELS else 1

    def push_frame(surface, draw_vehicles=None):
        """Push ``surface`` (the network, already drawn) to the Tk canvas.

        ``draw_vehicles(target, view, scale)`` draws the vehicles: on the
        native surface (view None, scale 1) when the view shows at most one
        screen px per world px, so the smoothscale's filtering carries their
        sub-pixel motion; but straight onto the scaled frame when zoomed in
        past that, so a vehicle at a fractional world x lands on the right
        screen pixel instead of hopping a whole (magnified) world pixel.
        """
        scaled = state["scaled"]
        view = view_rect() if state["zoom"] != 1.0 else surface.get_rect()
        scale = state["target"][0] / float(view.width)
        if draw_vehicles is not None and scale <= 1.0:
            draw_vehicles(surface, None, 1.0)
        if scaled is not None:
            if state["zoom"] != 1.0:
                surface = surface.subsurface(view)
            surface = pygame.transform.smoothscale(surface, state["target"], scaled)
        if draw_vehicles is not None and scale > 1.0:
            draw_vehicles(surface, view, scale)
        photo.configure(
            data=state["header"] + pygame.image.tostring(surface, "RGB"),
            format="PPM",
        )
        simulation_canvas.itemconfig(image_item, image=photo)
        # Tk repaints from its idle queue, which only drains when no timer is
        # due. A scaled push plus a physics step can outlast the 16ms step
        # interval, and then nothing on screen (canvas or side panes) ever
        # repaints. Flushing here makes every pushed frame -- and any pending
        # widget redraw -- actually reach the screen.
        simulation_canvas.update_idletasks()

    def frame_is_due():
        """Whether this tick paints. Display only -- the physics step, the
        telemetry snapshot and the agent never see it, exactly like the
        view zoom. Gates the network draw and the DBL-state scan too, not
        just the push: both are render-only work that cost as much as the
        paint they feed.

        An unattended batch drops to BATCH_RENDER_EVERY because nobody is
        watching 60 fps of it, and at the campaign's demand the render side
        was a fifth of every run's wall clock (2026-09-23 audit)."""
        state["pushes"] += 1
        every = state["push_every"]
        if (control_panel.global_config.get("batch_runtime") or {}).get("active", False):
            every = max(every, BATCH_RENDER_EVERY)
        return state["pushes"] % every == 0

    simulation_canvas.frame_is_due = frame_is_due
    simulation_canvas.set_target_size = set_target_size
    simulation_canvas.set_view_zoom = set_zoom
    simulation_canvas.view_rect = view_rect
    return simulation_canvas, push_frame


INSTANCE_LOCK_PATH = DATA_DIR / "simulator.lock"
_instance_lock = None


def acquire_instance_lock(path=None):
    """One simulator per data/ directory. Two instances would share the
    telemetry, control, decision and log files and drive each other; the
    lock is an OS byte lock, so a crashed instance releases it. The default
    is read at call time so a test that redirects INSTANCE_LOCK_PATH never
    contends with a live simulator's lock."""
    path = INSTANCE_LOCK_PATH if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SystemExit(
            f"Another simulator is already running on {path.parent}; "
            "it owns the telemetry/decision files there. Close it first."
        )
    return handle


def raise_simulator_priority():
    """Give the simulator's one thread first claim on a core.

    The 60 Hz step and the Tk loop share one Python thread, and pace is only
    1.0 while that thread gets a core whenever it is runnable. A local model
    in Ollama runs at normal priority on every physical core it is allowed
    (agent.OLLAMA_NUM_THREAD caps that), so above-normal here means the
    scheduler hands this thread a core first instead of time-slicing it
    against inference threads. Above-normal, not high: the desktop stays
    responsive. The agent subprocess is created at normal priority, as
    Windows does for children of an above-normal process."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
        return bool(kernel32.SetPriorityClass(
            kernel32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS
        ))
    except (AttributeError, OSError):
        return False


def main():
    global _active_signal_controller, _instance_lock
    _instance_lock = acquire_instance_lock()
    raise_simulator_priority()
    # Seeding makes traffic generation reproducible, not LLM inference. The
    # valid benchmark is the same seed with one ARMED and one DISARMED run;
    # same-seed ARMED runs may diverge because model decisions can differ.
    pygame.init()
    pygame.font.init()
    font = pygame.font.SysFont("Consolas", 13, bold=True)

    # No pygame window is created: the network renders onto an offscreen
    # Surface (canvas_gemini.draw_network and Vehicle.draw are plain pygame
    # drawing calls and work identically on one), which is pushed into a Tk
    # Canvas living in the simulation pane instead of being flipped to a
    # native display. Lane/intersection geometry (canvas_gemini.WIDTH/HEIGHT/
    # LANE/INT_X/H_Y) is untouched -- only where the pixels end up changes.
    screen = pygame.Surface((canvas.WIDTH, canvas.HEIGHT))

    signals = SignalController(global_config=control_panel.global_config, yellow_time=60, red_clearance_time=60)
    _active_signal_controller = signals
    telemetry = TelemetryExporter(filename=TELEMETRY_PATH, export_interval_frames=10)

    vehicles = []

    # 1. Build the unified window shell, then mount the control panel into
    # its left pane. write_ai_control() behavior is byte-identical to the
    # standalone-window case; the agent subprocess depends on ai_control.json
    # and nothing about what gets written to it changes here.
    root, control_pane, simulation_pane, telemetry_pane = build_main_window()
    control_panel.create_dashboard_window(control_pane)
    bind_pane_mousewheel(control_pane, control_pane.scroll_canvas)

    # 2. Mount the telemetry dashboard into the right pane, in-process. It
    # keeps reading traffic_state_telemetry.json / agent_turn_log.jsonl /
    # ai_control.json from disk exactly as it did as a separate subprocess;
    # only where its widgets live has changed.
    TelemetryDashboard(telemetry_pane)
    bind_pane_mousewheel(telemetry_pane, telemetry_pane.scroll_canvas)

    # 2b. The simulation canvas: a Tk Canvas holding one PhotoImage that
    # every render mutates in place (itemconfig'd once, never recreated or
    # replaced) so Tk redraws it without ever losing the reference mid-
    # frame. The shape controller fits it (and the pushed, smoothscaled
    # pixels) to the centre pane in every shape.
    _simulation_canvas, push_simulation_frame = build_simulation_canvas(
        simulation_pane
    )
    # getattr-guarded: tests drive main() with stand-in root/canvas objects
    # that have neither the controller nor the resizer.
    shape_controller = getattr(root, "window_shape", None)
    set_canvas_target_size = getattr(_simulation_canvas, "set_target_size", None)
    if shape_controller is not None and set_canvas_target_size is not None:
        shape_controller.attach_canvas(set_canvas_target_size)
        control_panel.window_shape_hooks["cycle"] = shape_controller.cycle

    print("Launching LLM Control Agent...")
    # stdin is a pipe the agent never reads for input: it watches for EOF,
    # which arrives the moment this process dies for ANY reason (IDE stop,
    # crash, frozen loop) -- not only the WM_DELETE_WINDOW path below. An
    # agent that outlives its sim re-arms off the shared ai_control.json and
    # drives the next session alongside the new agent (2026-09-20 audit).
    agent_proc = subprocess.Popen(
        [sys.executable, "-m", "src.agents.agent"],
        cwd=str(BASE_DIR),
        stdin=subprocess.PIPE,
    )

    # 3. Register cleanup, then build the workbook after agent writes stop.
    def cleanup():
        try:
            agent_proc.terminate()
        except Exception:
            pass
        try:
            agent_proc.wait(timeout=2)
        except Exception:
            pass
        # One workbook per run: a timed test (and every batch run, which goes
        # through the same path) already wrote its own, fuller workbook from
        # these same logs. Only a run that exported nothing needs this one.
        if not _run_exported_workbook:
            export_session_excel()
    atexit.register(cleanup)

    # One WM_DELETE_WINDOW handler for the one real window in this process
    # (each of control_panel's and TelemetryDashboard's own close handling is
    # skipped when mounted, per commits 3 and 4). sys.exit() runs the atexit-
    # registered cleanup() above exactly once -- agent_proc termination and
    # the combined-export workbook build -- the same way the old pygame QUIT
    # handler already relied on it.
    def on_main_window_close():
        control_panel.global_config["is_running"] = False
        try:
            root.destroy()
        except Exception:
            pass
        sys.exit()

    root.protocol("WM_DELETE_WINDOW", on_main_window_close)

    master_frame_count = 0
    dt_step = 1.0 / 60.0
    time_accumulator = 0.0
    last_wall_time = time.monotonic()
    max_catchup_seconds = 0.25
    # Keep Tk's event loop responsive after a window drag or scheduler delay.
    # Normal 3x operation needs about three steps per 16 ms callback; six gives
    # headroom without allowing a long catch-up burst to monopolize the UI.
    max_steps_per_callback = 6
    def simulation_step():
        nonlocal master_frame_count, time_accumulator, last_wall_time

        now = time.monotonic()
        # Real wall time for the pace measurement; the physics accumulator
        # gets the clamped value, because a desktop stall it could never
        # replay is still wall time the run spent.
        wall_since_last = max(0.0, now - last_wall_time)
        elapsed = min(wall_since_last, max_catchup_seconds)
        last_wall_time = now
        run_just_reset = False

        if control_panel.global_config.get("start_requested", False):
            master_frame_count = perform_full_reset(vehicles, signals, telemetry)
            time_accumulator = 0.0
            control_panel.global_config["is_paused"] = False
            control_panel.global_config["reset_triggered"] = False
            control_panel.global_config["is_running"] = True
            control_panel.global_config["run_has_started"] = True
            control_panel.global_config["test_last_export"] = ""
            control_panel.global_config["start_requested"] = False
            control_panel.write_ai_control()
            run_just_reset = True
            last_wall_time = time.monotonic()
        elif control_panel.global_config["reset_triggered"]:
            master_frame_count = perform_full_reset(vehicles, signals, telemetry)
            time_accumulator = 0.0
            # Export before RESET to preserve the previous run: RESET clears
            # both session logs so the next benchmark run is isolated.
            control_panel.global_config["reset_triggered"] = False
            run_just_reset = True
            last_wall_time = time.monotonic()

        sim_speed = control_panel.global_config.get("sim_speed", 1.0)
        is_running = control_panel.global_config.get("is_running", False)
        is_paused = control_panel.global_config.get("is_paused", False)

        if is_running and not is_paused and not run_just_reset:
            time_accumulator += elapsed * sim_speed
            _pace_state["wall_seconds"] += wall_since_last

            # FIXED TIMESTEP LOOP: Physics, Signals, Spawners run exactly at 60Hz intervals
            steps_this_callback = 0
            while (
                time_accumulator >= dt_step
                and steps_this_callback < max_steps_per_callback
            ):
                master_frame_count += 1
                step_simulation(vehicles, signals, master_frame_count)
                time_accumulator -= dt_step
                steps_this_callback += 1
                # Never step past a checkpoint or the duration: the export
                # below must see the mark's own frame, not the end of this
                # catch-up burst. See at_export_boundary.
                if at_export_boundary(master_frame_count):
                    break

            # If the desktop held the callback during a window move, discard
            # excessive wall-time debt instead of freezing the UI to replay it.
            if time_accumulator >= dt_step:
                time_accumulator = min(time_accumulator, dt_step)
        else:
            # Paused wall time must never become a catch-up burst on resume.
            time_accumulator = 0.0

        # Calibration is setup, not part of the measured run: while it is
        # in progress the frame counter, the published sim clock and the
        # timed-test countdown all stay frozen at zero.
        if control_panel.global_config.get("calibrating", False):
            root.after(TICK_MS, simulation_step)
            return

        # Publish the simulation clock so the control panel can count a timed
        # test down in sim-time, matching the clock the auto-stop uses.
        control_panel.global_config["sim_time_seconds"] = round(
            master_frame_count / 60.0, 1
        )

        # Intermediate checkpoints (5/10/15/30/60/120 sim-minutes, whichever
        # are below this test's duration) auto-export without stopping the
        # run, on the same sim clock the final auto-stop below uses.
        fire_due_checkpoints(master_frame_count)

        # A timed benchmark ends on the SIMULATION clock, never the wall clock,
        # so the same duration means the same amount of simulated traffic on
        # any machine regardless of render speed.
        if timed_test_is_complete(master_frame_count):
            finish_timed_test(master_frame_count)
            is_running = False

        # Advance the Batch Benchmark Runner, if one is active: it can only
        # observe run completion through the flags perform_full_reset and
        # finish_timed_test just flipped above, so it is polled here.
        poll_batch_runner()

        telemetry_exported = telemetry.export(
            signal_controller=signals,
            vehicles=vehicles,
            frame_number=master_frame_count,
            demand_state=get_demand_telemetry(),
            throughput_state=network_throughput,
        )
        if telemetry_exported:
            try:
                with TELEMETRY_PATH.open("r", encoding="utf-8") as telemetry_file:
                    exported_payload = json.load(telemetry_file)
                log_telemetry_sample(
                    exported_payload.get("frame_number", master_frame_count),
                    exported_payload,
                )
                gridlock_monitor.sample(exported_payload)
            except (OSError, json.JSONDecodeError, AttributeError, TypeError):
                pass

        # Render only on a due tick (build_simulation_canvas.frame_is_due):
        # the network draw, the DBL-state scan and the push all hang off the
        # one gate, which halves the rate past FULL_RATE_PUSH_MAX_PIXELS and
        # drops to BATCH_RENDER_EVERY while a batch sweep is running.
        if _simulation_canvas.frame_is_due():
            # Vehicles are drawn by the push: at the position interpolated
            # between the last two physics steps, so the 60 Hz sim and the
            # display's own cadence never beat against each other into a hop.
            alpha = (
                max(0.0, min(1.0, time_accumulator / dt_step))
                if is_running and not is_paused else 1.0
            )

            def draw_vehicles(target, view, scale):
                for v in vehicles:
                    v.draw(target, alpha, view, scale)

            canvas.draw_network(
                screen,
                signal_data=signals.get_all_signals(canvas.INT_X),
                dbl_states=signals.get_all_dbl_states(canvas.INT_X, vehicles),
                font=font,
                is_paused=is_paused or not is_running,
            )
            push_simulation_frame(screen, draw_vehicles)

        # ~60 Hz cadence measured from the start of this tick, so the work
        # done here (physics steps, the push, a telemetry write) does not
        # stretch the period and jitter the frame spacing.
        work_ms = (time.monotonic() - now) * 1000.0
        root.after(max(1, int(TICK_MS - work_ms)), simulation_step)

    root.after(TICK_MS, simulation_step)
    root.mainloop()

if __name__ == "__main__":
    main()
