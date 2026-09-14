# batch_runner.py
"""Batch Benchmark Runner: chains the existing single-run timed benchmark
across every (model, seed) combination for an unattended comparison sweep.

This module is deliberately standalone -- it imports nothing from the rest
of the project -- so every piece of it (seed parsing, the preview math, and
the queue/failure-isolation state machine) is directly unit-testable without
Tk, without a live simulation, and without risking an import cycle with
control_panel.py/main.py. The engine here never re-implements a benchmark
run: ``BatchRunner`` only calls injected callbacks to trigger the ordinary
single-run path (the same ``request_start_test()`` a manual START TEST press
uses) and relies on its caller -- which can see wall-clock progress and log
contents -- to report back how each run ended.

Regime (demand, bus headways, speed scale, eligibility) is never touched
here: a batch varies only duration (fixed for the whole batch, chosen once
by the operator), seed, and model.
"""
import re

# Standard checkpoint marks, in sim-seconds. Duplicated from
# main.CHECKPOINT_MARKS_SEC (a fixed, HCM-inspired set that essentially never
# changes) so this module can compute an accurate preview without importing
# the composition root; callers that already have main's list on hand should
# pass it explicitly to compute_batch_preview/compute_checkpoint_count.
DEFAULT_CHECKPOINT_MARKS_SEC = (300, 600, 900, 1800, 3600, 7200)

_SEED_RANGE_RE = re.compile(r"\s*(-?\d+)\s*-\s*(-?\d+)\s*")
_SEED_SINGLE_RE = re.compile(r"\s*(-?\d+)\s*")


def parse_seed_list(text):
    """Parse a print-dialog style seed list into a sorted, deduplicated list
    of ints: ``"42,43,44"``, ``"42-46"``, or mixed ``"42,45-47,50"``.

    Returns ``(seeds, error)``: ``error`` is ``None`` on success (and
    ``seeds`` the parsed list); otherwise ``seeds`` is ``[]`` and ``error``
    is a human-readable message naming the offending entry.
    """
    text = (text or "").strip()
    if not text:
        return [], "Enter at least one seed"
    seeds = set()
    for raw_part in text.split(","):
        part = raw_part.strip()
        if not part:
            return [], "Invalid seed list: empty entry between commas"
        range_match = _SEED_RANGE_RE.fullmatch(part)
        if range_match:
            start, end = int(range_match.group(1)), int(range_match.group(2))
            if end < start:
                return [], f"Invalid range (end before start): {part!r}"
            seeds.update(range(start, end + 1))
            continue
        single_match = _SEED_SINGLE_RE.fullmatch(part)
        if single_match:
            seeds.add(int(single_match.group(1)))
            continue
        return [], f"Invalid seed entry: {part!r}"
    return sorted(seeds), None


def describe_seeds(seeds, max_shown=20):
    """Live-viewer label, e.g. ``"5 seeds: 42, 43, 44, 45, 46"``."""
    if not seeds:
        return "0 seeds"
    shown = seeds[:max_shown]
    listed = ", ".join(str(seed) for seed in shown)
    if len(seeds) > max_shown:
        listed += f", … (+{len(seeds) - max_shown} more)"
    return f"{len(seeds)} seed{'s' if len(seeds) != 1 else ''}: {listed}"


def expand_batch(models, seeds):
    """Expand the seed x model cross product as ``(seed, model)`` entries
    with seeds outer, models inner, so every model's result for one seed is
    complete early and can be analysed while the rest of the batch runs.
    The API rate-limit skip rule filters the whole remaining queue by model,
    so it does not depend on a model's runs being contiguous."""
    return [(seed, model) for seed in seeds for model in models]


def compute_checkpoint_count(duration_sim_seconds, checkpoint_marks_sec=DEFAULT_CHECKPOINT_MARKS_SEC):
    """Standard marks strictly below the duration, plus the duration's own
    final export -- matching main.compute_checkpoint_marks plus the
    finish_timed_test mark it deliberately excludes."""
    if not duration_sim_seconds:
        return 0
    marks_before = [mark for mark in checkpoint_marks_sec if mark < duration_sim_seconds]
    return len(marks_before) + 1


def estimate_wall_clock_seconds(run_count, duration_sim_seconds, sim_speed=1.0):
    """Rough estimate only: runs x duration / sim-speed. LLM inference
    latency is not modeled and can extend this considerably."""
    sim_speed = sim_speed if sim_speed and sim_speed > 0 else 1.0
    return run_count * (duration_sim_seconds or 0) / sim_speed


