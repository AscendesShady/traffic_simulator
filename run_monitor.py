"""Watch headless runs: python run_monitor.py [--jobs JOBS_FILE ...] [--dir DIR]

Shows every run that publishes progress (src/experiments/progress.py) --
parallel_campaign workers and campaigns, headless_run.run(progress_label=...),
a pytest session while it runs (tests/conftest.py; its row removes itself
when the session ends) -- plus any batch given as a jobs file whose lines end
with each job's output path (done when that file exists). It only reads
progress files; it never touches a run.
"""
import argparse
import time
import tkinter as tk
from tkinter import ttk

from src.experiments import progress
from src.ui.control_panel import (
    COLOR_ACCENT, COLOR_BG, COLOR_CARD, COLOR_CARD_ALT, COLOR_CARD_BORDER, COLOR_DANGER,
    COLOR_SUCCESS, COLOR_TEXT_PRIMARY, COLOR_TEXT_SECONDARY, COLOR_WARNING,
    FONT_BODY, FONT_BODY_BOLD, FONT_SECTION, FONT_TITLE, SPACE_LG, SPACE_SM, SPACE_XS,
    make_button,
)

REFRESH_MS = 1000
BAR_CELLS = 12
COUNTED_KINDS = ("campaign", "batch")   # rows that count runs, not frames
STATE_COLORS = {"running": COLOR_ACCENT, "done": COLOR_SUCCESS,
                "failed": COLOR_DANGER, "stalled": COLOR_WARNING}
COLUMNS = (("status", "Status", 80), ("progress", "Progress", 150), ("amount", "Done", 120),
           ("speed", "Speed", 90), ("eta", "Remaining", 90), ("updated", "Updated", 90))


def _duration(seconds):
    if seconds is None:
        return "—"
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def row_values(record):
    """The table cells for one progress record (see progress.describe)."""
    filled = int(round(record["fraction"] * BAR_CELLS))
    bar = "█" * filled + "░" * (BAR_CELLS - filled) + f" {record['fraction'] * 100:3.0f}%"
    if record.get("kind") == "tests":
        failed = int((record.get("meta") or {}).get("failed") or 0)
        amount = f"{record['done']} / {record['total']} tests" + (f" · {failed} failed" if failed else "")
        speed = f"{record['rate'] * 60:.0f} tests/min" if record.get("rate") else "—"
    elif record.get("kind") in COUNTED_KINDS:
        amount = f"{record['done']} / {record['total']} runs"
        speed = f"{record['rate'] * 3600:.1f} runs/h" if record.get("rate") else "—"
    else:
        amount = f"{record['done'] / 3600:.1f} / {record['total'] / 3600:.1f} sim-min"
        speed = f"{record['rate'] / 60:.2f}× real time" if record.get("rate") else "—"
    return (record["state"].upper(), bar, amount, speed, _duration(record.get("eta_sec")),
            f"{_duration(record.get('age'))} ago")


def summary(records):
    """(headline, aggregate fraction). The bar follows the campaigns and
    batches if any run, else the single runs' frames, else a pytest session --
    never a sum across those, whose units differ."""
    counts = {}
    for record in records:
        counts[record["state"]] = counts.get(record["state"], 0) + 1
    running = [r for r in records if r["state"] == "running"]
    pool = (
        [r for r in running if r.get("kind") in COUNTED_KINDS]
        or [r for r in running if r.get("kind") not in COUNTED_KINDS + ("tests",)]
        or [r for r in running if r.get("kind") == "tests"]
    )
    fraction = (sum(r["done"] for r in pool) / sum(r["total"] for r in pool)) if pool else 0.0
    order = ("running", "stalled", "failed", "done")
    headline = " · ".join(f"{counts[s]} {s}" for s in order if counts.get(s)) or "no runs yet"
    return headline, fraction


