"""Constant network-condition monitor and gridlock diagnosis.

Fed the same telemetry payload ``telemetry_exporter`` writes (every export
sample in-process, or a file read by the ``__main__`` script), it keeps a
per-sample timeline of network conditions, flags gridlock onset/clearance,
and diagnoses the queue heads that are stopped with nothing in front of them
-- the vehicle-level state a reviewer otherwise has to reconstruct by hand
from ``vehicle_positions``. ``write_workbook`` emits the report as its own
Excel file beside the run workbook.

Run standalone against the live snapshot::

    python -m src.telemetry.gridlock_monitor            # one diagnosis
    python -m src.telemetry.gridlock_monitor --watch    # every 2 s
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from src.ui.canvas_gemini import H_Y, LANE, ROAD_W, STOP

VEHICLE_LENGTH = {"car": 18, "truck": 28, "bus": 42}
# Gridlock: at least this share of a populated network stopped, sustained
# past GRIDLOCK_SUSTAIN_S (longer than one 12 s cycle, so a red-phase platoon
# does not trip it).
GRIDLOCK_STOPPED_SHARE = 0.75
GRIDLOCK_MIN_VEHICLES = 40
GRIDLOCK_SUSTAIN_S = 20.0
GRIDLOCK_CRAWL_SPEED_PX_PER_FRAME = 0.1
# A stopped queue head this far short of its stop bar, with an empty lane
# ahead, is holding for something other than the signal. Vehicles request
# intersection entry inside 25 px, so a head parked there is at the bar.
SHORT_OF_BAR_PX = 26.0

_HALF_W = ROAD_W // 2


def _dist_to_bar(v, node_x):
    half_len = VEHICLE_LENGTH.get(v.get("vehicle_type"), 18) / 2.0
    d = v["direction"]
    if d == "EB":
        return (node_x - _HALF_W - STOP) - (v["x_px"] + half_len)
    if d == "WB":
        return (v["x_px"] - half_len) - (node_x + _HALF_W + STOP)
    if d == "NB":
        return (v["y_px"] - half_len) - (H_Y + _HALF_W + STOP)
    if d == "SB":
        return (H_Y - _HALF_W - STOP) - (v["y_px"] + half_len)
    return 0.0


def _target_node(v, nodes):
    d = v["direction"]
    if d in ("EB", "WB"):
        upcoming = [n for n in sorted(nodes) if n not in (v.get("passed_nodes") or [])]
        if not upcoming:
            return None
        return upcoming[0] if d == "EB" else upcoming[-1]
    return v.get("assigned_node_x")


def _ahead_by(v, other):
    """How far ``other`` is ahead of ``v`` along ``v``'s travel direction."""
    d = v["direction"]
    if d == "EB":
        return other["x_px"] - v["x_px"]
    if d == "WB":
        return v["x_px"] - other["x_px"]
    if d == "NB":
        return v["y_px"] - other["y_px"]
    return other["y_px"] - v["y_px"]


def _same_band(v, other):
    if v["direction"] in ("EB", "WB"):
        return abs(other["y_px"] - v["y_px"]) < LANE / 2.0
    return abs(other["x_px"] - v["x_px"]) < LANE / 2.0


def _straddling(v):
    if v["direction"] not in ("EB", "WB"):
        return False
    frac = (abs(v["y_px"] - H_Y) / LANE) % 1.0
    return abs(frac - 0.5) > 0.1


