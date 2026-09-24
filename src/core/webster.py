"""Webster green-time splits for the two-phase nodes in this network.

Pure computation: the saturation flow S is always supplied by the caller and
never measured here, so the module stays deterministic and testable. S itself
is calibrated per run by main.calibrate_saturation_flow, because this
simulator's capacity depends on the configured speed scale and vehicle mix.

Each node runs two phases -- EW (EB + WB) and NS (that node's NB + SB) -- laid
out over the controller's six-state cycle:

    0 = EW green, 1 = EW yellow, 2 = EW all-red,
    3 = NS green, 4 = NS yellow, 5 = NS all-red

Splits use the standard critical-movement method: each phase is represented by
its heaviest *lane*, y = q_lane / S, and the available effective green is
shared in proportion to y. S is measured per lane (main.calibrate_saturation_flow
discharges a single-lane queue), so the flow ratio must use a per-lane flow
too. The per-lane flows come from a node-by-node movement matrix
(movement_lane_flows): every (approach, turn option) pair of
control_panel.APPROACH_TURN_OPTIONS is walked node by node with the lane the
spawner and vehicle.py give it -- straight from the source in lanes 0/1
(evenly), a left from lane 2 at the node it turns at (a far-node left rides
lanes 0/1 through the near node first), and after any left the car lands on
the exit road's lane 2 and keeps it to the next node. A side-street left
therefore loads the corridor's lane 2 at the *other* node, and the two nodes
see different EB/WB lane flows.

Webster's optimum cycle collapses toward the lost time when Y is small, so the
used cycle is floored at MIN_CYCLE_SEC, the usual practical minimum.
"""

from src.ui.canvas_gemini import INT_X

FPS = 60
# Longest cycle the controller runs. Webster's optimum (1.5L + 5)/(1 - Y)
# diverges as Y -> 1 -- 371 s at Y = 0.94 and 4,111 s at Y = 0.99 on the
# 500 m network -- and is undefined at Y >= 1, so practice bounds the cycle
# (Signal Timing Manual, NCHRP Report 812: typically 60-120 s, up to
# 150-180 s at large intersections). It used to bind only at Y >= 1, which
# ran a 68-minute cycle at Y = 0.99 and dropped back to 120 s at Y = 1.05.
# 150 s, the manual's large-intersection range: on the 500 m network (seeds
# 234/764, 15 min, baseline) it cut total person-hours against 120 s by 10 %
# at 0.7x campaign demand (which then runs its own 132 s optimum), 7 % at
# 0.8x and 3 % at 1.0x; 180 s added under 1 % while buses waited longer reds
# (2026-09-24). It keeps a node below capacity up to Y = (C - L)/C = 0.916.
# main passes global_config["max_cycle_sec"] when set.
MAX_CYCLE_SEC = 150.0
OVERSATURATED_CYCLE_CAP_SEC = MAX_CYCLE_SEC   # the earlier name, kept for callers
MIN_CYCLE_SEC = 40.0
STRAIGHT_LANES = (0, 1)   # share the straight movement evenly
LEFT_LANE = 2
# Direction after a left, and the next node a direction reaches from a node
# (None = leaves the network).
_LEFT_OF = {"EB": "NB", "WB": "SB", "NB": "WB", "SB": "EB"}
PHASE_OF = {"EB": "EW", "WB": "EW", "NB": "NS", "SB": "NS"}


def _next_node(direction, node_x):
    if direction == "EB" and node_x == INT_X[0]:
        return INT_X[1]
    if direction == "WB" and node_x == INT_X[1]:
        return INT_X[0]
    return None


def _first_node(approach_key, direction):
    if direction == "EB":
        return INT_X[0]
    if direction == "WB":
        return INT_X[1]
    return INT_X[0] if approach_key.startswith("A_") else INT_X[1]


def _turn_options(turn_options):
    if turn_options is None:
        from src.ui.control_panel import APPROACH_TURN_OPTIONS  # lazy: avoids a Tk import here
        turn_options = APPROACH_TURN_OPTIONS
    return turn_options