class Monitor:
    def __init__(self, root, directory=None, jobs=()):
        self.root, self.directory, self.jobs = root, directory, list(jobs)
        root.title("Headless run monitor")
        root.configure(bg=COLOR_BG)
        root.minsize(760, 320)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("Monitor.Treeview", background=COLOR_CARD, fieldbackground=COLOR_CARD,
                        foreground=COLOR_TEXT_PRIMARY, font=FONT_BODY, rowheight=24,
                        bordercolor=COLOR_CARD_BORDER)
        style.configure("Monitor.Treeview.Heading", background=COLOR_CARD_ALT,
                        foreground=COLOR_TEXT_SECONDARY, font=FONT_BODY_BOLD, relief="flat")
        style.map("Monitor.Treeview", background=[("selected", COLOR_CARD_ALT)])
        style.configure("Monitor.Horizontal.TProgressbar", troughcolor=COLOR_CARD_ALT,
                        background=COLOR_ACCENT, bordercolor=COLOR_CARD_BORDER)

        header = tk.Frame(root, bg=COLOR_BG)
        header.pack(fill="x", padx=SPACE_LG, pady=(SPACE_LG, SPACE_SM))
        tk.Label(header, text="Headless runs", font=FONT_TITLE, bg=COLOR_BG,
                 fg=COLOR_TEXT_PRIMARY).pack(side="left")
        make_button(header, "Clear finished", command=self.clear).pack(side="right")
        self.headline = tk.Label(root, text="", font=FONT_SECTION, bg=COLOR_BG, fg=COLOR_TEXT_PRIMARY, anchor="w")
        self.headline.pack(fill="x", padx=SPACE_LG)
        self.bar = ttk.Progressbar(root, style="Monitor.Horizontal.TProgressbar", maximum=1.0)
        self.bar.pack(fill="x", padx=SPACE_LG, pady=(SPACE_XS, SPACE_SM))

        body = tk.Frame(root, bg=COLOR_BG)
        body.pack(fill="both", expand=True, padx=SPACE_LG, pady=(0, SPACE_SM))
        self.tree = ttk.Treeview(body, style="Monitor.Treeview", columns=[c for c, _, _ in COLUMNS])
        self.tree.heading("#0", text="Run", anchor="w")
        self.tree.column("#0", width=260, anchor="w")
        for key, title, width in COLUMNS:
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=width, anchor="w")
        for state, colour in STATE_COLORS.items():
            self.tree.tag_configure(state, foreground=colour)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        tk.Label(root, text=f"Watching {self.directory or progress.progress_dir()}",
                 font=FONT_BODY, bg=COLOR_BG, fg=COLOR_TEXT_SECONDARY, anchor="w").pack(
            fill="x", padx=SPACE_LG, pady=(0, SPACE_LG))
        self._job = None
        root.bind("<Destroy>", self._stop, add="+")
        self.refresh()

    def records(self):
        found = progress.read_all(self.directory)
        batches = [progress.jobs_progress(path) for path in self.jobs]
        return [r for r in batches if r] + found

    def refresh(self):
        records = self.records()
        seen = set()
        for index, record in enumerate(records):
            iid = record.get("path") or record["label"]
            seen.add(iid)
            values = row_values(record)
            if self.tree.exists(iid):
                self.tree.item(iid, text=record["label"], values=values, tags=(record["state"],))
                self.tree.move(iid, "", index)
            else:
                self.tree.insert("", index, iid=iid, text=record["label"], values=values,
                                 tags=(record["state"],))
        for iid in self.tree.get_children():
            if iid not in seen:
                self.tree.delete(iid)
        headline, fraction = summary(records)
        self.headline.config(text=headline)
        self.bar["value"] = fraction
        self._job = self.root.after(REFRESH_MS, self.refresh)

    def clear(self):
        progress.clear_finished(self.directory)
        # A --jobs batch has no progress file to delete: it is recounted from
        # its outputs every refresh, so a finished one is dropped from the watch.
        self.jobs = [path for path in self.jobs
                     if (progress.jobs_progress(path) or {}).get("state") == "running"]

    def _stop(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", default=None, help="progress directory (default runtime/progress)")
    parser.add_argument("--jobs", action="append", default=[], help="jobs file of a batch without progress files")
    args = parser.parse_args(argv)
    root = tk.Tk()
    Monitor(root, args.dir, args.jobs)
    root.mainloop()


if __name__ == "__main__":
    main()
