"""Real-world unit conversions for the simulator, anchored on saturation flow.

The simulator was never built to one consistent physical scale, so no single
factor makes distance AND speed both realistic at once. This module picks
the quantity a traffic-engineering reader cares about -- saturation flow --
declares that the sim's measured S corresponds to the HCM standard, and
derives every other scale from that one mapping. Each converted quantity
carries a tag saying how much to trust it:

    EXACT    frames -> seconds. Sim time is real time; no assumption.
    ANCHOR   the declared S_sim == S_real mapping everything hangs on.
    DERIVED  follows arithmetically from the anchor (distances, flows, v/c,
             delay-based level of service).
    APPROX   also derived, but the sim was never calibrated to it
             independently (speeds): treat as sim-equivalent, not measured.

Display and export only: nothing here feeds back into the simulation.
"""

import canvas_gemini as canvas
import webster

# --- Anchor constants (edit these to sensitivity-test the reporting basis) ---
REAL_SATURATION_FLOW_VEH_HR = 1900   # HCM standard per lane, the anchor
REAL_JAM_SPACING_M = 7.5             # HCM queued vehicle spacing (front to front)

FRAMES_PER_SECOND = 60
# A queued sim car occupies its own length plus the standing gap it keeps to
# the vehicle ahead (vehicle.py: car length 18 px, SAFE_GAP 12 px).
SIM_CAR_LENGTH_PX = 18
SIM_QUEUE_GAP_PX = 12
SIM_QUEUE_SPACING_PX = SIM_CAR_LENGTH_PX + SIM_QUEUE_GAP_PX
# Per-approach lane count and node-to-node road length, from the canvas.
LANES_PER_APPROACH = canvas.LANES
ROAD_LENGTH_BETWEEN_NODES_PX = canvas.INT_X[1] - canvas.INT_X[0]

TAG_EXACT = "EXACT"
TAG_ANCHOR = "ANCHOR"
TAG_DERIVED = "DERIVED"
TAG_APPROX = "APPROX"
ALL_TAGS = (TAG_EXACT, TAG_ANCHOR, TAG_DERIVED, TAG_APPROX)

# HCM 6th edition, signalized intersections: control delay (s/veh) -> LOS.
HCM_LOS_THRESHOLDS = ((10.0, "A"), (20.0, "B"), (35.0, "C"), (55.0, "D"), (80.0, "E"))

SPEED_CAVEAT = (
    "Speed is derived from the saturation-flow anchor; the sim was not "
    "calibrated to real speeds independently, so treat it as sim-equivalent."
)


# --- elementary conversions --------------------------------------------------

def frames_to_seconds(frames):
    """EXACT: the sim clock runs at 60 frames per real second."""
    return float(frames) / FRAMES_PER_SECOND


def saturation_scale(sim_saturation_flow, real_saturation_flow=REAL_SATURATION_FLOW_VEH_HR):
    """ANCHOR: k = S_real / S_sim, how much denser real traffic is than the
    sim at the same geometry. None until the sim has measured S."""
    if not sim_saturation_flow or float(sim_saturation_flow) <= 0:
        return None
    return float(real_saturation_flow) / float(sim_saturation_flow)


def saturation_headway_s(saturation_flow_veh_hr):
    """Seconds between successive discharging vehicles at saturation."""
    if not saturation_flow_veh_hr or float(saturation_flow_veh_hr) <= 0:
        return None
    return 3600.0 / float(saturation_flow_veh_hr)


def meters_per_pixel(queue_spacing_px=SIM_QUEUE_SPACING_PX, jam_spacing_m=REAL_JAM_SPACING_M):
    """DERIVED: one queued sim vehicle's spacing maps to one HCM jam spacing."""
    return float(jam_spacing_m) / float(queue_spacing_px)


def px_to_m(pixels, mpp=None):
    return float(pixels) * (meters_per_pixel() if mpp is None else mpp)


def px_per_frame_to_kmh(px_per_frame, mpp=None):
    """APPROX: px/frame -> m/s via the derived scale, then km/h."""
    mpp = meters_per_pixel() if mpp is None else mpp
    return float(px_per_frame) * FRAMES_PER_SECOND * mpp * 3.6