def format_duration_hms(total_seconds):
    """``~1h 40m`` / ``~45m`` / ``<1m`` style short duration text."""
    total_seconds = max(0, int(round(total_seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"~{hours}h {minutes}m"
    if hours:
        return f"~{hours}h"
    if minutes:
        return f"~{minutes}m"
    return "<1m"


def compute_batch_preview(
    model_count, seed_count, duration_sim_seconds, duration_label="",
    checkpoint_marks_sec=DEFAULT_CHECKPOINT_MARKS_SEC, sim_speed=1.0,
):
    """The live, read-only batch summary shown above RUN BATCH.

    Returns a dict with the individual figures plus a ready-to-display
    ``text`` block in the agreed three-line format.
    """
    runs = model_count * seed_count
    checkpoints_per_run = compute_checkpoint_count(duration_sim_seconds, checkpoint_marks_sec)
    result_sets = runs * checkpoints_per_run
    wall_clock_seconds = estimate_wall_clock_seconds(runs, duration_sim_seconds, sim_speed)
    wall_clock_text = format_duration_hms(wall_clock_seconds)
    duration_label = duration_label or (
        f"{int(duration_sim_seconds) // 60} min" if duration_sim_seconds else "?"
    )
    text = (
        f"{runs} runs  →  {result_sets} result sets\n"
        f"({model_count} models × {seed_count} seeds = {runs} runs; "
        f"{duration_label} duration = {checkpoints_per_run} checkpoints each)\n"
        f"Estimated wall-clock: {wall_clock_text}  (varies with model inference speed)"
    )
    return {
        "runs": runs,
        "result_sets": result_sets,
        "checkpoints_per_run": checkpoints_per_run,
        "wall_clock_seconds": wall_clock_seconds,
        "wall_clock_text": wall_clock_text,
        "text": text,
    }


class BatchRunner:
    """Sequences existing single timed-benchmark runs across a (model, seed)
    cross product.

    The engine holds no reference to control_panel/main: it is constructed
    with three callbacks (``set_seed``, ``set_model``, ``start_run``) that a
    caller wires to the real single-run path, and it is advanced entirely by
    that caller reporting outcomes through ``report_run_outcome`` -- never by
    polling simulation state itself. This keeps the queueing and
    failure-isolation logic (the part with real branching to get wrong)
    testable with plain fakes.
    """

    def __init__(self, set_seed, set_model, start_run):
        self._set_seed = set_seed
        self._set_model = set_model
        self._start_run = start_run
        self.queue = []
        self.results = []
        self.current = None
        self.state = "IDLE"
        self._consecutive_failures = {}

    @property
    def total(self):
        pending = len(self.queue) + (1 if self.current else 0)
        return len(self.results) + pending

    def is_active(self):
        return self.state in ("RUNNING", "STOPPING")

    def start(self, models, seeds):
        """Queue every (model, seed) combination and kick off the first."""
        self.queue = expand_batch(models, seeds)
        self.results = []
        self.current = None
        self._consecutive_failures = {}
        self.state = "RUNNING"
        self._advance()

    def request_stop(self):
        """STOP BATCH: abort after the run in flight finishes, never
        mid-run -- an in-progress run is left alone; only the queue is
        drained once it completes."""
        if self.state == "RUNNING":
            self.state = "STOPPING"

    def _advance(self):
        if self.current is not None:
            return
        if self.state != "RUNNING" or not self.queue:
            self.state = "DONE"
            self.current = None
            return
        seed, model = self.queue.pop(0)
        self.current = {"model": model, "seed": seed}
        self._set_seed(seed)
        self._set_model(model)
        self._start_run()

    def _skip_remaining_for_model(self, model, reason):
        remaining = []
        for pending_seed, pending_model in self.queue:
            if pending_model == model:
                self.results.append({
                    "model": pending_model, "seed": pending_seed,
                    "status": "SKIPPED", "reason": reason,
                })
            else:
                remaining.append((pending_seed, pending_model))
        self.queue = remaining

    def report_run_outcome(self, status, reason="", rate_limited=False, is_api_model=False):
        """Record how the run in flight ended and advance the queue.

        ``status`` is ``"COMPLETED"`` or ``"FAILED"``. A failed run never
        stops the batch -- it is logged and the next run starts -- unless
        ``rate_limited`` is true (or, when indistinguishable, two
        consecutive failures of the same ``is_api_model`` model in a row),
        in which case the remaining queued seeds for that model are skipped
        so a doomed API quota is not retried and local-model runs are
        never starved by it.
        """
        if self.current is None:
            return
        model, seed = self.current["model"], self.current["seed"]
        if status == "COMPLETED":
            self._consecutive_failures[model] = 0
            self.results.append(
                {"model": model, "seed": seed, "status": "COMPLETED", "reason": ""}
            )
        else:
            failures = self._consecutive_failures.get(model, 0) + 1
            self._consecutive_failures[model] = failures
            treat_as_rate_limited = rate_limited or (is_api_model and failures >= 2)
            final_reason = reason or (
                "rate_limited" if treat_as_rate_limited else "run_failed"
            )
            self.results.append(
                {"model": model, "seed": seed, "status": "FAILED", "reason": final_reason}
            )
            if treat_as_rate_limited:
                self._skip_remaining_for_model(
                    model, "skipped: prior run on this model hit an API rate limit"
                )
        self.current = None
        self._advance()