def movement_lane_flows(approach_configs, turn_options=None):
    """Node-by-node movement matrix: veh/hr entering each node, by entry
    direction and lane, ``{node_x: {direction: {lane: veh_hr}}}``.

    Each (approach, path) of ``turn_options`` (straight, first left option,
    second left option with ``turn_split`` / ``left_far_share``) is walked
    node to node with the lane vehicle.py gives it, so route transfers --
    a B_NB left that then enters Node A westbound, a far-node left that
    passes the near node in a general lane -- land on the node and lane
    they really use. An inactive approach offers nothing.
    """
    turn_options = _turn_options(turn_options)
    flows = {node_x: {} for node_x in INT_X}

    def add(node_x, direction, lane, veh_hr):
        lanes = flows[node_x].setdefault(direction, {0: 0.0, 1: 0.0, 2: 0.0})
        lanes[lane] += veh_hr

    def walk(approach_key, direction, left_nodes, veh_hr):
        node_x = _first_node(approach_key, direction)
        lane = None  # None = a source straight lane, spread over lanes 0/1
        while node_x is not None:
            if node_x in left_nodes:
                add(node_x, direction, LEFT_LANE, veh_hr)
                direction = _LEFT_OF[direction]
                lane = LEFT_LANE
            elif lane is None:
                for straight_lane in STRAIGHT_LANES:
                    add(node_x, direction, straight_lane, veh_hr / len(STRAIGHT_LANES))
            else:
                add(node_x, direction, lane, veh_hr)
            node_x = _next_node(direction, node_x)

    for approach_key, cfg in approach_configs.items():
        if not cfg.get("active", True):
            continue
        veh_hr = float(cfg.get("rate", 0) or 0) * 60.0
        if veh_hr <= 0:
            continue
        direction = approach_key.rsplit("_", 1)[-1]
        options = turn_options.get(approach_key, ())
        straight = min(1.0, max(0.0, float(cfg.get("turn_split", 0.8))))
        far = min(1.0 - straight, max(0.0, float(cfg.get("left_far_share", 0.0)))) if len(options) > 1 else 0.0
        near = 1.0 - straight - far
        walk(approach_key, direction, (), straight * veh_hr)
        if options and near > 0:
            walk(approach_key, direction, options[0][1], near * veh_hr)
        if far > 0:
            walk(approach_key, direction, options[1][1], far * veh_hr)
    return flows


def critical_lane_flows(lane_flows, node_x):
    """One node's critical (heaviest) lane per phase from the movement matrix."""
    critical = {"EW": 0.0, "NS": 0.0}
    for direction, lanes in lane_flows.get(node_x, {}).items():
        phase = PHASE_OF[direction]
        critical[phase] = max(critical[phase], *lanes.values())
    return critical


def approach_critical_lane_flow_veh_hr(approach_configs, approach_key, turn_options=None):
    """Busiest lane (veh/hr) an approach's own entry direction loads at any
    node it enters -- the v/c movement for that approach."""
    lane_flows = movement_lane_flows(approach_configs, turn_options)
    direction = approach_key.rsplit("_", 1)[-1]
    nodes = INT_X if direction in ("EB", "WB") else (_first_node(approach_key, direction),)
    return max(
        max(lane_flows[node_x].get(direction, {0: 0.0}).values()) for node_x in nodes
    )