def demand_to_real_veh_hr(veh_per_min, k):
    """DERIVED: configured demand scaled by k, consistent with the anchored
    capacity. None until k exists."""
    if k is None:
        return None
    return float(veh_per_min) * 60.0 * k


def approach_capacity_veh_hr(green_ratio, lanes=1,
                             real_saturation_flow=REAL_SATURATION_FLOW_VEH_HR):
    """DERIVED: c = S_real x (g/C) x lanes. Default is one lane: v/c is
    evaluated on the approach's critical (busiest) lane, exactly as Webster
    evaluates y, so the two views agree by construction."""
    if green_ratio is None:
        return None
    return float(real_saturation_flow) * float(green_ratio) * int(lanes)


def critical_lane_flow_veh_hr(approach_cfg):
    """Busiest-lane flow of an approach (shared with Webster)."""
    return webster.critical_lane_flow_veh_hr(approach_cfg)


def volume_to_capacity(flow_veh_hr, capacity_veh_hr):
    if flow_veh_hr is None or not capacity_veh_hr:
        return None
    return float(flow_veh_hr) / float(capacity_veh_hr)


def hcm_level_of_service(delay_s_per_veh):
    """DERIVED: HCM signalized-intersection LOS from control delay."""
    if delay_s_per_veh is None:
        return None
    delay = float(delay_s_per_veh)
    for threshold, grade in HCM_LOS_THRESHOLDS:
        if delay <= threshold:
            return grade
    return "F"


# --- table builder -----------------------------------------------------------

def _green_ratios(webster_splits):
    """Per-approach g/C from the published Webster splits.

    EB/WB run through both nodes, so their capacity is set by the tighter of
    the two EW splits; each side street belongs to one node.
    """
    ratios = {}
    if not isinstance(webster_splits, dict):
        return ratios
    by_node = {}
    for node_x, split in webster_splits.items():
        if not isinstance(split, dict):
            continue
        cycle = float(split.get("cycle_time_sec") or 0.0)
        if cycle <= 0:
            continue
        by_node[str(node_x)] = (
            float(split.get("EW_green_sec") or 0.0) / cycle,
            float(split.get("NS_green_sec") or 0.0) / cycle,
        )
    if not by_node:
        return ratios
    ew = min(ratio[0] for ratio in by_node.values())
    ratios["EB"] = ratios["WB"] = ew
    node_a, node_b = (str(x) for x in canvas.INT_X)
    if node_a in by_node:
        ratios["A_NB"] = ratios["A_SB"] = by_node[node_a][1]
    if node_b in by_node:
        ratios["B_NB"] = ratios["B_SB"] = by_node[node_b][1]
    return ratios


def _fmt(value, unit="", decimals=1):
    if value is None:
        return "--"
    if isinstance(value, str):
        return value
    return f"{value:,.{decimals}f}{unit}"


def anchor_statement(sim_saturation_flow, real_saturation_flow=REAL_SATURATION_FLOW_VEH_HR):
    sim_text = _fmt(sim_saturation_flow, decimals=0) if sim_saturation_flow else "--"
    return (
        f"Real-world units anchored on saturation flow: sim S={sim_text} veh/hr "
        f"≡ real {real_saturation_flow:,.0f} veh/hr/lane (HCM). "
        "Distances and delay derived; speeds approximate."
    )


