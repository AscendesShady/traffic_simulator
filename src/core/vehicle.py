# vehicle.py
import contextlib
import itertools
import math
import random

import pygame

from src.ui.canvas_gemini import HEIGHT, INT_X, WIDTH


CAR_PASSENGERS = 4
TRUCK_PASSENGERS = 1
# Fixed, not stochastic: a declared simplification (every bus carries exactly
# this many passengers), not a measurement. Person-delay for buses is
# therefore a pure multiple of bus count with no load variation -- keep this
# in mind when reading bus_person_hours_delay across runs.
BUS_PASSENGERS = 45
DBL_LANE_INDEX = 2
# How long a bus may hold for a blocked DBL merge before giving the merge up
# for the current leg. 300 frames is about five seconds at 60 Hz.
DBL_MERGE_ABANDON_FRAMES = 300
# Speed at or below which a vehicle counts as stopped for blocking checks
# (legacy engine: an absolute 0.5 px/frame, 27 km/h at scale; the IDM engine
# uses SLOW_VEHICLE_SPEED_MPS through slow_vehicle_speed()).
SLOW_VEHICLE_SPEED = 0.5
SLOW_VEHICLE_SPEED_MPS = 1.5
# A multi-leg bus that needs a different lane at the next node gets a short,
# deterministic merge area immediately after the node it just crossed.  The
# target-lane gap is checked before the bus enters that upstream node; if the
# gap disappears later, the bus waits at the end of this merge area instead of
# carrying an unresolved lane change to the next stop bar.
ROUTE_MERGE_AREA_PX = 80.0
ROUTE_MERGE_SAFE_GAP_PX = 15.0
ROUTE_MERGE_YIELD_DISTANCE_PX = 100.0
# Retained as a public compatibility constant. DBL lane clearing is now
# unconditional across the reserved approach; it is no longer proximity-gated.
DBL_CLEAR_AHEAD_PX = 200.0
# Lateral step per frame for any cooperative lane change; matches the bus's
# own merge step so no vehicle ever jumps between lanes.
LANE_CHANGE_STEP_PX = 0.5
# A car opening a gap for a bus merging into the DBL lane caps its speed at
# this fraction of its own maximum until the bus's corridor is clear.
DBL_MERGE_YIELD_SPEED_RATIO = 0.5
# Slowest speed an arrival estimate may assume. A stopped bus divides by this
# instead of zero, so one queued a few px from the bar still reads as arriving
# soon while one stopped far back reads as a long, finite wait.
# ponytail: 10% of free flow; tune against measured queue-discharge speed.
ETA_MIN_SPEED_PX_PER_FRAME = 0.05
ETA_MAX_FRAMES = 6000.0
# Within this of its node's stop bar a left-turner (bus or car) moves into
# the turn lane (lane 2) and is not asked to leave it.
TURN_LANE_MERGE_PX = 250.0
# A left-turner still out of lane 2 after waiting this long at the bar takes
# the missed turn and goes straight, as a driver in the wrong lane does,
# rather than hold a through lane indefinitely (cars held 100 s+ in lane 0,
# 2026-09-23 audit). Counted in SAFETY_COUNTERS["missed_turns"].
MISSED_TURN_HOLD_FRAMES = 600
# Car-following: stop inside SAFE_GAP, follow at a gap-proportional speed
# up to FOLLOW_FREE_GAP, free flow beyond it.
SAFE_GAP_PX = 12.0
# How close to the stop bar a vehicle must be before it asks for an
# intersection reservation and runs its keep-clear (spillback) check. The
# signal controller reads the same zone when it decides whether a green is
# discharging anything, so the two can never disagree about who is "at the bar".
ENTRY_ZONE_PX = 25.0
FOLLOW_FREE_GAP_PX = SAFE_GAP_PX + 25.0
# Discretionary lane changes (MOBIL-lite: Kesting/Treiber/Helbing 2007 incentive
# and safety criteria on this sim's gap-based following law). Only straight
# cars, only between the two general lanes -- lane 2 stays the left-turn/DBL
# lane -- and only upstream, clear of the stop-bar approach. Incentive is the
# follow-speed gain as a fraction of the driver's own desired speed; moving
# inward (lane 1) must gain LANE_CHANGE_GAIN + LANE_CHANGE_KEEP_OUTER_BIAS,
# moving outward (lane 0) may even lose up to the bias, so free-flowing
# traffic drifts out and the inner lane stays open for overtaking (MOBIL's
# asymmetric keep-right rule, mirrored for this network). The per-frame hazard
# keeps a platoon from changing in lockstep; it is the only stochastic term
# and draws from the run-seeded global RNG.
DISCRETIONARY_LANES = (0, 1)
LANE_CHANGE_GAIN = 0.10
LANE_CHANGE_KEEP_OUTER_BIAS = 0.10
LANE_CHANGE_MIN_DIST_TO_BAR_PX = 150.0
LANE_CHANGE_REAR_GAP_PX = 30.0
LANE_CHANGE_HAZARD_PER_FRAME = 1.0 / 120.0  # mean 2 s between considerations

# --- Physical units -----------------------------------------------------------
# One scale for the whole engine. real_world_units anchors the same 0.25 m/px
# from HCM jam spacing (7.5 m) over a queued car (18 + 12 px) and a test pins
# the two together; time is exact (60 frames = 1 s).
METERS_PER_PX = 0.25
FPS = 60


def mps_to_px_per_frame(v_mps):
    return float(v_mps) / METERS_PER_PX / FPS


def mps2_to_px_per_frame2(a_mps2):
    return float(a_mps2) / METERS_PER_PX / FPS ** 2


def px_per_frame_to_mps(v):
    return float(v) * METERS_PER_PX * FPS


def px_per_frame2_to_mps2(a):
    return float(a) * METERS_PER_PX * FPS ** 2


# --- Movement model -----------------------------------------------------------
# "legacy": the gap/speed following rule above with a +-0.05 px/frame^2 ramp
# (45 m/s^2 at this scale) and a 0.5 px/frame lateral slide (0.7 s per lane):
# the engine every pinned result was produced with, kept for continuity.
# "idm": Intelligent Driver Model car following per vehicle class (Treiber,
# Hennecke & Helbing 2000) with comfortable braking to a red bar, full MOBIL
# lane changes (politeness, follower safety; Kesting, Treiber & Helbing 2007)
# and a LANE_CHANGE_DURATION_S slide. Selected per run from
# global_config["movement_model"] in main.perform_full_reset; part of
# movement_model_signature, so runs from different engines never pair.
MOVEMENT_MODEL_LEGACY = "legacy"
MOVEMENT_MODEL_IDM = "idm"
MOVEMENT_MODELS = (MOVEMENT_MODEL_LEGACY, MOVEMENT_MODEL_IDM)
# IDM is the default: it is the engine pinned against HCM saturation flow,
# start-up behaviour and queue spacing (tests/test_physical_calibration.py).
# Legacy is kept for comparison only -- its uninterrupted saturation flow is
# ~2,800 veh/h/lane at the default speed scale, far above HCM's 1,900 base.
_movement_model = {"name": MOVEMENT_MODEL_IDM}
LEGACY_ACCEL_PX_PER_FRAME2 = 0.05

# IDM parameters per vehicle class, physical units (Treiber & Kesting, Traffic
# Flow Dynamics 2013, ch. 11 urban values; heavy vehicles: longer headway,
# gentler acceleration). s0 is SAFE_GAP_PX (3 m) so the standing gap -- and
# the jam-spacing anchor -- is the same in both engines. v0 is each
# vehicle's own max_speed (already heterogeneous per driver).
IDM_PARAMS_MPS = {
    "car":   {"T": 1.0, "a": 2.0, "b": 2.0},
    "truck": {"T": 1.7, "a": 0.8, "b": 1.5},
    "bus":   {"T": 1.5, "a": 1.2, "b": 1.5},
}
IDM_S0_PX = SAFE_GAP_PX
# Braking bound. The most a vehicle may decelerate in one step, per class:
# SUMO's vehicle-type defaults for emergencyDecel (passenger 9.0, truck and
# bus 7.0 m/s^2; SUMO documentation, "Vehicle Type Parameter Defaults"). The
# IDM law has no such bound -- its interaction term grows without limit as a
# gap closes -- so an obstacle that appears close (a vehicle entering at
# speed, a stop line inside the braking distance) used to be absorbed by an
# arbitrarily hard stop: 71 events above 2 g per vehicle-hour at the
# 2026-09-22 campaign's demand. MSCFModel::finalizeSpeed applies the same
# floor; what the floor cannot resolve is recorded, not hidden (see
# SAFETY_COUNTERS).
EMERGENCY_DECEL_MPS2 = {"car": 9.0, "truck": 7.0, "bus": 7.0}
# Surrogate safety measure: time-to-collision to the leader below this is a
# conflict (FHWA, Surrogate Safety Assessment Model, FHWA-HRT-08-051, 2008,
# default maximum TTC 1.5 s).
SSM_TTC_THRESHOLD_SEC = 1.5
STOP_LINE_EPSILON_PX = 1e-3
# Comfortable deceleration a vehicle entering the network is assumed to be
# able to use against the vehicle ahead, for the legacy engine (SUMO's
# passenger default decel, 4.5 m/s^2); the IDM engine uses its class b.
LEGACY_INSERTION_DECEL_MPS2 = 4.5

# Run-level safety counters, restarted per run (reset_safety_counters) and
# copied into main.network_throughput each frame so steady-state versions
# exist. Frames are vehicle-frames.
#   emergency_decel_frames  the law asked for more than the class bound
#   leader_clamp_frames     move truncated at the leader's rear bumper
#   stop_line_clamp_frames  move truncated at a stop line / hold point
#   ttc_conflict_frames     TTC to the leader below SSM_TTC_THRESHOLD_SEC
#   ttc_conflicts           conflicts (entries into that state)
SAFETY_COUNTERS = {
    "emergency_decel_frames": 0,
    "emergency_decel_events": 0,
    "leader_clamp_frames": 0,
    "stop_line_clamp_frames": 0,
    "ttc_conflict_frames": 0,
    "ttc_conflicts": 0,
    # Not a safety surrogate: a realism counter reported alongside them.
    "missed_turns": 0,
}


def reset_safety_counters():
    for key in SAFETY_COUNTERS:
        SAFETY_COUNTERS[key] = 0


def emergency_decel_px(vehicle):
    return mps2_to_px_per_frame2(EMERGENCY_DECEL_MPS2[vehicle_class(vehicle)])


def safe_insertion_speed(vehicle, gap, leader):
    """Fastest speed a newly entering vehicle can have and still stop behind
    its leader at a comfortable rate if the leader also stops -- the role of
    SUMO's insertionFollowSpeed. Entering at full desired speed behind a
    queue was 36 % of all IDM braking beyond 9 m/s^2."""
    if leader is None or gap == float("inf"):
        return vehicle.max_speed
    if is_idm():
        decel = idm_params_px(vehicle)[2]
    else:
        decel = mps2_to_px_per_frame2(LEGACY_INSERTION_DECEL_MPS2)
    leader_v = max(0.0, float(getattr(leader, "speed", 0.0)))
    room = max(0.0, float(gap) - IDM_S0_PX)
    return min(vehicle.max_speed, math.sqrt(leader_v * leader_v + 2.0 * decel * room))
LANE_CHANGE_DURATION_S = 3.0        # Toledo & Zohar 2007: 3-5 s urban
MOBIL_POLITENESS = 0.3
MOBIL_THRESHOLD_MPS2 = 0.1
MOBIL_KEEP_OUTER_BIAS_MPS2 = 0.3
MOBIL_SAFE_DECEL_MPS2 = 4.0


def set_movement_model(name):
    if name not in MOVEMENT_MODELS:
        raise ValueError(f"movement_model must be one of {MOVEMENT_MODELS}, not {name!r}")
    _movement_model["name"] = name