def compute_node_green_splits(
    critical_flows,
    s,
    cycle_sec=None,
    lost_time_sec=4.0,
    fps=FPS,
    oversaturated_cycle_cap_sec=OVERSATURATED_CYCLE_CAP_SEC,
    min_cycle_sec=MIN_CYCLE_SEC,
    displayed_green_offset_sec=0.0,
):
    """Derive one node's Webster cycle and split it between EW and NS.

    ``displayed_green_offset_sec`` is HCM's l1 - e: the displayed green that
    yields effective green g is G = g + l1 - e (HCM 7th ed., Ch. 19), so the
    controller's real cycle -- displayed greens plus two change intervals --
    comes out at exactly the cycle Webster chose. Zero when l1 = e, which is
    HCM's default pair (2.0 s each).

    `critical_flows` maps "EW" and "NS" to the critical (heaviest) lane
    flow for that phase, in veh/hr/lane. `s` is the measured saturation flow
    in veh/hr/lane. By default the actually-used cycle is Webster's optimum,
    floored at `min_cycle_sec` (the floor is reported as its own
    `cycle_source`). The optional `cycle_sec` override keeps the function
    useful for controlled tests and future experiments without making cycle
    length an operator input. At or above capacity Webster's optimum is
    undefined, so the run uses a bounded fallback cycle and reports the
    oversaturation explicitly.
    """
    y_ew = max(0.0, float(critical_flows["EW"])) / s if s > 0 else 0
    y_ns = max(0.0, float(critical_flows["NS"])) / s if s > 0 else 0
    total_y = y_ew + y_ns
    oversaturated = total_y >= 1.0
    optimal_cycle = (
        (1.5 * lost_time_sec + 5.0) / (1.0 - total_y)
        if not oversaturated
        else None
    )
    if cycle_sec is not None:
        used_cycle = max(float(cycle_sec), lost_time_sec + 1.0)
        cycle_source = "explicit_override"
    elif oversaturated:
        used_cycle = max(
            float(oversaturated_cycle_cap_sec), lost_time_sec + 1.0
        )
        cycle_source = "oversaturation_cap"
    elif float(optimal_cycle) < float(min_cycle_sec):
        used_cycle = max(float(min_cycle_sec), lost_time_sec + 1.0)
        cycle_source = "min_cycle_floor"
    elif float(optimal_cycle) > float(oversaturated_cycle_cap_sec):
        used_cycle = max(float(oversaturated_cycle_cap_sec), lost_time_sec + 1.0)
        cycle_source = "max_cycle_cap"
    else:
        used_cycle = max(float(optimal_cycle), lost_time_sec + 1.0)
        cycle_source = "webster_optimal"

    effective_green = max(used_cycle - lost_time_sec, 1.0)
    if total_y <= 0:
        # No demand configured: fall back to an even split rather than
        # starving either phase.
        green_ew = green_ns = effective_green / 2
    else:
        green_ew = (y_ew / total_y) * effective_green
        green_ns = (y_ns / total_y) * effective_green
    green_ew = max(green_ew + float(displayed_green_offset_sec), 1.0 / fps)
    green_ns = max(green_ns + float(displayed_green_offset_sec), 1.0 / fps)
    return {
        "EW_green_frames": max(1, int(round(green_ew * fps))),
        "NS_green_frames": max(1, int(round(green_ns * fps))),
        "EW_green_sec": round(green_ew, 2),
        "NS_green_sec": round(green_ns, 2),
        "cycle_time_sec": round(used_cycle, 2),
        "cycle_time_frames": max(1, int(round(used_cycle * fps))),
        "lost_time_sec": round(float(lost_time_sec), 2),
        "EW_critical_lane_flow_veh_hr": round(float(critical_flows["EW"]), 1),
        "NS_critical_lane_flow_veh_hr": round(float(critical_flows["NS"]), 1),
        "y_ew": round(y_ew, 3),
        "y_ns": round(y_ns, 3),
        "Y": round(total_y, 3),
        "oversaturated": oversaturated,
        "cycle_source": cycle_source,
        "min_cycle_sec": round(float(min_cycle_sec), 2),
        "webster_optimal_cycle_sec": (
            round(optimal_cycle, 2) if optimal_cycle is not None else None
        ),
    }


def compute_all_nodes(
    approach_configs,
    s,
    cycle_sec=None,
    lost_time_sec=4.0,
    fps=FPS,
    oversaturated_cycle_cap_sec=OVERSATURATED_CYCLE_CAP_SEC,
    min_cycle_sec=MIN_CYCLE_SEC,
    turn_options=None,
    displayed_green_offset_sec=0.0,
    common_cycle=False,
):
    """Derive Webster cycles and splits for both nodes.

    ``common_cycle`` runs both nodes on one cycle -- the longest either
    node needs -- re-splitting each node's green by its own flow ratios.
    Coordinated signals share a cycle length (NCHRP Report 812, Signal
    Timing Manual, 2nd ed., coordination chapter); two nodes on different
    cycles have a relative offset that drifts every cycle, so progression on
    the link between them sweeps from good to bad over a run. Each node's
    own optimum is kept as ``node_webster_cycle_sec``.

    Approach rates are configured in veh/min; each node's phase is
    represented by its heaviest lane from the movement matrix
    (movement_lane_flows), so the ratio against the per-lane S is
    dimensionally consistent and each node is evaluated on the movements
    that actually enter it. Asymmetric demand, or a side-street left that
    feeds the corridor at the other node, therefore gives the nodes
    different cycle lengths.
    """
    lane_flows = movement_lane_flows(approach_configs, turn_options)

    def split(node_x, cycle):
        return compute_node_green_splits(
            critical_lane_flows(lane_flows, node_x),
            s,
            cycle,
            lost_time_sec,
            fps,
            oversaturated_cycle_cap_sec,
            min_cycle_sec,
            displayed_green_offset_sec,
        )

    independent = {node_x: split(node_x, cycle_sec) for node_x in INT_X}
    if not common_cycle or cycle_sec is not None:
        return independent
    common = max(result["cycle_time_sec"] for result in independent.values())
    coordinated = {}
    for node_x, own in independent.items():
        result = split(node_x, common)
        result["cycle_source"] = f"common_cycle ({own['cycle_source']})"
        result["node_webster_cycle_sec"] = own["cycle_time_sec"]
        # A node whose own optimum is undefined stays reported as such.
        result["oversaturated"] = own["oversaturated"]
        result["webster_optimal_cycle_sec"] = own["webster_optimal_cycle_sec"]
        coordinated[node_x] = result
    return coordinated