def build_conversion_table(global_config, approach_configs, approach_names, telemetry=None):
    """Sections of rows for the Units tab and the export.

    Returns a list of {"title", "rows"}; each row is a dict with
    ``quantity``, ``sim``, ``real``, ``tag`` and ``note``. Values are
    pre-formatted strings so the tab and the sheet show the same text.
    Missing inputs (before START) render as "--" rather than raising.
    """
    telemetry = telemetry if isinstance(telemetry, dict) else {}
    summary = telemetry.get("network_summary") or {}
    throughput = telemetry.get("network_throughput") or {}
    sim_s = global_config.get("measured_saturation_flow")
    k = saturation_scale(sim_s)
    mpp = meters_per_pixel()
    speed_scale = float(global_config.get("_active_vehicle_speed_scale")
                        or global_config.get("vehicle_speed_scale") or 0.0)
    sections = []

    sim_time = telemetry.get("simulation_time_seconds")
    frame = telemetry.get("frame_number")
    sections.append({
        "title": "Time",
        "rows": [
            {"quantity": "Simulation clock",
             "sim": _fmt(frame, " frames", 0) if frame is not None else "--",
             "real": _fmt(sim_time, " s") if sim_time is not None else "--",
             "tag": TAG_EXACT, "note": "60 frames = 1 s; sim time is real time"},
            {"quantity": "Frame duration", "sim": "1 frame",
             "real": _fmt(frames_to_seconds(1), " s", 4), "tag": TAG_EXACT, "note": ""},
        ],
    })

    sections.append({
        "title": "Capacity",
        "rows": [
            {"quantity": "Saturation flow (per lane)",
             "sim": _fmt(sim_s, " veh/hr", 0),
             "real": _fmt(REAL_SATURATION_FLOW_VEH_HR, " veh/hr", 0),
             "tag": TAG_ANCHOR, "note": "measured on START ≡ HCM standard"},
            {"quantity": "Saturation headway",
             "sim": _fmt(saturation_headway_s(sim_s), " s/veh", 2),
             "real": _fmt(saturation_headway_s(REAL_SATURATION_FLOW_VEH_HR), " s/veh", 2),
             "tag": TAG_ANCHOR, "note": "3600 / S"},
            {"quantity": "Scale factor k", "sim": "1.00",
             "real": _fmt(k, "×", 2), "tag": TAG_ANCHOR,
             "note": "k = S_real / S_sim"},
        ],
    })

    sections.append({
        "title": "Distance",
        "rows": [
            {"quantity": "Queued vehicle spacing",
             "sim": _fmt(SIM_QUEUE_SPACING_PX, " px", 0),
             "real": _fmt(REAL_JAM_SPACING_M, " m"),
             "tag": TAG_DERIVED, "note": f"1 px = {mpp:.3f} m (HCM jam spacing)"},
            {"quantity": "Lane width", "sim": _fmt(canvas.LANE, " px", 0),
             "real": _fmt(px_to_m(canvas.LANE, mpp), " m", 2), "tag": TAG_DERIVED, "note": ""},
            {"quantity": "Road length between nodes",
             "sim": _fmt(ROAD_LENGTH_BETWEEN_NODES_PX, " px", 0),
             "real": _fmt(px_to_m(ROAD_LENGTH_BETWEEN_NODES_PX, mpp), " m", 0),
             "tag": TAG_DERIVED, "note": ""},
            {"quantity": "TSP eligibility zone",
             "sim": _fmt(global_config.get("priority_eligibility_px"), " px", 0),
             "real": _fmt(px_to_m(global_config.get("priority_eligibility_px") or 0, mpp), " m", 0)
             if global_config.get("priority_eligibility_px") else "--",
             "tag": TAG_DERIVED, "note": ""},
        ],
    })

    mean_speed = summary.get("mean_speed_px_per_frame")
    sections.append({
        "title": "Speed",
        "rows": [
            {"quantity": "Free-flow speed (cars)",
             "sim": _fmt(speed_scale, " px/frame", 2) if speed_scale else "--",
             "real": _fmt(px_per_frame_to_kmh(speed_scale, mpp), " km/h") if speed_scale else "--",
             "tag": TAG_APPROX, "note": SPEED_CAVEAT},
            {"quantity": "Current mean vehicle speed",
             "sim": _fmt(mean_speed, " px/frame", 2) if mean_speed is not None else "--",
             "real": _fmt(px_per_frame_to_kmh(mean_speed, mpp), " km/h") if mean_speed is not None else "--",
             "tag": TAG_APPROX, "note": "live network average"},
        ],
    })

    ratios = _green_ratios(global_config.get("webster_splits"))
    flow_rows = []
    for key, name in approach_names.items():
        cfg = approach_configs.get(key, {}) or {}
        active = bool(cfg.get("active", False))
        rate = float(cfg.get("rate") or 0.0) if active else 0.0
        real_flow = demand_to_real_veh_hr(rate, k)
        # v/c on the busiest lane, the same critical movement Webster uses.
        lane_fraction = webster.critical_lane_fraction(cfg.get("turn_split", 0.8))
        critical_real_flow = demand_to_real_veh_hr(rate * lane_fraction, k)
        capacity = approach_capacity_veh_hr(ratios.get(key))
        vc = volume_to_capacity(critical_real_flow, capacity)
        flow_rows.append({
            "quantity": f"{name} demand",
            "sim": _fmt(rate, " veh/min", 0),
            "real": _fmt(real_flow, " veh/hr", 0),
            "tag": TAG_DERIVED,
            "note": (
                f"critical lane {lane_fraction:.0%} of flow = "
                f"{critical_real_flow:,.0f} veh/hr; v/c = {vc:.2f} "
                f"(c = {capacity:,.0f} veh/hr/lane)"
                if vc is not None else "v/c needs Webster splits (START)"
            ),
        })
    sections.append({"title": "Flow", "rows": flow_rows})

    delay = throughput.get("mean_stopped_delay_sec_per_vehicle")
    served = throughput.get("vehicles_served_total")
    los = hcm_level_of_service(delay)
    sections.append({
        "title": "Delay / LOS",
        "rows": [
            {"quantity": "Mean stopped delay per vehicle",
             "sim": _fmt(delay * FRAMES_PER_SECOND, " frames", 0) if delay is not None else "--",
             "real": _fmt(delay, " s/veh") if delay is not None else "--",
             "tag": TAG_DERIVED,
             "note": (f"over {served} served vehicles; stopped delay approximates "
                      "HCM control delay") if served else "no vehicles served yet"},
            {"quantity": "Level of service (HCM signalized)",
             "sim": "--", "real": f"LOS {los}" if los else "--",
             "tag": TAG_DERIVED,
             "note": "A ≤10 s, B ≤20, C ≤35, D ≤55, E ≤80, F >80"},
        ],
    })
    return sections


