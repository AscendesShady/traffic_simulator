"""Progress files for headless runs, and reading them back for run_monitor.py.

A headless run given a label writes one small JSON record -- label, frame,
total, timings, status -- to progress_dir() every few simulated seconds,
atomically, so a monitor can show every run in flight without touching the
simulation. It is opt-in (no label, no file) so tests and one-off calls leave
nothing behind. A campaign coordinator writes the same record with
kind "campaign", counting finished runs instead of frames; a pytest session
(tests/conftest.py) writes kind "tests" and removes its record at the end.
"""
import json
import os
import pathlib
import re
import time

REPO = pathlib.Path(__file__).resolve().parents[2]
PROGRESS_DIR_ENV = "TRAFFIC_PROGRESS_DIR"
# A running record not refreshed for this long belongs to a process that died.
STALE_AFTER_SEC = 90.0


def progress_dir():
    return pathlib.Path(os.environ.get(PROGRESS_DIR_ENV) or REPO / "runtime" / "progress")


def _slug(label):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(label)).strip("_") or "run"


class ProgressReporter:
    """One run's progress file. ``update(frame)`` is cheap to call every
    frame: it writes only every ``every_frames`` and on the last frame."""

    def __init__(self, label, total, kind="run", meta=None, every_frames=600,
                 mean_rate=False, stale_after_sec=None):
        self.label = str(label)
        # mean_rate: rate over the whole run, not since the last write -- for
        # items as uneven as tests. stale_after_sec: when one item can
        # outlast STALE_AFTER_SEC without the process having died.
        self.mean_rate = mean_rate
        self.stale_after_sec = stale_after_sec
        self.total = max(1, int(total))
        self.kind = kind
        self.meta = dict(meta or {})
        self.every = max(1, int(every_frames))
        self.started = time.time()
        self._mark = (self.started, 0)
        self.path = progress_dir() / f"{_slug(self.label)}.json"
        self._write("running", 0, None)

    def update(self, done):
        if done % self.every and done < self.total:
            return
        now = time.time()
        then, before = (self.started, 0) if self.mean_rate else self._mark
        rate = (done - before) / (now - then) if now > then and done > before else None
        self._mark = (now, done)
        self._write("running", done, rate)

    def finish(self, status="done", done=None, **meta):
        self.meta.update(meta)
        self._write(status, self.total if done is None else done, None)

    def remove(self):
        """Take the record away, for a run that should not linger once over."""
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _write(self, status, done, rate):
        record = {
            "label": self.label, "kind": self.kind, "status": status,
            "done": int(done), "total": self.total,
            "started": self.started, "updated": time.time(),
            "rate_per_wall_sec": rate, "pid": os.getpid(), "meta": self.meta,
            "stale_after_sec": self.stale_after_sec,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(record), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass                 # progress is a courtesy, never a reason to fail a run


def describe(record, now=None):
    """Add the figures a monitor shows: fraction, state, ETA, age."""
    now = time.time() if now is None else now
    done, total = int(record.get("done") or 0), max(1, int(record.get("total") or 1))
    age = max(0.0, now - float(record.get("updated") or now))
    status = str(record.get("status") or "running")
    stale_after = float(record.get("stale_after_sec") or STALE_AFTER_SEC)
    state = "stalled" if status == "running" and age > stale_after else status
    rate = record.get("rate_per_wall_sec")
    if not rate and done and status == "running":
        elapsed = float(record.get("updated") or now) - float(record.get("started") or now)
        rate = done / elapsed if elapsed > 0 else None
    eta = (total - done) / rate if rate and state == "running" else None
    return dict(record, fraction=min(1.0, done / total), state=state, age=age,
                rate=rate, eta_sec=eta)


def read_all(directory=None, now=None):
    """Every progress record in ``directory``, campaigns first, newest first."""
    directory = pathlib.Path(directory) if directory else progress_dir()
    records = []
    for path in directory.glob("*.json") if directory.exists() else ():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or "label" not in record:
            continue  # some other JSON in the directory, not a progress record
        record["path"] = str(path)
        records.append(describe(record, now))
    return sorted(records, key=lambda r: (r.get("kind") != "campaign", -float(r.get("started") or 0)))


def jobs_progress(jobs_file, now=None):
    """Progress of a batch that writes no progress files: a jobs list whose
    lines end with each job's output path, done once that file exists."""
    jobs_file = pathlib.Path(jobs_file)
    try:
        lines = [line.split() for line in jobs_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return None
    outputs = [pathlib.Path(parts[-1]) for parts in lines if parts]
    done = sum(1 for path in outputs if path.exists())
    started = jobs_file.stat().st_mtime
    record = {"label": f"batch {jobs_file.parent.name}/{jobs_file.name}", "kind": "batch",
              "status": "done" if outputs and done == len(outputs) else "running",
              "done": done, "total": max(1, len(outputs)), "started": started,
              "updated": time.time() if now is None else now, "rate_per_wall_sec": None,
              "meta": {"jobs": str(jobs_file)}}
    finished = [path.stat().st_mtime for path in outputs if path.exists()]
    if finished and done < len(outputs):
        rate = done / max(1.0, max(finished) - started)
        record["rate_per_wall_sec"] = rate
    return describe(record, now)


def clear_finished(directory=None):
    """Delete the records of runs that are done, failed or stalled."""
    removed = 0
    for record in read_all(directory):
        if record["state"] != "running":
            try:
                os.remove(record["path"])
                removed += 1
            except OSError:
                pass
    return removed