def diagnose_stalled_heads(payload):
    """Stopped vehicles at the head of their lane on an approach, with nothing
    in front of them, that are either short of the bar or sitting at it on
    green (or on a permissive left). One dict per vehicle."""
    nodes_state = (payload.get("signal_state") or {}).get("nodes") or {}
    nodes = [int(k) for k in nodes_state]
    vehicles = [
        v for v in payload.get("vehicle_positions") or []
        if isinstance(v, dict) and v.get("direction") in ("EB", "WB", "NB", "SB")
    ]
    rows = []
    for v in vehicles:
        if (v.get("speed_px_per_frame") or 0.0) > GRIDLOCK_CRAWL_SPEED_PX_PER_FRAME:
            continue
        node_x = _target_node(v, nodes)
        if node_x is None:
            continue
        dist = _dist_to_bar(v, node_x)
        if dist < 0.0:
            continue
        # Nothing in this lane band between the vehicle and the far side of
        # the intersection box (a car inside the box still counts as ahead).
        blocked = any(
            o is not v and o["direction"] == v["direction"] and _same_band(v, o)
            and 0.0 < _ahead_by(v, o) < dist + ROAD_W + 2 * STOP
            for o in vehicles
        )
        if blocked:
            continue
        node = nodes_state.get(str(node_x)) or {}
        signal = (node.get("signals") or {}).get(v["direction"], "?")
        spillback = bool((node.get("downstream_blocked") or {}).get(v["direction"]))
        if dist > SHORT_OF_BAR_PX:
            reason = "stopped short of bar, lane ahead empty"
        elif spillback:
            reason = "at bar, downstream blocked (spillback hold)"
        elif signal == "GREEN":
            reason = "stopped at bar on green"
        else:
            continue  # waiting at red, or a left turner waiting for a gap
        rows.append({
            "vehicle": v.get("snapshot_id"),
            "type": v.get("vehicle_type"),
            "route": v.get("route_id"),
            "node": node_x,
            "direction": v["direction"],
            "lane": v.get("lane_index"),
            "x_px": round(v["x_px"], 1),
            "y_px": round(v["y_px"], 1),
            "dist_to_bar_px": round(dist, 1),
            "signal": signal,
            "turn": v.get("target_turn"),
            "straddling_lanes": _straddling(v),
            "lane_vacate_target": v.get("lane_vacate_target"),
            "must_hold_for_lane": bool(v.get("must_hold_for_lane")),
            "route_merge_hold": bool(v.get("route_merge_hold_active")),
            "reason": reason,
        })
    return rows