def movement_model():
    return _movement_model["name"]


def is_idm():
    return _movement_model["name"] == MOVEMENT_MODEL_IDM


def lane_change_step_px(lane_w=22):
    """Lateral px per frame of every lane change (cars, evictions, bus merges)."""
    if is_idm():
        return lane_w / (LANE_CHANGE_DURATION_S * FPS)
    return LANE_CHANGE_STEP_PX


def slow_vehicle_speed():
    """Standing threshold for spillback / DBL-lane checks, in px/frame."""
    if is_idm():
        return mps_to_px_per_frame(SLOW_VEHICLE_SPEED_MPS)
    return SLOW_VEHICLE_SPEED


def vehicle_class(vehicle):
    if isinstance(vehicle, Bus):
        return "bus"
    return "truck" if getattr(vehicle, "is_heavy", False) else "car"


def idm_params_px(vehicle):
    """(T frames, a px/frame^2, b px/frame^2) for this vehicle's class."""
    p = IDM_PARAMS_MPS[vehicle_class(vehicle)]
    return p["T"] * FPS, mps2_to_px_per_frame2(p["a"]), mps2_to_px_per_frame2(p["b"])


def idm_acceleration(v, v0, gap, dv, T, a, b, s0=IDM_S0_PX):
    """IDM acceleration in px/frame^2. ``gap`` is the bumper gap to the
    leader (inf = free road), ``dv`` the closing speed v - v_leader."""
    free = 1.0 - (v / v0) ** 4 if v0 > 0 else -1.0
    if gap == float("inf"):
        return a * free
    s_star = s0 + max(0.0, v * T + v * dv / (2.0 * math.sqrt(a * b)))
    return a * (free - (s_star / max(gap, 0.1)) ** 2)


def comfortable_stop_px(v, b):
    """Distance to stop from v at the comfortable deceleration b."""
    return v * v / (2.0 * b) if b > 0 else 0.0


def movement_model_signature():
    """The tunables that set capacity, for experiment_summary's config_hash."""
    return {
        "model": movement_model(),
        "idm": IDM_PARAMS_MPS if is_idm() else None,
        "lane_change_duration_s": LANE_CHANGE_DURATION_S if is_idm() else None,
        "safe_gap": SAFE_GAP_PX,
        "follow_free_gap": FOLLOW_FREE_GAP_PX,
        "lane_band_follower_exempt": True,
        "lane_change": [
            list(DISCRETIONARY_LANES), LANE_CHANGE_GAIN, LANE_CHANGE_KEEP_OUTER_BIAS,
            LANE_CHANGE_MIN_DIST_TO_BAR_PX, LANE_CHANGE_REAR_GAP_PX,
            LANE_CHANGE_HAZARD_PER_FRAME,
        ],
    }


@contextlib.contextmanager
def lane_changes_suspended():
    """No discretionary lane changes inside the block. The saturation-flow
    calibrators discharge one standing lane; cars peeling into the empty
    neighbour would turn that into a two-lane count."""
    global LANE_CHANGE_HAZARD_PER_FRAME
    saved = LANE_CHANGE_HAZARD_PER_FRAME
    LANE_CHANGE_HAZARD_PER_FRAME = 0.0
    try:
        yield
    finally:
        LANE_CHANGE_HAZARD_PER_FRAME = saved


def eta_frames_to_stop_bar(distance_px, speed_px_per_frame, max_speed=None, accel=None):
    """Frames until a vehicle ``distance_px`` short of a stop bar reaches it.
    The one estimator shared by telemetry, the LLM/rule decision path and
    the signal arbiter, so no two of them can disagree about when a bus
    arrives.

    With ``max_speed`` and ``accel`` it is the UNIMPEDED arrival time:
    accelerate from the current speed to the desired speed at ``accel``,
    then cruise. That is the question a priority request asks -- when would
    the bus arrive if it were given the green -- and how deployed TSP
    predicts arrival (from travel time past a check-in point, not the bus's
    instantaneous speed). Under IDM a bus facing red slows well before the
    bar, so its current speed badly overstates the time it needs once the
    green comes. Without them: at the current speed (the legacy engine,
    whose vehicles hold speed until the bar)."""
    distance = float(distance_px)
    if distance <= 0:
        return 0.0
    v = max(0.0, float(speed_px_per_frame))
    if max_speed is not None and accel is not None and float(accel) > 0 and float(max_speed) > v:
        v0, a = float(max_speed), float(accel)
        accel_distance = (v0 * v0 - v * v) / (2.0 * a)
        if distance <= accel_distance:
            frames = (-v + math.sqrt(v * v + 2.0 * a * distance)) / a
        else:
            frames = (v0 - v) / a + (distance - accel_distance) / v0
        return min(ETA_MAX_FRAMES, frames)
    return min(ETA_MAX_FRAMES, distance / max(v, ETA_MIN_SPEED_PX_PER_FRAME))


def bus_eta_frames(vehicle, distance_px):
    """eta_frames_to_stop_bar for this vehicle on the active engine."""
    if is_idm():
        return eta_frames_to_stop_bar(
            distance_px, vehicle.speed, vehicle.max_speed, idm_params_px(vehicle)[1]
        )
    return eta_frames_to_stop_bar(distance_px, vehicle.speed)


def _is_following(mover, other):
    """``other`` is behind ``mover`` and already overlaps its current y-band.

    The following law holds such a vehicle at SAFE_GAP_PX, which is less than
    the 15 px lateral margin below, so counting it as "alongside" would pin
    every mover mid-slide once its follower catches up (a queue-head deadlock
    seen live: a left-turning truck frozen straddling lanes 1/2 at Node A).
    The margin still applies to vehicles ahead and to starting a slide."""
    ox, oy, _, _ = _seen(other)
    if mover.direction == "EB":
        behind = ox < mover.x
    elif mover.direction == "WB":
        behind = ox > mover.x
    else:
        return False
    return behind and abs(oy - mover.y) < (mover.width + other.width) / 2.0


# --- per-frame spatial index -------------------------------------------
#
# Every neighbour query below used to walk the whole vehicle list, and each
# one runs about once per vehicle per frame: at the benchmark regime's ~230
# vehicles that is O(n^2) per frame and it was 60% of a run's wall clock
# (profile, 2026-09-23 audit). The index is a plain uniform grid over the
# physics surface, rebuilt once per frame by main.step_simulation.
#
# It is a CANDIDATE filter only. Every query re-applies the original
# predicate, in the original list order, to whatever the grid hands back --
# so the index can only ever make a query faster, never answer it
# differently. Two properties keep the candidate set a guaranteed superset:
#
#  * Positions go stale within the frame (vehicles move as they update), so
#    every lookup is padded by ``margin``: the largest half-extent plus the
#    largest speed on the surface, i.e. further than anything can have moved
#    or turned since the rebuild.
#  * The list itself can change (spawns, removals), so a cached index is
#    used only for the exact list object it was built from, at the length it
#    was built at; anything else falls back to the full scan.
_INDEX_CELL_PX = 64.0


