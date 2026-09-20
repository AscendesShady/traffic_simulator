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
too: main.try_spawn_vehicle sends straight vehicles to lanes 0/1 at random and
left-turners to lane 2, so the busiest lane carries
max(turn_split / 2, 1 - turn_split) of the approach flow.

Webster's optimum cycle collapses toward the lost time when Y is small, so the
used cycle is floored at MIN_CYCLE_SEC, the usual practical minimum.
"""

from src.ui.canvas_gemini import INT_X

FPS = 60
OVERSATURATED_CYCLE_CAP_SEC = 120.0
MIN_CYCLE_SEC = 40.0
STRAIGHT_LANE_COUNT = 2   # lanes 0 and 1 share the straight movement


def critical_lane_fraction(turn_split):
    """Share of an approach's flow carried by its busiest lane.

    Straight traffic (``turn_split``) is split evenly across the two straight
    lanes; the remainder all uses the single left-turn lane.
    """
    straight = min(1.0, max(0.0, float(turn_split)))
    return max(straight / STRAIGHT_LANE_COUNT, 1.0 - straight)


def critical_lane_flow_veh_hr(approach_cfg):
    """Busiest-lane flow (veh/hr) for one approach configuration."""
    rate_veh_hr = float(approach_cfg.get("rate", 0)) * 60.0
    return rate_veh_hr * critical_lane_fraction(approach_cfg.get("turn_split", 0.8))


def compute_node_green_splits(
    critical_flows,
    s,
    cycle_sec=None,
    lost_time_sec=4.0,
    fps=FPS,
    oversaturated_cycle_cap_sec=OVERSATURATED_CYCLE_CAP_SEC,
    min_cycle_sec=MIN_CYCLE_SEC,
):
    """Derive one node's Webster cycle and split it between EW and NS.

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
):
    """Derive independent Webster cycles and splits for both nodes.

    Approach rates are configured in veh/min; each approach contributes the
    flow of its busiest lane (see critical_lane_flow_veh_hr) so the ratio
    against the per-lane S is dimensionally consistent. The EW corridor is
    shared by both nodes; each node contributes its own north-south pair.
    With no explicit override, asymmetric node demand can therefore produce
    different cycle lengths.
    """

    def veh_per_hour(key):
        return critical_lane_flow_veh_hr(approach_configs[key])

    east_west = max(veh_per_hour("EB"), veh_per_hour("WB"))
    return {
        INT_X[0]: compute_node_green_splits(
            {
                "EW": east_west,
                "NS": max(veh_per_hour("A_NB"), veh_per_hour("A_SB")),
            },
            s,
            cycle_sec,
            lost_time_sec,
            fps,
            oversaturated_cycle_cap_sec,
            min_cycle_sec,
        ),
        INT_X[1]: compute_node_green_splits(
            {
                "EW": east_west,
                "NS": max(veh_per_hour("B_NB"), veh_per_hour("B_SB")),
            },
            s,
            cycle_sec,
            lost_time_sec,
            fps,
            oversaturated_cycle_cap_sec,
            min_cycle_sec,
        ),
    }


def lost_time_seconds(yellow_frames, red_clearance_frames, fps=FPS):
    """Total lost time per cycle: two phase changes of yellow plus all-red."""
    return 2.0 * (yellow_frames + red_clearance_frames) / fps
