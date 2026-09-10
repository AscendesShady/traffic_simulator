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
its heaviest approach, y = q / S, and the available effective green is shared
in proportion to y.
"""

FPS = 60
OVERSATURATED_CYCLE_CAP_SEC = 120.0


def compute_node_green_splits(
    critical_flows,
    s,
    cycle_sec=None,
    lost_time_sec=4.0,
    fps=FPS,
    oversaturated_cycle_cap_sec=OVERSATURATED_CYCLE_CAP_SEC,
):
    """Derive one node's Webster cycle and split it between EW and NS.

    `critical_flows` maps "EW" and "NS" to the critical (heaviest) approach
    flow for that phase, in veh/hr. `s` is the measured saturation flow in
    veh/hr. By default the actually-used cycle is Webster's optimum. The
    optional `cycle_sec` override keeps the function useful for controlled
    tests and future experiments without making cycle length an operator
    input. At or above capacity Webster's optimum is undefined, so the run
    uses a bounded fallback cycle and reports the oversaturation explicitly.
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
        "y_ew": round(y_ew, 3),
        "y_ns": round(y_ns, 3),
        "Y": round(total_y, 3),
        "oversaturated": oversaturated,
        "cycle_source": cycle_source,
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
):
    """Derive independent Webster cycles and splits for both nodes.

    Approach rates are configured in veh/min and converted to veh/hr here.
    The EW corridor is shared by both nodes; each node contributes its own
    north-south pair. With no explicit override, asymmetric node demand can
    therefore produce different cycle lengths.
    """

    def veh_per_hour(key):
        return float(approach_configs[key].get("rate", 0)) * 60.0

    east_west = max(veh_per_hour("EB"), veh_per_hour("WB"))
    return {
        300: compute_node_green_splits(
            {
                "EW": east_west,
                "NS": max(veh_per_hour("A_NB"), veh_per_hour("A_SB")),
            },
            s,
            cycle_sec,
            lost_time_sec,
            fps,
            oversaturated_cycle_cap_sec,
        ),
        700: compute_node_green_splits(
            {
                "EW": east_west,
                "NS": max(veh_per_hour("B_NB"), veh_per_hour("B_SB")),
            },
            s,
            cycle_sec,
            lost_time_sec,
            fps,
            oversaturated_cycle_cap_sec,
        ),
    }


def lost_time_seconds(yellow_frames, red_clearance_frames, fps=FPS):
    """Total lost time per cycle: two phase changes of yellow plus all-red."""
    return 2.0 * (yellow_frames + red_clearance_frames) / fps