class _FrameIndex:
    """Uniform grid of (list position, vehicle) over one frame's vehicles."""

    __slots__ = ("source", "length", "cells", "margin", "buses", "bounds", "receiving")

    def __init__(self, vehicles):
        self.source = vehicles
        self.length = len(vehicles)
        # receiving_space_px answers per (node, exit lane), read from the
        # frame-start snapshot, so it cannot change within a frame unless a
        # vehicle leaves; drop() clears it.
        self.receiving = {}
        cells = {}
        buses = []
        half_max = 0.0
        speed_max = 0.0
        cell = _INDEX_CELL_PX
        for pos, v in enumerate(vehicles):
            if isinstance(v, Bus):
                buses.append(v)
            half = max(v.length, v.width) / 2.0
            if half > half_max:
                half_max = half
            reach = max(abs(v.speed), abs(getattr(v, "max_speed", 0.0) or 0.0))
            if reach > speed_max:
                speed_max = reach
            key = (int(v.x // cell), int(v.y // cell))
            bucket = cells.get(key)
            if bucket is None:
                cells[key] = [(pos, v)]
            else:
                bucket.append((pos, v))
        self.cells = cells
        self.buses = buses
        # Occupied cell range per axis, so a scan walks to the last vehicle
        # that exists rather than to the canvas edge: a vehicle stays in the
        # list for 60 px beyond the surface before the frame removes it, and
        # bounding the walk by the surface once lost those leaders entirely.
        cols = [key[0] for key in cells] or [0]
        rows = [key[1] for key in cells] or [0]
        self.bounds = (min(cols), max(cols), min(rows), max(rows))
        # A vehicle's own extent, plus the furthest it can travel in the one
        # frame this index lives for. Nothing can leave a padded window.
        self.margin = half_max + speed_max + 1.0

    def matches(self, vehicles):
        # Identity AND length: a mutation nobody told the index about (the
        # saturation calibrator's own list, a reset) can then only ever cost
        # the fast path, never return a vehicle that has left the network.
        return self.source is vehicles and self.length == len(vehicles)

    def drop(self, vehicle):
        """Forget one vehicle the frame has just removed from the network."""
        self.receiving.clear()
        key = (int(vehicle.x // _INDEX_CELL_PX), int(vehicle.y // _INDEX_CELL_PX))
        bucket = self.cells.get(key)
        if bucket is not None:
            kept = [item for item in bucket if item[1] is not vehicle]
            if len(kept) != len(bucket):
                self.cells[key] = kept
                self.length -= 1
                if isinstance(vehicle, Bus):
                    self.buses = [b for b in self.buses if b is not vehicle]
                return
        # Moved out of its indexed cell before leaving: fall back to a
        # rebuild-on-next-query rather than carry a phantom.
        self.length = -1

    def column(self, along_cell, cross_lo, cross_hi, vertical):
        """Every (pos, vehicle) in one along-axis cell slice, across the
        cross-axis band. ``vertical`` when the along axis is y."""
        cell = _INDEX_CELL_PX
        lo = int(cross_lo // cell)
        hi = int(cross_hi // cell)
        found = []
        get = self.cells.get
        for index in range(lo, hi + 1):
            bucket = get((index, along_cell) if vertical else (along_cell, index))
            if bucket:
                found.extend(bucket)
        return found


_frame_index = None

# Plan-before-commit perception. SUMO plans every vehicle's move against one
# frozen state and only then moves them all (MSEdgeControl::planMovements,
# then executeMovements; 09_INVARIANTS "planning precedes execution"). This
# engine updates vehicles one at a time, so a vehicle used to see some
# neighbours already moved this frame and others not, depending only on list
# order: reversing that order moved passengers served by up to 9.5 % and
# travel delay by up to 8.9 % on one seed (2026-09-23 audit). Every
# neighbour query now reads the position, direction and speed each vehicle
# had when the frame began (_seen), so car-following and lane-change
# perception are independent of update order. Reservations at the conflict
# box stay sequential -- first to ask is first served -- which is SUMO's
# link-arbitration behaviour too.
_SNAPSHOT = {"current": None, "counter": 0}


def _seen(vehicle):
    """(x, y, direction, speed) as the frame began; live when no snapshot
    belongs to the current frame (tests, calibration, foreign lists)."""
    snap = vehicle.__dict__.get("_snap")
    if snap is not None and snap[0] == _SNAPSHOT["current"]:
        return snap[1], snap[2], snap[3], snap[4]
    return vehicle.x, vehicle.y, vehicle.direction, vehicle.speed


def _take_snapshot(vehicles):
    _SNAPSHOT["counter"] += 1
    epoch = _SNAPSHOT["current"] = _SNAPSHOT["counter"]
    for v in vehicles:
        v._snap = (epoch, v.x, v.y, v.direction, v.speed)


def index_frame(vehicles):
    """Rebuild the per-frame index, or drop it when given None.

    main.step_simulation drops it at the top of the frame (positions from
    the previous frame are further stale than the margin allows) and
    rebuilds once the frame's spawns and dispatches are in. A caller that
    never calls either just gets the full scans, unchanged.
    """
    global _frame_index
    if vehicles is None:
        _frame_index = None
        _SNAPSHOT["current"] = None
        return
    _take_snapshot(vehicles)
    _frame_index = _FrameIndex(vehicles)


def drop_from_index(vehicle):
    """Tell the index a vehicle has left the network mid-frame, so the rest
    of the frame keeps the fast path instead of falling back on the
    length guard."""
    if _frame_index is not None:
        _frame_index.drop(vehicle)


# Escape hatch: set False to force every query back through the full scan.
# The index is meant to be a pure accelerator, so a run with it off must
# produce the identical fingerprint -- tests/test_frame_index.py asserts it.
USE_FRAME_INDEX = True


def _index_for(vehicles):
    if not USE_FRAME_INDEX:
        return None
    index = _frame_index
    return index if index is not None and index.matches(vehicles) else None


def buses_in(all_vehicles):
    """The buses of ``all_vehicles``, in list order. Indexed when this is
    the frame's own list, scanned when it is not."""
    index = _index_for(all_vehicles)
    if index is not None:
        return index.buses
    return [v for v in all_vehicles or [] if isinstance(v, Bus)]


def _gap_ahead(vehicle, other, my_y, my_half_w):
    """Bumper gap from ``vehicle`` to ``other`` when ``other`` is ahead of it
    in the lane centred on ``my_y`` (on ``vehicle.x`` for NB/SB), else None.

    The one copy of get_lead_vehicle's test: the grid scan and the final
    pick both go through it, so a candidate can never be judged by a
    different rule than the answer is.
    """
    if other is vehicle:
        return None
    ox, oy, odir, _ = _seen(other)
    if odir in ("EB", "WB"):
        o_min_y, o_max_y = oy - other.width / 2.0, oy + other.width / 2.0
        o_min_x, o_max_x = ox - other.length / 2.0, ox + other.length / 2.0
    else:
        o_min_y, o_max_y = oy - other.length / 2.0, oy + other.length / 2.0
        o_min_x, o_max_x = ox - other.width / 2.0, ox + other.width / 2.0

    direction = vehicle.direction
    if direction in ("EB", "WB"):
        if my_y + my_half_w <= o_min_y or my_y - my_half_w >= o_max_y:
            return None
        if direction == "EB" and o_min_x > vehicle.x:
            dist = o_min_x - (vehicle.x + vehicle.length / 2.0)
        elif direction == "WB" and o_max_x < vehicle.x:
            dist = (vehicle.x - vehicle.length / 2.0) - o_max_x
        else:
            return None
    elif direction in ("NB", "SB"):
        if vehicle.x + my_half_w <= o_min_x or vehicle.x - my_half_w >= o_max_x:
            return None
        if direction == "NB" and o_max_y < vehicle.y:
            dist = (vehicle.y - vehicle.length / 2.0) - o_max_y
        elif direction == "SB" and o_min_y > vehicle.y:
            dist = o_min_y - (vehicle.y + vehicle.length / 2.0)
        else:
            return None
    else:
        return None
    return dist if dist >= 0 else None


def _lead_candidates(vehicle, all_vehicles, my_y, my_half_w):
    """Candidate leaders for ``get_lead_vehicle``, in list order, or None
    when there is no usable index and the caller should scan everything.

    Walks cells away from the vehicle along its own travel axis and stops
    once the closest gap found beats anything an unscanned cell could still
    hold, so the set always contains the true leader.
    """
    index = _index_for(all_vehicles)
    if index is None:
        return None
    cell = _INDEX_CELL_PX
    margin = index.margin
    direction = vehicle.direction
    vertical = direction in ("NB", "SB")
    forward = direction in ("EB", "SB")
    sign = 1 if forward else -1
    col_min, col_max, row_min, row_max = index.bounds
    if vertical:
        here = vehicle.y
        cross_centre = vehicle.x
        along_min, along_max = row_min, row_max
    else:
        here = vehicle.x
        cross_centre = my_y
        along_min, along_max = col_min, col_max
    front = here + sign * vehicle.length / 2.0
    cross_lo = cross_centre - my_half_w - margin
    cross_hi = cross_centre + my_half_w + margin
    # Start one margin behind: a vehicle indexed just short of us may have
    # moved ahead of us since the rebuild. Both ends clamp to the cells that
    # actually hold vehicles, so no occupied cell is ever skipped and no
    # empty one is ever walked.
    first = int((here - sign * margin) // cell)
    if forward:
        first, last = max(first, along_min), along_max
    else:
        first, last = min(first, along_max), along_min

    found = []
    best = float("inf")
    for at in range(first, last + sign, sign):
        bucket = index.column(at, cross_lo, cross_hi, vertical)
        if bucket:
            found.extend(bucket)
            for _, other in bucket:
                gap = _gap_ahead(vehicle, other, my_y, my_half_w)
                if gap is not None and gap < best:
                    best = gap
        # Closest a vehicle in any cell past this one could be, allowing for
        # staleness. Once the best gap beats it, nothing further can win.
        edge = (at + 1) * cell if forward else at * cell
        if best <= (edge - margin - front) * sign:
            break
    found.sort(key=lambda item: item[0])  # list order, so ties break as before
    return [item[1] for item in found]


def corridor_blockers(mover, desired_y, all_vehicles):
    """Same-direction vehicles occupying `mover`'s lane-change corridor.

    Shared by the live merge check and by telemetry, so the obstruction the
    model is told about is the same one that actually stops a merge.
    """
    corridor_min = min(mover.y, desired_y) - mover.width / 2.0
    corridor_max = max(mover.y, desired_y) + mover.width / 2.0
    blockers = []
    for other in all_vehicles or []:
        if other is mover:
            continue
        ox, oy, odir, _ = _seen(other)
        if odir != mover.direction:
            continue
        other_min = oy - other.width / 2.0
        other_max = oy + other.width / 2.0
        if not (other_min < corridor_max and other_max > corridor_min):
            continue
        if _is_following(mover, other):
            continue
        if abs(ox - mover.x) < (mover.length + other.length) / 2.0 + 15:
            blockers.append(other)
    return blockers


def lane_band_blockers(mover, desired_y, all_vehicles, origin_y, lane_w=22):
    """Same-direction vehicles alongside `mover` anywhere it sweeps between
    its current y and ``desired_y``, except those centred in the lane it is
    leaving (``origin_y``). Unlike corridor_blockers this ignores the origin
    lane: a leader a car-length ahead there is a following-distance matter,
    not a lateral one, and must not pin a car that wants (or is told) to
    move over. A lane crossed on the way still counts."""
    band_min = min(mover.y, desired_y) - mover.width / 2.0
    band_max = max(mover.y, desired_y) + mover.width / 2.0
    blockers = []
    for other in all_vehicles or []:
        if other is mover:
            continue
        ox, oy, odir, _ = _seen(other)
        if odir != mover.direction or abs(oy - origin_y) < lane_w / 2.0:
            continue
        if not (oy - other.width / 2.0 < band_max and oy + other.width / 2.0 > band_min):
            continue
        if _is_following(mover, other):
            continue
        if abs(ox - mover.x) < (mover.length + other.length) / 2.0 + 15:
            blockers.append(other)
    return blockers


def dbl_lane_center_y(direction, h_y, lane_w=22):
    """Centre line of the DBL lane for an EB or WB approach."""
    offset = (DBL_LANE_INDEX + 0.5) * lane_w
    return h_y - offset if direction == "EB" else h_y + offset


def _is_ahead_on_approach(bus, other):
    if bus.direction == "EB":
        return other.x > bus.x
    if bus.direction == "WB":
        return other.x < bus.x
    return False


def dbl_lane_queue_ahead(bus, all_vehicles, h_y, target_node=None, lane_w=22):
    """Stopped/crawling vehicles ahead of ``bus`` in the reserved DBL lane.

    When ``target_node`` is supplied, only the current approach between the
    bus and that node's stop bar is inspected. This is the queue that would
    make a DBL activation useless: reserving a lane cannot remove a standing
    vehicle already in front of the priority bus.
    """
    desired_y = dbl_lane_center_y(bus.direction, h_y, lane_w)
    bus_distance = None
    if target_node is not None:
        bus_distance = bus.distance_to_node_stop_bar(target_node, h_y)

    queued = []
    for other in all_vehicles or []:
        if other is bus or other.direction != bus.direction:
            continue
        if isinstance(other, Bus):
            # Another bus queued in the reserved lane is its intended
            # occupant, not an obstruction to be cleared out of it. Counting
            # one vetoed the follower's DBL -- stickily, via
            # dbl_merge_abandoned_for_leg -- so wherever several routes share
            # an approach (EB lane 2 at Node A carries R1, R2 and R3) only the
            # leading bus could ever hold the lane and every bus behind it
            # lost DBL for that leg. The leader clears under its own priority.
            continue
        if not _is_ahead_on_approach(bus, other):
            continue
        other_half_width = getattr(other, "width", 0.0) / 2.0
        if not (
            other.y - other_half_width
            < desired_y
            < other.y + other_half_width
        ):
            continue
        if not _is_queued_for_dbl(other, all_vehicles, desired_y):
            continue
        if target_node is not None:
            other_distance = other.distance_to_node_stop_bar(target_node, h_y)
            if not (0.0 <= other_distance < bus_distance):
                continue
        queued.append(other)
    return queued


def _is_queued_for_dbl(vehicle, all_vehicles, lane_y):
    """Distinguish a queue from a naturally slow bus or heavy vehicle."""
    speed = max(0.0, float(getattr(vehicle, "speed", 0.0)))
    desired = max(0.0, float(getattr(vehicle, "max_speed", 0.0)))
    if speed <= 0.05:  # A standing head can have no leader (for example at red).
        return True
    if desired <= 0 or speed > 0.35 * desired:
        return False
    for leader in all_vehicles or []:
        if leader is vehicle or leader.direction != vehicle.direction:
            continue
        if not _is_ahead_on_approach(vehicle, leader):
            continue
        if abs(leader.y - lane_y) >= (getattr(leader, "width", 0) + getattr(vehicle, "width", 0)) / 2:
            continue
        gap = abs(leader.x - vehicle.x) - (leader.length + vehicle.length) / 2
        if 0 <= gap <= max(SAFE_GAP_PX * 3, vehicle.length):
            return True
    return False


def dbl_lane_is_obstructed(
    bus, all_vehicles, h_y, lane_w=22, target_node=None
):
    """Whether DBL would strand ``bus`` behind traffic or block its merge.

    A stopped/crawling vehicle ahead is always an obstruction, including when
    the bus is already in the DBL lane. Otherwise, a bus outside that lane is
    obstructed when stopped/crawling traffic occupies its merge corridor.
    Moving traffic is not a queue: once DBL is active it is ordered to vacate
    the reserved lane immediately and can also clear longitudinally.
    """
    if dbl_lane_queue_ahead(
        bus, all_vehicles, h_y, target_node=target_node, lane_w=lane_w
    ):
        return True
    if getattr(bus, "lane_index", None) == DBL_LANE_INDEX:
        return False
    desired_y = dbl_lane_center_y(bus.direction, h_y, lane_w)
    return any(
        _is_queued_for_dbl(other, all_vehicles, desired_y)
        for other in corridor_blockers(bus, desired_y, all_vehicles)
    )


LEFT_EXIT_DIRECTION = {"EB": "NB", "WB": "SB", "NB": "WB", "SB": "EB"}


def receiving_lane(node_x, direction, movement, lane_cross, h_y=300, lane_w=22):
    """``(exit_direction, cross)`` of the lane a movement enters after
    ``node_x``: ``cross`` is the lane centre's y on EB/WB, x on NB/SB. A
    straight movement keeps its own lane (``lane_cross``); a left turn's
    square-corner pivot lands on the exit road's lane 2."""
    if movement != "LEFT":
        return direction, lane_cross
    offset = (DBL_LANE_INDEX + 0.5) * lane_w
    return LEFT_EXIT_DIRECTION[direction], {
        "EB": node_x - offset, "WB": node_x + offset,
        "NB": h_y + offset, "SB": h_y - offset,
    }[direction]


def receiving_space_px(
    node_x, direction, movement, lane_cross, all_vehicles,
    h_y=300, road_w=132, lane_w=22, int_x_list=INT_X,
):
    """Free road in the one lane a movement enters after ``node_x``: from
    the far edge of the conflict box to the tail of the nearest standing
    vehicle in that lane, else to the next node's box or the canvas edge.

    The single receiving-space measure: a vehicle's spillback hold at the
    bar, the controller's TSP gate and telemetry's downstream figures all
    read it, keyed by node, entry direction, movement and receiving lane,
    so a blocked target lane is never hidden by an empty neighbour."""
    exit_dir, cross = receiving_lane(node_x, direction, movement, lane_cross, h_y, lane_w)
    # The metrics pass asks this for every vehicle upstream of a bar, every
    # frame, and each answer scans the whole list: 24 ms of a 16.67 ms frame
    # at 200 vehicles (2026-09-23). Only 2 nodes x 4 exits x 3 lanes of
    # answers exist per frame, so the frame's index memoises them.
    index = _index_for(all_vehicles)
    key = (node_x, exit_dir, cross, h_y, road_w, lane_w, tuple(int_x_list))
    if index is not None and key in index.receiving:
        return index.receiving[key]
    free = _receiving_scan(exit_dir, cross, node_x, all_vehicles, h_y, road_w, lane_w, int_x_list)
    if index is not None:
        index.receiving[key] = free
    return free


def _receiving_scan(exit_dir, cross, node_x, all_vehicles, h_y, road_w, lane_w, int_x_list):
    half_w = road_w / 2.0
    horizontal = exit_dir in ("EB", "WB")
    if exit_dir == "EB":
        start = node_x + half_w
        nexts = [x for x in int_x_list if x > node_x]
        end = min(nexts) - half_w if nexts else float(WIDTH)
    elif exit_dir == "WB":
        start = node_x - half_w
        prevs = [x for x in int_x_list if x < node_x]
        end = max(prevs) + half_w if prevs else 0.0
    elif exit_dir == "NB":
        start, end = h_y - half_w, 0.0
    else:
        start, end = h_y + half_w, float(HEIGHT)
    # +1 when the exit runs along increasing x/y, -1 when it runs back.
    sign = 1.0 if end > start else -1.0
    free = abs(end - start)
    half_band = lane_w / 2.0
    slow = slow_vehicle_speed()
    for other in all_vehicles or []:
        ox, oy, odir, ospeed = _seen(other)
        if odir != exit_dir or ospeed >= slow:
            continue
        # Straight-line arithmetic, not per-call lambdas: this runs for
        # every vehicle on every call and the call itself runs per vehicle
        # per frame (2026-09-23 audit).
        if horizontal:
            if abs(oy - cross) >= half_band:
                continue
            centre = ox
        else:
            if abs(ox - cross) >= half_band:
                continue
            centre = oy
        reach = other.length / 2.0
        tail = (centre - sign * reach - start) * sign
        head = (centre + sign * reach - start) * sign
        if head > 0.0 and tail < free:
            free = max(0.0, tail)
    return free


def vehicle_is_inside_any_intersection(vehicle, int_x_list, h_y, road_w):
    """Conservative geometry check used to avoid asking an in-node car to yield."""
    half_w = road_w / 2.0
    if vehicle.direction in ("EB", "WB"):
        front = vehicle.x + vehicle.length / 2.0
        rear = vehicle.x - vehicle.length / 2.0
        return any(rear < node_x + half_w and front > node_x - half_w for node_x in int_x_list)
    front = vehicle.y + vehicle.length / 2.0
    rear = vehicle.y - vehicle.length / 2.0
    return rear < h_y + half_w and front > h_y - half_w


def should_yield_for_route_merge(vehicle, all_vehicles, int_x_list, h_y, road_w):
    """Return True when a target-lane vehicle should open a gap behind a bus.

    Only the vehicle behind the merging bus yields.  Vehicles ahead continue
    discharging and create the forward half of the gap.  A vehicle already in
    an intersection is never stopped by this cooperative rule.
    """
    if vehicle_is_inside_any_intersection(vehicle, int_x_list, h_y, road_w):
        return False

    for bus in buses_in(all_vehicles):
        if bus is vehicle:
            continue
        if not getattr(bus, "route_merge_active", False):
            continue
        if vehicle.direction != bus.direction:
            continue
        desired_y = getattr(bus, "route_merge_desired_y", None)
        if desired_y is None or abs(vehicle.y - desired_y) >= 8.0:
            continue

        if bus.direction == "EB":
            behind = vehicle.x < bus.x
        elif bus.direction == "WB":
            behind = vehicle.x > bus.x
        else:
            behind = False
        if not behind:
            continue

        bumper_gap = abs(vehicle.x - bus.x) - (
            vehicle.length + bus.length
        ) / 2.0
        if bumper_gap <= ROUTE_MERGE_YIELD_DISTANCE_PX:
            return True
    return False


def dbl_merge_bus_blocked_by(vehicle, all_vehicles):
    """Return the bus whose DBL-lane merge `vehicle` is the chosen blocker of.

    A bus names exactly one blocker per frame (the nearest car in its merge
    corridor), so at most one car is ever asked to make room for it.
    """
    for bus in buses_in(all_vehicles):
        if bus is not vehicle and getattr(bus, "dbl_merge_blocker", None) is vehicle:
            return bus
    return None


# Per-run vehicle serial. id() cannot be used to count distinct vehicles
# over a run: CPython reuses the address of a freed object, so a later
# vehicle silently collides with an earlier one and the count comes out low
# and irreproducible (measured: 147-150 counted against 168 actual, and the
# figure moved when unrelated allocations shifted). main.perform_full_reset
# restarts the sequence so two runs of one seed number their vehicles alike.
_serial_counter = itertools.count()


def reset_vehicle_serials():
    global _serial_counter
    _serial_counter = itertools.count()


# Yellow-onset go/stop decision (the ITE "Type I dilemma zone"): a driver
# who cannot stop before the bar from the moment the yellow appears --
# perception-reaction plus braking at the comfortable rate the change
# interval is designed around -- proceeds. These are the same t and a the
# yellow is computed from (webster.ITE_REACTION_SEC / ITE_DECEL_MPS2; ITE,
# "Guidelines for Determining Traffic Signal Change and Clearance
# Intervals", 2020), so a vehicle told to go can in fact reach the bar
# before red at its approach speed, and the all-red covers its clearance.
YELLOW_REACTION_SEC = 1.0
YELLOW_DECEL_MPS2 = 3.05


def ite_stopping_distance_px(speed_px_per_frame):
    """Distance a driver needs to stop from yellow onset, px."""
    v = max(0.0, float(speed_px_per_frame))
    decel = mps2_to_px_per_frame2(YELLOW_DECEL_MPS2)
    return v * YELLOW_REACTION_SEC * FPS + v * v / (2.0 * decel)


class Vehicle:
    def __init__(self, x, y, direction, max_speed=1.0, color=(50, 150, 250), is_heavy=False, target_turn="STRAIGHT", lane_index=2, assigned_node_x=None, left_nodes=None):
        self.x = float(x)
        self.y = float(y)
        self.serial = next(_serial_counter)
        # node_x -> True (go) / False (stop), fixed at the first yellow frame
        # and kept until the next green, so the decision is made once.
        self.yellow_decision = {}
        # Behavioural draws (the lane-change hazard) come from this vehicle's
        # own stream. main keys it on the vehicle's exogenous identity -- its
        # source and arrival index, or its route and trip -- so a vehicle's
        # draws do not depend on the order vehicles update in or on what any
        # other vehicle did: common random numbers extend from demand to
        # behaviour. On the shared global stream, reversing the update order
        # handed every vehicle a different number and swung vehicles served
        # by 20 % on one seed. A vehicle built outside the spawner keeps the
        # module RNG, as before.
        self.behaviour_rng = random
        self.direction = direction
        self.spawn_direction = direction  # origin, kept through turns (trip records)
        # Where the last physics step started, for render interpolation.
        self.prev_x, self.prev_y, self.prev_direction = self.x, self.y, direction
        self.max_speed = max_speed
        self.speed = max_speed
        self.color = color
        self.is_heavy = is_heavy
        self.lane_index = lane_index
        self.target_turn = target_turn
        # Nodes this vehicle turns left at (control_panel.APPROACH_TURN_OPTIONS);
        # when given, target_turn is derived per leg from it, so a car can go
        # straight through one node and turn at the next, or turn twice. None
        # keeps the legacy single-leg target_turn as constructed.
        self.left_nodes = tuple(left_nodes) if left_nodes is not None else None
        self.passed_nodes = set()
        self.length = 28 if is_heavy else 18
        self.width = 12 if is_heavy else 10
        self.passengers = TRUCK_PASSENGERS if is_heavy else CAR_PASSENGERS
        self.leg_state = "APPROACHING"
        self.intersection_entry_approaches = {}
        self.intersection_entry_movements = {}
        self.must_hold_for_lane = False
        self.lane_hold_frames = 0
        self.merge_hold_distance = 35.0
        self.route_merge_hold_active = False
        self.route_exit_merge_blocked = False
        # Cooperative lane change in progress (target lane index), used by a
        # car clearing the DBL lane ahead of a bus or making room for a bus
        # merging into it. None when no lane change is under way.
        self.lane_vacate_target = None
        self.dbl_merge_yield_slow = False
        # NB/SB traffic belongs to one physical vertical road. Pinning that
        # node prevents it from falsely "completing" the remote intersection,
        # which shares the same horizontal y-coordinate.
        self.assigned_node_x = assigned_node_x

    def lane_center_y(self, lane_index, h_y, lane_w=22):
        offset = (lane_index + 0.5) * lane_w
        return h_y - offset if self.direction == "EB" else h_y + offset

    def is_target_lane_clear(self, desired_y, all_vehicles):
        return not corridor_blockers(self, desired_y, all_vehicles)

    def is_lane_band_clear(self, desired_y, all_vehicles, h_y, lane_w=22):
        origin_y = self.lane_center_y(self.lane_index, h_y, lane_w)
        return not lane_band_blockers(self, desired_y, all_vehicles, origin_y, lane_w)

    def choose_vacate_lane(self, candidate_lanes, h_y, lane_w, all_vehicles):
        """First lane in `candidate_lanes` with nobody alongside in it."""
        for lane_index in candidate_lanes:
            if lane_index == self.lane_index:
                continue
            desired_y = self.lane_center_y(lane_index, h_y, lane_w)
            if self.is_lane_band_clear(desired_y, all_vehicles, h_y, lane_w):
                return lane_index
        return None

    def step_lane_vacate(self, int_x_list, h_y, road_w, lane_w, all_vehicles):
        """Advance an in-progress cooperative lane change by one small step.

        The move pauses (never reverses, never jumps) while the corridor is
        occupied or the vehicle is inside an intersection box, and resumes
        once it is clear again, so a started change always completes.
        """
        if self.lane_vacate_target is None:
            return
        if self.direction not in ("EB", "WB"):
            self.lane_vacate_target = None
            return
        if vehicle_is_inside_any_intersection(self, int_x_list, h_y, road_w):
            return
        desired_y = self.lane_center_y(self.lane_vacate_target, h_y, lane_w)
        # Lateral safety only: the leader ahead in the lane being left is
        # already handled by the following law (it overlaps the mover's y
        # for the whole slide).
        if not self.is_lane_band_clear(desired_y, all_vehicles, h_y, lane_w):
            return
        step = lane_change_step_px(lane_w)
        if abs(self.y - desired_y) > 1.0:
            self.y += step if self.y < desired_y else -step
        else:
            self.y = desired_y
            self.lane_index = self.lane_vacate_target
            self.lane_vacate_target = None

    def get_next_target_node(self, int_x_list):
        sorted_nodes = sorted(int_x_list)
        if self.direction == "EB":
            upcoming = [nx for nx in sorted_nodes if nx not in self.passed_nodes]
            return upcoming[0] if upcoming else sorted_nodes[-1]
        elif self.direction == "WB":
            upcoming = [nx for nx in sorted_nodes if nx not in self.passed_nodes]
            return upcoming[-1] if upcoming else sorted_nodes[0]
        else:
            if self.assigned_node_x not in sorted_nodes:
                self.assigned_node_x = min(sorted_nodes, key=lambda cx: abs(self.x - cx))
            return self.assigned_node_x

    def needs_turn_lane(self, dist_to_stop):
        """A left-turner on the approach to its node must be in lane 2."""
        return self.target_turn == "LEFT" and 0.0 <= dist_to_stop <= TURN_LANE_MERGE_PX

    def positions_for_turn(self, dist_to_stop):
        """Whether a left-turner should be working its way into lane 2 now.
        Within TURN_LANE_MERGE_PX of its node, or -- for a far-node turn, one
        that has already passed a node -- anywhere on the link after it, as a
        driver positions for the next junction: two lane changes of
        LANE_CHANGE_DURATION_S do not fit in 62 m of queued road. (The DBL
        eviction exemption stays needs_turn_lane.)"""
        if self.target_turn != "LEFT" or dist_to_stop < 0.0:
            return False
        return dist_to_stop <= TURN_LANE_MERGE_PX or bool(self.passed_nodes)

    def follow_target_speed(self, lead_dist):
        """Speed the following law aims for at this gap to the leader."""
        if lead_dist < SAFE_GAP_PX:
            return 0.0
        if lead_dist < FOLLOW_FREE_GAP_PX:
            return min(self.max_speed, (lead_dist / 30.0) * self.max_speed)
        return self.max_speed

    def rear_vehicle_gap(self, all_vehicles, at_y):
        return self.rear_vehicle(all_vehicles, at_y)[0]

    def rear_vehicle(self, all_vehicles, at_y):
        """(bumper gap, vehicle) to the nearest same-direction vehicle behind,
        in the lane centred on ``at_y``; (inf, None) when it is empty behind."""
        gap, follower = float("inf"), None
        for other in all_vehicles or []:
            if other is self:
                continue
            ox, oy, odir, _ = _seen(other)
            if odir != self.direction or abs(oy - at_y) >= 8.0:
                continue
            if self.direction == "EB":
                behind = ox < self.x
            else:
                behind = ox > self.x
            if behind:
                candidate = abs(ox - self.x) - (other.length + self.length) / 2.0
                if candidate < gap:
                    gap, follower = candidate, other
        return gap, follower

    @staticmethod
    def _idm_accel_between(follower, leader):
        """IDM acceleration of ``follower`` behind ``leader`` (None = free)."""
        T, a, b = idm_params_px(follower)
        fx, _, _, fv = _seen(follower)
        if leader is None:
            gap, dv = float("inf"), 0.0
        else:
            lx, _, _, lv = _seen(leader)
            gap = abs(lx - fx) - (leader.length + follower.length) / 2.0
            dv = fv - lv
        return idm_acceleration(
            fv, follower.max_speed, max(gap, 0.0), dv, T, a, b
        )

    def choose_discretionary_lane_mobil(self, h_y, lane_w, all_vehicles):
        """Full MOBIL (Kesting, Treiber & Helbing 2007) on IDM accelerations:
        safety -- the new follower is never forced below -MOBIL_SAFE_DECEL;
        incentive -- own gain plus politeness x (new + old follower change)
        beats the threshold, with the keep-outer bias for an inward move."""
        _, lead_now = self.get_lead_vehicle(all_vehicles)
        _, old_follower = self.rear_vehicle(all_vehicles, self.y)
        accel = self._idm_accel_between
        a_c = accel(self, lead_now)
        threshold = mps2_to_px_per_frame2(MOBIL_THRESHOLD_MPS2)
        bias = mps2_to_px_per_frame2(MOBIL_KEEP_OUTER_BIAS_MPS2)
        b_safe = mps2_to_px_per_frame2(MOBIL_SAFE_DECEL_MPS2)
        for lane_index in DISCRETIONARY_LANES:
            if lane_index == self.lane_index:
                continue
            desired_y = self.lane_center_y(lane_index, h_y, lane_w)
            if not self.is_lane_band_clear(desired_y, all_vehicles, h_y, lane_w):
                continue
            _, lead_there = self.get_lead_vehicle(all_vehicles, at_y=desired_y)
            _, new_follower = self.rear_vehicle(all_vehicles, desired_y)
            a_n_new = accel(new_follower, self) if new_follower else 0.0
            if a_n_new < -b_safe:
                continue
            a_n = accel(new_follower, lead_there) if new_follower else 0.0
            a_o = accel(old_follower, self) if old_follower else 0.0
            a_o_new = accel(old_follower, lead_now) if old_follower else 0.0
            incentive = (accel(self, lead_there) - a_c) + MOBIL_POLITENESS * (
                (a_n_new - a_n) + (a_o_new - a_o)
            )
            inward = lane_index > self.lane_index
            if incentive > threshold + (bias if inward else -bias):
                return lane_index
        return None

    def choose_discretionary_lane(self, h_y, lane_w, all_vehicles):
        """MOBIL-lite: the other general lane, if it is safe and pays."""
        if is_idm():
            return self.choose_discretionary_lane_mobil(h_y, lane_w, all_vehicles)
        current = self.follow_target_speed(self.get_lead_vehicle_distance(all_vehicles))
        for lane_index in DISCRETIONARY_LANES:
            if lane_index == self.lane_index:
                continue
            desired_y = self.lane_center_y(lane_index, h_y, lane_w)
            # Safety: nobody alongside in that lane, and the new follower is
            # not pushed into its braking band.
            if not self.is_lane_band_clear(desired_y, all_vehicles, h_y, lane_w):
                continue
            if self.rear_vehicle_gap(all_vehicles, desired_y) < LANE_CHANGE_REAR_GAP_PX:
                continue
            there = self.follow_target_speed(
                self.get_lead_vehicle_distance(all_vehicles, at_y=desired_y)
            )
            gain = (there - current) / self.max_speed
            inward = lane_index > self.lane_index
            threshold = (
                LANE_CHANGE_GAIN + LANE_CHANGE_KEEP_OUTER_BIAS
                if inward
                else -LANE_CHANGE_KEEP_OUTER_BIAS
            )
            if gain > threshold:
                return lane_index
        return None

    def get_lead_vehicle_distance(self, all_vehicles, at_y=None):
        return self.get_lead_vehicle(all_vehicles, at_y)[0]

    def get_lead_vehicle(self, all_vehicles, at_y=None):
        """(bumper gap, vehicle) to the nearest vehicle ahead in the lane
        centred on ``at_y`` (own y by default); (inf, None) on a free road."""
        if not all_vehicles: return float('inf'), None
        min_dist = float('inf')
        leader = None
        my_half_w = self.width / 2.0
        my_y = self.y if at_y is None else at_y

        # The grid narrows WHICH vehicles are considered, never how one is
        # judged: _gap_ahead below is the same test the full scan applies,
        # over the same list order (_lead_candidates sorts by it).
        candidates = _lead_candidates(self, all_vehicles, my_y, my_half_w)
        for other in (all_vehicles if candidates is None else candidates):
            dist = _gap_ahead(self, other, my_y, my_half_w)
            if dist is not None and dist < min_dist:
                min_dist, leader = dist, other

        return min_dist, leader

    def is_front_bumper_upstream(self, target_node_x, h_y=300, road_w=132, stop_offset=10):
        half_w = road_w // 2
        fx = self.x + self.length / 2.0 if self.direction == "EB" else self.x - self.length / 2.0
        fy = self.y + self.length / 2.0 if self.direction == "SB" else self.y - self.length / 2.0

        if self.direction == "EB": return fx < (target_node_x - half_w - stop_offset)
        elif self.direction == "WB": return fx > (target_node_x + half_w + stop_offset)
        elif self.direction == "NB": return fy > (h_y + half_w + stop_offset)
        elif self.direction == "SB": return fy < (h_y - half_w - stop_offset)
        return False

    def distance_to_node_stop_bar(self, target_node_x, h_y=300, road_w=132, stop_offset=10):
        half_w = road_w // 2
        if self.direction == "EB": return (target_node_x - half_w - stop_offset) - (self.x + self.length / 2.0)
        elif self.direction == "WB": return (self.x - self.length / 2.0) - (target_node_x + half_w + stop_offset)
        elif self.direction == "NB": return (self.y - self.length / 2.0) - (h_y + half_w + stop_offset)
        elif self.direction == "SB": return (h_y - half_w - stop_offset) - (self.y + self.length / 2.0)
        return 0.0

    def receiving_space_px(self, target_node_x, all_vehicles, h_y=300, road_w=132, lane_w=22):
        """Free road in the lane this vehicle's movement enters after the node."""
        lane_cross = self.y if self.direction in ("EB", "WB") else self.x
        return receiving_space_px(
            target_node_x, self.direction, self.target_turn, lane_cross,
            all_vehicles, h_y, road_w, lane_w,
        )

    def is_spillback_blocked(self, target_node_x, h_y, road_w, all_vehicles):
        """No room for this vehicle in its receiving lane beyond the node."""
        return (
            self.receiving_space_px(target_node_x, all_vehicles, h_y, road_w)
            < self.length + SAFE_GAP_PX
        )

    def update(self, signal_data, int_x_list, h_y, road_w=132, stop_offset=10, lane_w=22, all_vehicles=None, signal_controller=None):
        if all_vehicles is None: all_vehicles = []
        self.prev_x, self.prev_y, self.prev_direction = self.x, self.y, self.direction
        target_node_x = self.get_next_target_node(int_x_list)
        if self.left_nodes is not None:
            self.target_turn = (
                "LEFT"
                if target_node_x in self.left_nodes and target_node_x not in self.passed_nodes
                else "STRAIGHT"
            )
        node_signals = signal_data.get(target_node_x, {})
        half_w = road_w // 2
        should_stop = False

        upstream = self.is_front_bumper_upstream(target_node_x, h_y, road_w, stop_offset)
        
        if upstream:
            self.leg_state = "APPROACHING"
        elif self.leg_state == "APPROACHING" and target_node_x not in self.passed_nodes:
            self.leg_state = "TURNING" if self.target_turn == "LEFT" else "IN_INTERSECTION"

        # 1. UPSTREAM DBL YIELDING. Once DBL is armed, every non-bus vehicle
        #    in the reserved approach lane is ordered out immediately, ahead
        #    of or behind the priority bus. A safe lane change is never forced:
        #    a vehicle that cannot vacate keeps moving when ahead of the bus,
        #    or holds behind it, and retries every frame.
        self.dbl_merge_yield_slow = False
        if not isinstance(self, Bus) and signal_controller:
            dbl_request = signal_controller.get_active_dbl_request(
                target_node_x, self.direction
            )
            if upstream and dbl_request and self.lane_index == dbl_request["entry_lane"]:
                dist_to_stop = self.distance_to_node_stop_bar(target_node_x, h_y, road_w, stop_offset)
                eligibility_px = signal_controller.get_priority_eligibility_px()
                if 0.0 <= dist_to_stop <= eligibility_px:
                    priority_bus = next(
                        (
                            other
                            for other in all_vehicles
                            if isinstance(other, Bus)
                            and other.bus_id == dbl_request["bus_id"]
                        ),
                        None,
                    )
                    if (
                        self.lane_vacate_target is None
                        and self.lane_index == DBL_LANE_INDEX
                        and not self.needs_turn_lane(dist_to_stop)
                    ):
                        # Only start a change that can finish before the stop
                        # bar, so the car never enters the box mid-lane.
                        run_out_px = (lane_w / lane_change_step_px(lane_w)) * self.max_speed
                        if dist_to_stop >= run_out_px:
                            self.lane_vacate_target = self.choose_vacate_lane(
                                (1, 0), h_y, lane_w, all_vehicles
                            )
                    # A commanded lane (AI Configured) has no bus to hold
                    # for: a car that cannot vacate drives on.
                    if self.lane_vacate_target is None and not dbl_request.get("commanded") and (
                        priority_bus is None
                        or not _is_ahead_on_approach(priority_bus, self)
                    ):
                        should_stop = True

        # 1b. DBL MERGE GAP. A bus moving into the DBL lane names the one car
        #     blocking its corridor. Ahead of the bus, that car tries to leave
        #     the DBL lane (else drives on normally); behind or alongside, it
        #     eases off so the bus pulls clear. Never inside an intersection.
        if not isinstance(self, Bus) and self.direction in ("EB", "WB"):
            merging_bus = dbl_merge_bus_blocked_by(self, all_vehicles)
            if merging_bus and not vehicle_is_inside_any_intersection(
                self, int_x_list, h_y, road_w
            ):
                if merging_bus.blocker_is_ahead(self):
                    if (
                        self.lane_vacate_target is None
                        and self.lane_index == DBL_LANE_INDEX
                        and not self.needs_turn_lane(
                            self.distance_to_node_stop_bar(target_node_x, h_y, road_w, stop_offset)
                        )
                    ):
                        self.lane_vacate_target = self.choose_vacate_lane(
                            (1, 0), h_y, lane_w, all_vehicles
                        )
                else:
                    self.dbl_merge_yield_slow = True

        # 1c. DISCRETIONARY LANE CHANGE. A straight car in a general lane,
        #     upstream and clear of the stop-bar approach, occasionally asks
        #     whether the other general lane pays (choose_discretionary_lane)
        #     and rides the same cooperative slide as a DBL eviction.
        if (
            not isinstance(self, Bus)
            and self.direction in ("EB", "WB")
            and self.target_turn == "STRAIGHT"
            and self.lane_vacate_target is None
            and self.lane_index in DISCRETIONARY_LANES
            and upstream
            and self.distance_to_node_stop_bar(target_node_x, h_y, road_w, stop_offset)
            > LANE_CHANGE_MIN_DIST_TO_BAR_PX
            and self.behaviour_rng.random() < LANE_CHANGE_HAZARD_PER_FRAME
        ):
            self.lane_vacate_target = self.choose_discretionary_lane(
                h_y, lane_w, all_vehicles
            )

        # 1d. TURN LANE. A car turning left at this node moves into lane 2 on
        #     the approach (it arrived in a general lane for a far-node turn,
        #     or a DBL eviction put it out) and holds at the bar until it is.
        #     While DBL holds that lane for a bus, the car stays in its
        #     general lane and holds at the bar: nothing but the priority bus
        #     may enter the reserved lane (the spawner retains new left
        #     turners at the source for the same reason).
        if not isinstance(self, Bus):
            self.must_hold_for_lane = False
            if (
                self.target_turn == "LEFT"
                and self.direction in ("EB", "WB")
                and self.lane_index != DBL_LANE_INDEX
                and upstream
                and self.positions_for_turn(
                    self.distance_to_node_stop_bar(target_node_x, h_y, road_w, stop_offset)
                )
            ):
                self.must_hold_for_lane = True
                if not (
                    signal_controller
                    and signal_controller.dbl_excludes_left_turns(
                        target_node_x, self.direction
                    )
                ):
                    self.lane_vacate_target = DBL_LANE_INDEX

        self.step_lane_vacate(int_x_list, h_y, road_w, lane_w, all_vehicles)

        # A bus changing lanes between route legs owns a small cooperative
        # merge gap.  Only traffic behind it in the target lane yields; traffic
        # ahead keeps moving so this cannot freeze both sides of the gap.
        if should_yield_for_route_merge(
            self, all_vehicles, int_x_list, h_y, road_w
        ):
            should_stop = True

        # Cooperative holds set above (hold behind a DBL bus, yield to a bus's
        # route merge) mean "stop where you are", not "stop at the next bar".
        # The legacy engine stops on the spot either way; under IDM the bar
        # logic below would have turned them into a gentle brake for a line
        # hundreds of pixels away -- a yield that never yielded. They are kept
        # apart and applied after the bar logic as a comfortable stop.
        cooperative_hold = should_stop
        if is_idm():
            should_stop = False

        # 2. SIGNAL YIELDING & DOWNSTREAM SPILLBACK
        # IDM engine: the stop bar becomes a standing obstacle the vehicle
        # brakes for at its comfortable rate -- on red and yellow (nothing
        # enters on yellow without a reservation, as in the legacy engine;
        # a too-short yellow still ends in a hard stop, as it does for a
        # real driver) and on green when its receiving lane is full --
        # instead of only the legacy hard stop inside 15-25 px. A hold set
        # below (a refused entry, a lane hold) is likewise braked for.
        bar_obstacle_gap = hold_gap = None
        if target_node_x not in self.passed_nodes and upstream:
            dist_to_stop = self.distance_to_node_stop_bar(target_node_x, h_y, road_w, stop_offset)
            sig_state = node_signals.get(self.direction, "RED")
            if not isinstance(sig_state, str) or sig_state.upper() not in {
                "RED", "YELLOW", "GREEN"
            }:
                sig_state = "RED"
            else:
                sig_state = sig_state.upper()

            # Dilemma zone. The go/stop choice is made once, at the first
            # yellow frame, and held through the red that follows (a vehicle
            # committed to go does not change its mind at the bar); a green
            # clears it. A committed vehicle is treated as facing green: it
            # requests its reservation like any other entrant, so the
            # controller's conflict check -- not the lamp -- still decides
            # whether the box is safe to enter.
            if sig_state == "GREEN":
                self.yellow_decision.pop(target_node_x, None)
            elif sig_state == "YELLOW" and target_node_x not in self.yellow_decision:
                self.yellow_decision[target_node_x] = (
                    dist_to_stop < ite_stopping_distance_px(self.speed)
                )
            if self.yellow_decision.get(target_node_x) and self.speed <= 1e-3:
                # Stopped short of the bar after all (refused the box, held
                # for its lane): no dilemma is left, so it waits for the next
                # green. A kept commitment let it book the box on red.
                self.yellow_decision[target_node_x] = False
            if self.yellow_decision.get(target_node_x) and sig_state != "GREEN":
                sig_state = "GREEN"

            # Near-side turns (left-hand traffic) run with their approach's
            # green like through traffic: no turn on red at a signal without a
            # green filter arrow (TSRGD 2016; Highway Code rule 175). They used
            # to ignore the lamp and book the box whenever no reserved vehicle
            # conflicted -- 30 of 61 turns entered on red at 0.6x demand, across
            # the cross street's green, and the all-red then waited for them
            # (median 13 s against 3.5 s at Node A; 2026-09-23 audit).
            if sig_state != "GREEN" and signal_controller:
                signal_controller.cancel_intersection_entry(self, target_node_x)

            if sig_state in ("RED", "YELLOW") and dist_to_stop <= 15.0:
                should_stop = True

            if (
                sig_state == "GREEN"
                and 0.0 <= dist_to_stop <= ENTRY_ZONE_PX
                and not getattr(self, "dwelling", False)
            ):
                if self.route_exit_merge_blocked:
                    should_stop = True
                elif self.must_hold_for_lane and dist_to_stop <= self.merge_hold_distance:
                    # Held for its turn lane: it cannot go, so it books nothing.
                    should_stop = True
                elif self.is_spillback_blocked(target_node_x, h_y, road_w, all_vehicles):
                    should_stop = True
                elif signal_controller and not signal_controller.request_intersection_entry(
                    self, target_node_x, all_vehicles
                ):
                    should_stop = True

            if self.must_hold_for_lane and 0.0 <= dist_to_stop <= self.merge_hold_distance:
                should_stop = True
                self.lane_hold_frames += 1
                if self.lane_hold_frames >= MISSED_TURN_HOLD_FRAMES and self.left_nodes is not None:
                    # Missed turn: straight on from the next frame.
                    self.left_nodes = tuple(n for n in self.left_nodes if n != target_node_x)
                    self.lane_vacate_target = None
                    self.lane_hold_frames = 0
                    SAFETY_COUNTERS["missed_turns"] += 1
            else:
                self.lane_hold_frames = 0

            if is_idm():
                if should_stop:
                    hold_gap = dist_to_stop
                brake_px = comfortable_stop_px(self.speed, idm_params_px(self)[2])
                if sig_state != "GREEN":
                    bar_obstacle_gap = dist_to_stop
                elif dist_to_stop <= brake_px + ENTRY_ZONE_PX and (
                    self.is_spillback_blocked(target_node_x, h_y, road_w, all_vehicles)
                    or (
                        signal_controller is not None
                        and dist_to_stop > ENTRY_ZONE_PX
                        and hasattr(signal_controller, "entry_would_be_granted")
                        and not signal_controller.entry_would_be_granted(
                            self, target_node_x, all_vehicles
                        )
                    )
                ):
                    # Look-ahead: the box will not open for this vehicle, so
                    # brake for the bar at the comfortable rate now rather
                    # than be refused at the booking distance.
                    bar_obstacle_gap = dist_to_stop

        # This hold point is on the link immediately after the previous node,
        # not at the next intersection. It keeps a bus with an unresolved
        # route-required merge out of the next node's middle-lane queue.
        if self.route_merge_hold_active:
            should_stop = True

        if cooperative_hold and is_idm():
            # Stop now at the comfortable rate: an obstacle exactly one
            # comfortable braking distance ahead, re-placed every frame.
            brake_gap = comfortable_stop_px(self.speed, idm_params_px(self)[2]) + IDM_S0_PX
            hold_gap = brake_gap if hold_gap is None else min(hold_gap, brake_gap)
            should_stop = True

        # A point this vehicle must stop at off the stop-bar logic (a bus
        # stop): braked for like a hold, and a full stop while dwelling.
        external = getattr(self, "external_hold_gap", None)
        if external is not None:
            if is_idm():
                hold_gap = external if hold_gap is None else min(hold_gap, external)
                if getattr(self, "dwelling", False):
                    should_stop = True
            elif external <= 15.0:
                should_stop = True

        # 3. KINEMATICS
        lead_dist, leader = self.get_lead_vehicle(all_vehicles)
        lead_dist = max(0.0, lead_dist)
        self._record_ttc(lead_dist, leader)
        # A vehicle that must stop at a line it has no reservation for may
        # not move past it, whatever speed the braking bound left it with.
        stop_line_limit = hold_gap if (should_stop and hold_gap is not None) else None
        if is_idm():
            should_stop = self._idm_step(
                lead_dist, leader, should_stop, hold_gap, bar_obstacle_gap
            )
        else:
            target_speed = self.follow_target_speed(lead_dist)
            if target_speed <= 0.0 or should_stop:
                should_stop = True
                self.speed = 0.0
            elif self.speed > target_speed:
                self.speed = max(target_speed, self.speed - LEGACY_ACCEL_PX_PER_FRAME2)
            else:
                # Ramp toward the gap-proportional (or free-flow) target: a
                # queued vehicle pulls away as soon as the one ahead does,
                # instead of sitting until a full FOLLOW_FREE_GAP_PX opens up.
                self.speed = min(target_speed, self.speed + LEGACY_ACCEL_PX_PER_FRAME2)

        if self.dbl_merge_yield_slow and not should_stop:
            self.speed = min(self.speed, self.max_speed * DBL_MERGE_YIELD_SPEED_RATIO)

        if should_stop and upstream and signal_controller:
            signal_controller.cancel_intersection_entry(self, target_node_x)

        # 4. TURN TRIGGERS & CONTINUOUS MOVEMENT
        # Recovery layer, counted. The following law (IDM within its braking
        # bound) is meant to leave these limits slack; when it cannot, the
        # move is truncated so nothing overlaps, and the event is recorded as
        # the collision SUMO would detect -- never silently absorbed.
        step_dist = self.speed
        if lead_dist < float('inf') and step_dist > lead_dist:
            step_dist = lead_dist
            SAFETY_COUNTERS["leader_clamp_frames"] += 1
        # Strictly short of the line: a front bumper that reached it exactly
        # would stop counting as upstream, drop out of the stop-bar logic and
        # enter the box without a reservation (tests/test_vehicle_safety.py
        # fail-closed cases caught exactly that).
        line = None if stop_line_limit is None else max(0.0, stop_line_limit - STOP_LINE_EPSILON_PX)
        if line is not None and step_dist > line:
            step_dist = line
            SAFETY_COUNTERS["stop_line_clamp_frames"] += 1
        if step_dist < self.speed:
            self.speed = step_dist
        turned = False

        if self.target_turn == "LEFT" and target_node_x not in self.passed_nodes:
            lane_offset = 2.5 * lane_w
            
            if isinstance(self, Bus) and self.lane_index != 2 and not upstream:
                self.speed = 0.0
                step_dist = 0.0
            else:
                if self.direction == "EB":
                    pivot_x = target_node_x - lane_offset
                    if self.x <= pivot_x and (self.x + step_dist) >= pivot_x:
                        overshoot = (self.x + step_dist) - pivot_x
                        self.x = pivot_x
                        self.direction = "NB"
                        self.assigned_node_x = target_node_x
                        self.y -= overshoot
                        turned = True
                        step_dist = 0.0
                elif self.direction == "WB":
                    pivot_x = target_node_x + lane_offset
                    if self.x >= pivot_x and (self.x - step_dist) <= pivot_x:
                        overshoot = pivot_x - (self.x - step_dist)
                        self.x = pivot_x
                        self.direction = "SB"
                        self.assigned_node_x = target_node_x
                        self.y += overshoot
                        turned = True
                        step_dist = 0.0
                elif self.direction == "NB":
                    pivot_y = h_y + lane_offset
                    if self.y >= pivot_y and (self.y - step_dist) <= pivot_y:
                        overshoot = pivot_y - (self.y - step_dist)
                        self.y = pivot_y
                        self.direction = "WB"
                        self.assigned_node_x = target_node_x
                        self.x -= overshoot
                        turned = True
                        step_dist = 0.0
                elif self.direction == "SB":
                    pivot_y = h_y - lane_offset
                    if self.y <= pivot_y and (self.y + step_dist) >= pivot_y:
                        overshoot = (self.y + step_dist) - pivot_y
                        self.y = pivot_y
                        self.direction = "EB"
                        self.assigned_node_x = target_node_x
                        self.x += overshoot
                        turned = True
                        step_dist = 0.0

        if step_dist > 0.0:
            if self.direction == "EB": self.x += step_dist
            elif self.direction == "WB": self.x -= step_dist
            elif self.direction == "NB": self.y -= step_dist
            elif self.direction == "SB": self.y += step_dist

        if turned:
            self.leg_state = "DEPARTING"
            # The square-corner pivot lands on the exit road's lane 2, so a
            # second left (B_NB / A_SB double-left) starts from the turn lane.
            self.lane_index = DBL_LANE_INDEX

        rx = self.x - self.length / 2.0 if self.direction == "EB" else self.x + self.length / 2.0
        ry = self.y - self.length / 2.0 if self.direction == "SB" else self.y + self.length / 2.0

        conflict_cleared = False
        if self.direction == "EB" and rx > target_node_x + half_w: conflict_cleared = True
        elif self.direction == "WB" and rx < target_node_x - half_w: conflict_cleared = True
        elif self.direction == "NB" and ry < h_y - half_w: conflict_cleared = True
        elif self.direction == "SB" and ry > h_y + half_w: conflict_cleared = True

        if conflict_cleared and target_node_x not in self.passed_nodes:
            self.passed_nodes.add(target_node_x)
            self.leg_state = "COMPLETE"
            if not getattr(self, "is_heavy", False):
                self.target_turn = "STRAIGHT"

    def _record_ttc(self, lead_dist, leader):
        """Count frames and conflicts with TTC to the leader under the SSAM
        threshold. Only a closing, moving follower has a finite TTC."""
        closing = self.speed - (_seen(leader)[3] if leader is not None else 0.0)
        in_conflict = bool(
            leader is not None and self.speed > 0.0 and closing > 1e-9
            and lead_dist / closing < SSM_TTC_THRESHOLD_SEC * FPS
        )
        if in_conflict:
            SAFETY_COUNTERS["ttc_conflict_frames"] += 1
            if not getattr(self, "_in_ttc_conflict", False):
                SAFETY_COUNTERS["ttc_conflicts"] += 1
        self._in_ttc_conflict = in_conflict

    def _idm_step(self, lead_dist, leader, should_stop, hold_gap, bar_obstacle_gap):
        """One IDM speed update. The leader, a hold point and the stop bar
        (when it is an obstacle) compete for the smallest gap; a hold with
        no bar reference, or one the vehicle has reached, is a full stop.
        Returns the stop flag for the reservation bookkeeping."""
        T, a, b = idm_params_px(self)
        v = self.speed
        gap, dv = lead_dist, v - (_seen(leader)[3] if leader is not None else 0.0)
        if should_stop and (hold_gap is None or hold_gap <= IDM_S0_PX):
            wanted = 0.0
        else:
            for obstacle in (hold_gap, bar_obstacle_gap):
                if obstacle is not None and obstacle < gap:
                    gap, dv = obstacle, v
            accel = idm_acceleration(v, self.max_speed, gap, dv, T, a, b)
            wanted = min(self.max_speed, max(0.0, v + accel))
        floor = max(0.0, v - emergency_decel_px(self))
        at_bound = wanted < floor
        if at_bound:
            SAFETY_COUNTERS["emergency_decel_frames"] += 1
            if not getattr(self, "_at_braking_bound", False):
                SAFETY_COUNTERS["emergency_decel_events"] += 1
            wanted = floor
        self._at_braking_bound = at_bound
        self.speed = wanted
        return should_stop or self.speed <= 0.0

    def render_position(self, alpha=1.0):
        """Position to draw at, ``alpha`` (0-1) of the way from the previous
        physics step to the current one. A pivot (direction change) is not
        interpolated: the two positions are on different roads."""
        if alpha >= 1.0 or self.direction != self.prev_direction:
            return self.x, self.y
        return (
            self.prev_x + (self.x - self.prev_x) * alpha,
            self.prev_y + (self.y - self.prev_y) * alpha,
        )

    def body_rect(self, alpha=1.0, view=None, scale=1.0):
        """Axis-aligned body in draw space: world px, or view px when the
        view is zoomed past 1 world px per screen px (``view`` is the world
        rect on show, ``scale`` its screen px per world px), so a vehicle at
        a fractional world x lands on the right screen pixel instead of the
        nearest whole world pixel."""
        x, y = self.render_position(alpha)
        along, across = (
            (self.length, self.width) if self.direction in ("EB", "WB")
            else (self.width, self.length)
        )
        if view is not None:
            x, y = (x - view.x) * scale, (y - view.y) * scale
            along, across = along * scale, across * scale
        return pygame.Rect(
            round(x - along / 2.0), round(y - across / 2.0), round(along), round(across)
        )

    def draw(self, screen, alpha=1.0, view=None, scale=1.0):
        rect = self.body_rect(alpha, view, scale)
        pygame.draw.rect(screen, self.color, rect, border_radius=max(1, round(3 * scale)))


# Bus stops. A near-side stop holds the bus this far short of the stop bar
# (front bumper), beyond the reservation booking distance, so a dwelling bus
# never holds the conflict box; a far-side stop puts the bus's rear this far
# clear of the box on the exit road. Arrival: within this gap of the point,
# standing.
NEAR_SIDE_STOP_SETBACK_PX = 30.0
FAR_SIDE_STOP_CLEARANCE_PX = 20.0
STOP_ARRIVAL_TOLERANCE_PX = 4.0


class Bus(Vehicle):
    def __init__(
        self, x, y, direction, route_info, bus_id="BUS_01", max_speed=1.0
    ):
        first_node_x = INT_X[0] if direction == "EB" else INT_X[-1]
        target_turn = route_info.get("waypoints", {}).get(first_node_x, "STRAIGHT")
        super().__init__(
            x=x,
            y=y,
            direction=direction,
            max_speed=max_speed,
            color=(245, 158, 11),
            is_heavy=True,
            target_turn=target_turn,
            lane_index=(2 if target_turn == "LEFT" else 1),
        )
        self.bus_id = bus_id
        self.route_info = route_info
        self.route_id = route_info.get("route_id", "")
        self.length, self.width, self.passengers = 42, 14, BUS_PASSENGERS
        self.route_nodes = sorted(
            route_info.get("waypoints", {}).keys(),
            reverse=(direction == "WB"),
        )
        # Per-leg DBL merge tracking. See the invariant in update().
        self.dbl_merge_hold_frames = 0
        self.dbl_merge_abandoned_for_leg = False
        self._dbl_merge_leg_key = None
        self.route_merge_active = False
        self.route_merge_desired_y = None
        # The single car asked to make room for this bus's DBL-lane merge.
        self.dbl_merge_blocker = None
        # This trip's stops and dwells (main.draw_stop_plan), served in order.
        self.stop_plan = [dict(stop) for stop in route_info.get("stops", []) or []]
        self.dwelling = False
        self.dwell_frames_total = 0
        self.near_side_stop_pending = None
        self.external_hold_gap = None

    def _stop_gap_px(self, stop, h_y, road_w, stop_offset):
        """Front-bumper distance to where this stop wants the front, along the
        current road, or None while the stop is not on the road ahead."""
        node = stop["node"]
        half = road_w / 2.0
        if stop["side"] == "near":
            if node in self.passed_nodes or self.get_next_target_node(INT_X) != node:
                return None
            if not self.is_front_bumper_upstream(node, h_y, road_w, stop_offset):
                return None
            return self.distance_to_node_stop_bar(node, h_y, road_w, stop_offset) - NEAR_SIDE_STOP_SETBACK_PX
        if node not in self.passed_nodes:
            return None
        reach = FAR_SIDE_STOP_CLEARANCE_PX + self.length
        front = {"EB": self.x + self.length / 2.0, "WB": self.x - self.length / 2.0,
                 "NB": self.y - self.length / 2.0, "SB": self.y + self.length / 2.0}[self.direction]
        if self.direction == "EB":
            return (node + half + reach) - front
        if self.direction == "WB":
            return front - (node - half - reach)
        if self.direction == "NB":
            return front - (h_y - half - reach)
        return (h_y + half + reach) - front

    def _update_stop(self, h_y, road_w, stop_offset):
        """Approach, dwell at and leave the next unserved stop. Sets
        external_hold_gap (a point Vehicle.update brakes for), dwelling, and
        near_side_stop_pending (the node whose TSP check-in waits for it)."""
        self.external_hold_gap = None
        self.dwelling = False
        pending = [stop for stop in self.stop_plan if not stop["served"]]
        self.near_side_stop_pending = next(
            (stop["node"] for stop in pending if stop["side"] == "near"), None
        )
        if not pending:
            return
        stop = pending[0]
        gap = self._stop_gap_px(stop, h_y, road_w, stop_offset)
        if gap is None:
            return
        if gap < -self.length:
            stop["served"] = True  # passed it (cannot happen when braked for)
            return
        arrived = gap <= IDM_S0_PX + STOP_ARRIVAL_TOLERANCE_PX and self.speed <= 0.05
        if stop["dwelt"] > 0 or arrived:
            stop["dwelt"] += 1
            self.dwell_frames_total += 1
            if stop["dwelt"] >= stop["dwell_frames"]:
                stop["served"] = True
                if stop["side"] == "near":
                    self.near_side_stop_pending = next(
                        (s["node"] for s in self.stop_plan if not s["served"] and s["side"] == "near"), None
                    )
                return
            self.dwelling = True
            self.external_hold_gap = 0.0
            return
        self.external_hold_gap = max(0.0, gap)

    def get_active_route_leg(self, int_x_list):
        """Return canonical metadata for the next unfinished route leg."""
        for route_leg_index, node_x in enumerate(self.route_nodes):
            if node_x in self.passed_nodes:
                continue
            movement = self.route_info.get("waypoints", {}).get(node_x, "STRAIGHT")
            configured_lanes = self.route_info.get("lanes", {})
            entry_lane = int(configured_lanes.get(node_x, 2 if movement == "LEFT" else 1))
            approach = self.direction
            if movement == "LEFT":
                exit_direction = {
                    "EB": "NB", "WB": "SB", "NB": "WB", "SB": "EB"
                }[approach]
            else:
                exit_direction = approach
            return {
                "route_leg_index": route_leg_index,
                "node_x": node_x,
                "approach": approach,
                "movement": movement,
                "entry_lane": entry_lane,
                "exit_direction": exit_direction,
            }
        return None

    def get_following_route_leg(self, leg):
        """Return the configured leg after ``leg``, if this route has one."""
        if not leg:
            return None
        route_leg_index = leg["route_leg_index"] + 1
        if route_leg_index >= len(self.route_nodes):
            return None
        node_x = self.route_nodes[route_leg_index]
        movement = self.route_info.get("waypoints", {}).get(node_x, "STRAIGHT")
        configured_lanes = self.route_info.get("lanes", {})
        return {
            "route_leg_index": route_leg_index,
            "node_x": node_x,
            "movement": movement,
            "entry_lane": int(
                configured_lanes.get(node_x, 2 if movement == "LEFT" else 1)
            ),
        }

    def route_merge_point_x(self, previous_node_x, road_w):
        """Centre of the post-node area reserved for a required lane merge."""
        clearance = road_w / 2.0 + self.length / 2.0 + ROUTE_MERGE_AREA_PX
        if self.direction == "EB":
            return previous_node_x + clearance
        return previous_node_x - clearance

    def target_lane_has_merge_storage(
        self, previous_node_x, target_lane, h_y, road_w, lane_w, all_vehicles
    ):
        """Check a bus-sized gap at the deterministic post-node merge point."""
        lane_offset = (target_lane + 0.5) * lane_w
        desired_y = h_y - lane_offset if self.direction == "EB" else h_y + lane_offset
        merge_x = self.route_merge_point_x(previous_node_x, road_w)
        for other in all_vehicles or []:
            if other is self or other.direction != self.direction:
                continue
            if abs(other.y - desired_y) >= 8.0:
                continue
            required_gap = (
                self.length + other.length
            ) / 2.0 + ROUTE_MERGE_SAFE_GAP_PX
            if abs(other.x - merge_x) < required_gap:
                return False
        return True

    def reached_route_merge_hold_point(self, previous_node_x, road_w):
        merge_x = self.route_merge_point_x(previous_node_x, road_w)
        if self.direction == "EB":
            return self.x >= merge_x
        return self.x <= merge_x

    def blocker_is_ahead(self, blocker):
        if self.direction == "EB":
            return blocker.x > self.x
        if self.direction == "WB":
            return blocker.x < self.x
        return False

    def update(self, signal_data, int_x_list, h_y, road_w=132, stop_offset=10, lane_w=22, all_vehicles=None, signal_controller=None):
        if all_vehicles is None: all_vehicles = []
        leg = self.get_active_route_leg(int_x_list)
        target_node_x = leg["node_x"] if leg else self.get_next_target_node(int_x_list)
        self.target_turn = leg["movement"] if leg else "STRAIGHT"
        required_lane = leg["entry_lane"] if leg else self.lane_index
        self.must_hold_for_lane = False
        self.route_merge_hold_active = False
        self.route_exit_merge_blocked = False
        self.route_merge_active = False
        self.route_merge_desired_y = None
        self.dbl_merge_blocker = None

        # Before entering the current node, reserve physical storage for a
        # lane change required by the following route leg.  R2/R4 therefore do
        # not cross the first node into a link whose turn lane has no bus-sized
        # gap.  This is route geometry and remains active with DBL/TSP off.
        following_leg = self.get_following_route_leg(leg)
        if (
            leg
            and following_leg
            and following_leg["entry_lane"] != leg["entry_lane"]
            and self.lane_index != following_leg["entry_lane"]
        ):
            self.route_exit_merge_blocked = not self.target_lane_has_merge_storage(
                leg["node_x"],
                following_leg["entry_lane"],
                h_y,
                road_w,
                lane_w,
                all_vehicles,
            )

        # DBL merge state is tracked per leg: a lane blocked at one node must
        # not disable DBL for the rest of the route, where it may be clear.
        leg_key = (leg["node_x"], leg["route_leg_index"]) if leg else None
        if leg_key != self._dbl_merge_leg_key:
            self._dbl_merge_leg_key = leg_key
            self.dbl_merge_hold_frames = 0
            self.dbl_merge_abandoned_for_leg = False

        dist_to_intersection = (
            abs(self.x - target_node_x)
            if self.direction in ("EB", "WB")
            else abs(self.y - h_y)
        )
        dbl_requested_for_leg = bool(
            leg
            and signal_controller
            and signal_controller.is_dbl_requested_for_bus_leg(self, target_node_x)
        )
        dbl_enabled_for_leg = bool(
            dbl_requested_for_leg
            and signal_controller.is_dbl_enabled_for_bus_leg(
                self, target_node_x, all_vehicles
            )
        )
        turn_lane_change_due = (
            self.target_turn == "LEFT" and dist_to_intersection < TURN_LANE_MERGE_PX
        )
        route_leg_merge_due = bool(
            leg
            and leg["route_leg_index"] > 0
            and self.lane_index != required_lane
        )

        # INVARIANT: DBL must never leave a bus worse off than no DBL at all.
        # A merge that stays blocked past DBL_MERGE_ABANDON_FRAMES is given up
        # for this leg, and the bus runs in its configured lane exactly as an
        # unequipped bus would, rather than holding upstream indefinitely.
        if not dbl_requested_for_leg:
            self.dbl_merge_hold_frames = 0
            self.dbl_merge_abandoned_for_leg = False
        elif not dbl_enabled_for_leg:
            # A stopped queue or blocked merge vetoes this activation before
            # the bus is made to hold. Keep that refusal sticky for this leg
            # while the same route flag remains on; otherwise a bus could
            # creep past the observed blocker and retry DBL at the stop bar.
            self.dbl_merge_hold_frames = 0
            self.dbl_merge_abandoned_for_leg = True
        dbl_merge_due = dbl_enabled_for_leg and not self.dbl_merge_abandoned_for_leg

        # A DBL-enabled bus occupies the continuous outer lane as early as
        # traffic permits. Close to a left turn, its configured turn lane wins
        # if that lane ever differs from the DBL lane.
        if turn_lane_change_due:
            target_lane = required_lane
        elif dbl_merge_due:
            target_lane = DBL_LANE_INDEX
        elif route_leg_merge_due:
            target_lane = required_lane
        else:
            target_lane = required_lane
        lane_change_due = (
            dbl_merge_due or turn_lane_change_due or route_leg_merge_due
        ) and self.lane_index != target_lane
        if lane_change_due:
            lane_offset = (target_lane + 0.5) * lane_w
            desired_y = (
                h_y - lane_offset if self.direction == "EB" else h_y + lane_offset
            )
            lane_clear = self.is_target_lane_clear(desired_y, all_vehicles)
            route_required_targeted = (
                route_leg_merge_due and target_lane == required_lane
            )
            if route_required_targeted:
                self.route_merge_active = True
                self.route_merge_desired_y = desired_y
            if lane_clear:
                step = lane_change_step_px(lane_w)
                if abs(self.y - desired_y) > 1.0:
                    self.y += step if self.y < desired_y else -step
                else:
                    self.y = desired_y
                    self.lane_index = target_lane
            elif dbl_merge_due and target_lane == DBL_LANE_INDEX:
                # Ask the nearest car in the DBL-lane corridor to make room
                # (see Vehicle.update step 1b). The bus itself still waits
                # until the corridor is actually clear; if no car can move
                # safely this is exactly the existing hold-and-abandon path.
                cars = [
                    item
                    for item in corridor_blockers(self, desired_y, all_vehicles)
                    if not isinstance(item, Bus)
                ]
                if cars:
                    self.dbl_merge_blocker = min(
                        cars, key=lambda item: abs(item.x - self.x)
                    )
            if self.lane_index != target_lane:
                self.must_hold_for_lane = True

            if route_required_targeted and self.lane_index != target_lane:
                previous_node_x = self.route_nodes[leg["route_leg_index"] - 1]
                blockers = corridor_blockers(self, desired_y, all_vehicles)
                # A follower in the target lane is asked to yield and the bus
                # may continue forward to open the gap.  A blocker ahead means
                # downstream storage is unavailable, so stop at the merge-area
                # boundary rather than queueing in the next node's middle lane.
                blocked_ahead = any(self.blocker_is_ahead(item) for item in blockers)
                if blocked_ahead and self.reached_route_merge_hold_point(
                    previous_node_x, road_w
                ):
                    self.route_merge_hold_active = True
                elif getattr(self, "_route_merge_holding", False) and not lane_clear:
                    # Once waiting, wait until the merge can happen. A bus
                    # braking at a physical rate rolls on past the blocker
                    # that started the hold; dropping the hold then would
                    # carry the unresolved merge to the next stop bar, which
                    # is what the hold exists to prevent.
                    self.route_merge_hold_active = True

            # Only a DBL-driven merge may be abandoned. A LEFT turn genuinely
            # needs its turn lane, and a leg whose configured lane already is
            # the DBL lane would block identically with DBL switched off, so
            # neither case is one that DBL made worse.
            abandonable = (
                dbl_merge_due
                and not turn_lane_change_due
                and not route_required_targeted
                and required_lane != DBL_LANE_INDEX
            )
            if abandonable and not lane_clear:
                self.dbl_merge_hold_frames += 1
                if self.dbl_merge_hold_frames > DBL_MERGE_ABANDON_FRAMES:
                    self.dbl_merge_abandoned_for_leg = True
                    # Settle fully back into the configured lane so the bus
                    # stops holding and the priority feasibility gate, which
                    # compares lane_index against the request entry lane, can
                    # still grant TSP to a bus that never got its DBL merge.
                    self.must_hold_for_lane = False
                    normal_offset = (required_lane + 0.5) * lane_w
                    self.y = (
                        h_y - normal_offset
                        if self.direction == "EB"
                        else h_y + normal_offset
                    )
                    self.lane_index = required_lane

        if dbl_enabled_for_leg and self.lane_index == DBL_LANE_INDEX:
            self.dbl_merge_hold_frames = 0
        # Every frame, so a hold never outlives the merge (or leg) it was for.
        self._route_merge_holding = self.route_merge_hold_active

        self._update_stop(h_y, road_w, stop_offset)
        super().update(signal_data, int_x_list, h_y, road_w, stop_offset, lane_w, all_vehicles, signal_controller)

    def draw(self, screen, alpha=1.0, view=None, scale=1.0):
        super().draw(screen, alpha, view, scale)
        inner_rect = self.body_rect(alpha, view, scale)
        inner_rect.inflate_ip(-inner_rect.width // 2, -inner_rect.height // 2)
        pygame.draw.rect(screen, (255, 255, 255), inner_rect, border_radius=1)