# HCM 7th ed. (TRB, 2022), Ch. 19 default values: start-up lost time l1 and
# extension of effective green e. main measures l1 per regime and overrides.
HCM_STARTUP_LOST_TIME_SEC = 2.0
HCM_EFFECTIVE_GREEN_EXTENSION_SEC = 2.0


def lost_time_seconds(
    yellow_frames, red_clearance_frames, fps=FPS,
    startup_lost_sec=HCM_STARTUP_LOST_TIME_SEC,
    extension_sec=HCM_EFFECTIVE_GREEN_EXTENSION_SEC,
):
    """Total lost time per cycle, two phases of HCM's t_L = l1 + l2.

    l2 = Y + AR - e is the clearance lost time; l1 the start-up lost time
    (HCM 7th ed., Ch. 19). At the former 1 s yellow / 1 s all-red with HCM's
    defaults this is the same 4 s the old formula gave, so the change adds
    the start-up term rather than moving any existing result by itself.
    """
    change = (yellow_frames + red_clearance_frames) / fps
    clearance_lost = max(0.0, change - float(extension_sec))
    return 2.0 * (max(0.0, float(startup_lost_sec)) + clearance_lost)


# ITE, "Guidelines for Determining Traffic Signal Change and Clearance
# Intervals" (Recommended Practice, 2020): Y = t + v / (2a + 2Gg) and
# R = (W + L) / v, with t = 1.0 s perception-reaction, a = 3.05 m/s^2
# (10 ft/s^2), L = 6.1 m (20 ft) and v the 85th-percentile approach speed.
# MUTCD (2009 ed., Sec. 4D.26) guidance bounds yellow to 3-6 s and red
# clearance to at most 6 s.
ITE_REACTION_SEC = 1.0
ITE_DECEL_MPS2 = 3.05
ITE_VEHICLE_LENGTH_M = 6.1
GRAVITY_MPS2 = 9.81
MUTCD_YELLOW_MIN_SEC = 3.0
MUTCD_YELLOW_MAX_SEC = 6.0
MUTCD_RED_CLEARANCE_MAX_SEC = 6.0


def _round_up(value, step=0.1):
    import math
    return math.ceil(round(value / step, 6)) * step


def ite_change_intervals(approach_speed_mps, intersection_width_m, grade=0.0):
    """(yellow_sec, all_red_sec) per the ITE kinematic formulas, bounded by
    MUTCD guidance and rounded up to 0.1 s."""
    v = max(0.1, float(approach_speed_mps))
    yellow = ITE_REACTION_SEC + v / (2.0 * ITE_DECEL_MPS2 + 2.0 * GRAVITY_MPS2 * float(grade))
    all_red = (float(intersection_width_m) + ITE_VEHICLE_LENGTH_M) / v
    yellow = min(MUTCD_YELLOW_MAX_SEC, max(MUTCD_YELLOW_MIN_SEC, _round_up(yellow)))
    all_red = min(MUTCD_RED_CLEARANCE_MAX_SEC, max(0.0, _round_up(all_red)))
    return round(yellow, 1), round(all_red, 1)