# --- export ------------------------------------------------------------------

UNIT_CONVERSION_HEADERS = ["section", "quantity", "sim_value", "real_value", "tag", "note"]


def conversion_rows(global_config, approach_configs, approach_names, telemetry=None):
    """Flat rows for a worksheet: the anchor constants first, then the table."""
    sim_s = global_config.get("measured_saturation_flow")
    k = saturation_scale(sim_s)
    rows = [
        ("Anchor", "REAL_SATURATION_FLOW_VEH_HR", "", REAL_SATURATION_FLOW_VEH_HR, TAG_ANCHOR,
         "HCM standard saturation flow per lane"),
        ("Anchor", "REAL_JAM_SPACING_M", "", REAL_JAM_SPACING_M, TAG_ANCHOR,
         "HCM queued vehicle spacing"),
        ("Anchor", "sim_measured_saturation_flow_veh_hr", sim_s, "", TAG_ANCHOR,
         "measured on START"),
        ("Anchor", "scale_factor_k", "", round(k, 4) if k else None, TAG_ANCHOR,
         "k = S_real / S_sim"),
        ("Anchor", "meters_per_pixel", SIM_QUEUE_SPACING_PX, round(meters_per_pixel(), 5),
         TAG_DERIVED, "REAL_JAM_SPACING_M / sim queue spacing px"),
        ("Anchor", "frames_per_second", FRAMES_PER_SECOND, 1.0, TAG_EXACT, "frames -> seconds"),
        ("Anchor", "statement", "", anchor_statement(sim_s), TAG_ANCHOR, ""),
    ]
    for section in build_conversion_table(global_config, approach_configs, approach_names, telemetry):
        for row in section["rows"]:
            rows.append((section["title"], row["quantity"], row["sim"], row["real"],
                         row["tag"], row["note"]))
    return rows


def write_unit_conversions_sheet(sheet, global_config, approach_configs, approach_names, telemetry=None):
    sheet.append(list(UNIT_CONVERSION_HEADERS))
    for row in conversion_rows(global_config, approach_configs, approach_names, telemetry):
        sheet.append(list(row))
    return sheet
