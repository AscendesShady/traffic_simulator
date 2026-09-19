"""Re-derive the steady-state primary DV for already-exported workbooks.

    python -m src.experiments.rederive_steady_dv "<folder of *.xlsx>" [--warmup-sec 120]

Reads each workbook's Telemetry sheet (cumulative passengers served, sampled
about once a second) and reports pax_per_min_steady over the post-warm-up
window plus the convergence flag, so completed runs need not be repeated.
Writes ``steady_dv_rederived.csv`` next to the workbooks.
"""

import argparse
import csv
from pathlib import Path

from openpyxl import load_workbook


def telemetry_rows(path):
    sheet = load_workbook(path, read_only=True)["Telemetry"]
    rows = sheet.iter_rows(values_only=True)
    header = [str(cell) for cell in next(rows)]
    for row in rows:
        record = dict(zip(header, row))
        if isinstance(record.get("sim_time_s"), (int, float)):
            yield record


def steady_dv(rows, warmup_sec):
    rows = sorted(rows, key=lambda row: row["sim_time_s"])
    if not rows:
        return None
    end = rows[-1]
    warm = next((row for row in rows if row["sim_time_s"] >= warmup_sec), None)
    duration = float(end["sim_time_s"])
    result = {
        "duration_sec": round(duration, 1),
        "pax_per_min_cumulative": round(
            float(end["passengers_served_total"] or 0) / (duration / 60.0), 2
        ) if duration else None,
        "warmup_discard_sec": warmup_sec,
        "steady_window_sec": None,
        "pax_per_min_steady": None,
        "converged": None,
    }
    if warm is not None and end["sim_time_s"] > warm["sim_time_s"]:
        window = float(end["sim_time_s"] - warm["sim_time_s"])
        served = float(end["passengers_served_total"] or 0) - float(
            warm["passengers_served_total"] or 0
        )
        result["steady_window_sec"] = round(window, 1)
        result["pax_per_min_steady"] = round(served / (window / 60.0), 2)
    earlier = [row for row in rows if row["sim_time_s"] <= 0.8 * duration]
    if earlier and result["pax_per_min_cumulative"]:
        then = float(earlier[-1].get("pax_per_min_cumulative") or 0)
        now = result["pax_per_min_cumulative"]
        result["converged"] = abs(now - then) / now < 0.05
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder")
    parser.add_argument("--warmup-sec", type=float, default=120.0)
    args = parser.parse_args()
    folder = Path(args.folder)
    out_rows = []
    for path in sorted(folder.glob("*.xlsx")):
        result = steady_dv(list(telemetry_rows(path)), args.warmup_sec)
        if result is None:
            continue
        out_rows.append({"workbook": path.name, **result})
        print(
            f"{path.name:55s} cumulative={result['pax_per_min_cumulative']:>7} "
            f"steady={result['pax_per_min_steady']:>7} converged={result['converged']}"
        )
    if out_rows:
        target = folder / "steady_dv_rederived.csv"
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(out_rows[0]))
            writer.writeheader()
            writer.writerows(out_rows)
        print(f"wrote {target}")


if __name__ == "__main__":
    main()