class GridlockMonitor:
    """Samples every telemetry payload; keeps the timeline, events and the
    stalled-head diagnosis at onset and at the last sample."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.timeline = []
        self.events = []
        self.state = "FREE"
        self._slow_since = None
        self.onset_time_s = None
        self.onset_heads = []
        self.final_heads = []
        self.gridlock_seconds = 0.0
        self._last_time_s = None

    def sample(self, payload):
        if not isinstance(payload, dict) or "signal_state" not in payload:
            return
        t = float(payload.get("simulation_time_seconds") or 0.0)
        summary = payload.get("network_summary") or {}
        vehicles = payload.get("vehicle_positions") or []
        total = int(summary.get("total_vehicles") or len(vehicles))
        mean_speed = float(summary.get("mean_speed_px_per_frame") or 0.0)
        stopped = sum(1 for v in vehicles if not v.get("speed_px_per_frame"))
        crawling = sum(
            1 for v in vehicles
            if (v.get("speed_px_per_frame") or 0.0) <= GRIDLOCK_CRAWL_SPEED_PX_PER_FRAME
        )
        heads = diagnose_stalled_heads(payload)

        if self.state == "GRIDLOCK" and self._last_time_s is not None:
            self.gridlock_seconds += max(0.0, t - self._last_time_s)
        slow = total >= GRIDLOCK_MIN_VEHICLES and crawling >= GRIDLOCK_STOPPED_SHARE * total
        if slow:
            self._slow_since = t if self._slow_since is None else self._slow_since
            if self.state == "FREE" and t - self._slow_since >= GRIDLOCK_SUSTAIN_S:
                self.state = "GRIDLOCK"
                self.onset_time_s = t
                self.onset_heads = heads
                self.events.append((t, "GRIDLOCK_ONSET", total, mean_speed, len(heads)))
        else:
            self._slow_since = None
            if self.state == "GRIDLOCK":
                self.state = "FREE"
                self.events.append((t, "GRIDLOCK_CLEARED", total, mean_speed, len(heads)))
        self._last_time_s = t
        self.final_heads = heads

        row = {
            "sim_time_s": round(t, 2),
            "frame": payload.get("frame_number"),
            "state": self.state,
            "vehicles": total,
            "stopped_vehicles": stopped,
            "crawling_vehicles": crawling,
            "mean_speed_px_per_frame": round(mean_speed, 4),
            "stalled_heads": len(heads),
        }
        for key, node in (payload["signal_state"].get("nodes") or {}).items():
            p = f"node_{key}_"
            row[p + "phase"] = node.get("phase")
            row[p + "phase_timer_s"] = round((node.get("phase_timer_frames") or 0) / 60.0, 1)
            row[p + "priority"] = node.get("priority_state")
            for approach in ("EB", "WB", "NB", "SB"):
                row[p + approach + "_queue"] = (node.get("queues") or {}).get(approach)
                row[p + approach + "_blocked"] = (node.get("downstream_blocked") or {}).get(approach)
        self.timeline.append(row)

    # -- report ------------------------------------------------------------

    def summary_rows(self):
        last = self.timeline[-1] if self.timeline else {}
        return [
            ("final_state", self.state),
            ("gridlock_onset_sim_s", self.onset_time_s),
            ("seconds_in_gridlock", round(self.gridlock_seconds, 1)),
            ("samples", len(self.timeline)),
            ("final_sim_time_s", last.get("sim_time_s")),
            ("final_vehicles", last.get("vehicles")),
            ("final_stopped_vehicles", last.get("stopped_vehicles")),
            ("final_crawling_vehicles", last.get("crawling_vehicles")),
            ("final_mean_speed_px_per_frame", last.get("mean_speed_px_per_frame")),
            ("final_stalled_heads", len(self.final_heads)),
            ("gridlock_rule", f">= {GRIDLOCK_STOPPED_SHARE:.0%} of >= {GRIDLOCK_MIN_VEHICLES} "
                              f"vehicles at <= {GRIDLOCK_CRAWL_SPEED_PX_PER_FRAME} px/frame "
                              f"for {GRIDLOCK_SUSTAIN_S:.0f} s"),
        ]

    def write_workbook(self, destination):
        try:
            from openpyxl import Workbook
        except ImportError:
            print("openpyxl not installed; skipping gridlock report")
            return None
        destination = Path(destination)
        wb = Workbook()
        ws = wb.active
        ws.title = "Summary"
        ws.append(["metric", "value"])
        for row in self.summary_rows():
            ws.append(list(row))

        ws = wb.create_sheet("Events")
        ws.append(["sim_time_s", "event", "vehicles", "mean_speed_px_per_frame", "stalled_heads"])
        for ev in self.events:
            ws.append(list(ev))

        ws = wb.create_sheet("Stalled Heads")
        cols = ["at"] + (list((self.final_heads or self.onset_heads or [{}])[0].keys()) or ["vehicle"])
        ws.append(cols)
        for label, heads in (("onset", self.onset_heads), ("final", self.final_heads)):
            for h in heads:
                ws.append([label] + [h.get(c) for c in cols[1:]])

        ws = wb.create_sheet("Timeline")
        if self.timeline:
            cols = list(self.timeline[0].keys())
            ws.append(cols)
            for row in self.timeline:
                ws.append([row.get(c) for c in cols])
        destination.parent.mkdir(parents=True, exist_ok=True)
        wb.save(destination)
        wb.close()
        print(f"Gridlock report exported: {destination}")
        return destination


def report_path_for(workbook_path):
    p = Path(workbook_path)
    return p.with_name(p.stem + "_gridlock.xlsx")


def _print_diagnosis(payload, monitor=None):
    m = monitor if monitor is not None else GridlockMonitor()
    m.sample(payload)
    row = m.timeline[-1]
    print(f"t={row['sim_time_s']}s vehicles={row['vehicles']} stopped={row['stopped_vehicles']} "
          f"mean_speed={row['mean_speed_px_per_frame']} "
          f"{'CRAWLING-SHARE OVER GRIDLOCK GATE' if row['crawling_vehicles'] >= GRIDLOCK_STOPPED_SHARE * max(row['vehicles'], 1) else 'moving'} "
          f"state={m.state}")
    for key, node in payload["signal_state"]["nodes"].items():
        print(f"  node {key}: {node.get('phase')} timer={node.get('phase_timer_frames')}f "
              f"queues={node.get('queues')} blocked={[a for a, b in (node.get('downstream_blocked') or {}).items() if b]}")
    heads = m.final_heads
    print(f"  stalled heads: {len(heads)}")
    for h in heads:
        print(f"    {h['vehicle']:>18} {h['direction']} lane{h['lane']} node{h['node']} "
              f"{h['dist_to_bar_px']:6.1f}px from bar sig={h['signal']} turn={h['turn']} "
              f"straddle={h['straddling_lanes']} hold={h['must_hold_for_lane']} vac={h['lane_vacate_target']} -- {h['reason']}")


if __name__ == "__main__":
    path = Path(__file__).resolve().parents[2] / "data" / "traffic_state_telemetry.json"
    watch = "--watch" in sys.argv
    monitor = GridlockMonitor()
    while True:
        try:
            _print_diagnosis(json.loads(path.read_text(encoding="utf-8")), monitor)
        except (OSError, ValueError) as exc:
            print(f"no snapshot: {exc}")
        if not watch:
            break
        time.sleep(2)
