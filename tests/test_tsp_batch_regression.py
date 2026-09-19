"""Seed-batch regression for the TSP arbiter (audit: tsp-early-green-mistiming).

Slow (a few minutes): five seeded 5-minute headless runs per arm. Enabled
with TSP_BATCH_TESTS=1; run it after any change to signal or movement code.

T5 as first written (median wait of early_green crossings <= median of
`none` crossings) is a label-conditional statistic and cannot hold: the
early_green label is applied to the buses that met a red, `none` to the
buses that met a green. The acceptance criterion here is the paired-seed
arm comparison instead: with TSP on, buses as a whole wait no more than
with TSP off on the same seeds.
"""
import os
import statistics

import pytest

from src.experiments import headless_run

pytestmark = pytest.mark.skipif(
    not os.environ.get("TSP_BATCH_TESTS"),
    reason="slow seed batch; set TSP_BATCH_TESTS=1",
)

SEEDS = (1, 2, 3, 4, 5)
FRAMES = 5 * 3600
# Pinned from the pre-gate controller on the same seeds (panel default
# demand). The extension path is untouched, so this must not move (T6).
PRE_PATCH_EXTENDING_MEDIAN_WAIT = 0.0


def _median_all(waits):
    return statistics.median(w for values in waits.values() for w in values)


def test_t5_tsp_on_does_not_make_buses_wait_longer_than_tsp_off():
    off = headless_run.node_waits_by_action(headless_run.batch(SEEDS, FRAMES, tsp=False))
    on = headless_run.node_waits_by_action(headless_run.batch(SEEDS, FRAMES, tsp=True))
    assert "early_green" in on and "extending" in on
    assert _median_all(on) <= _median_all(off), (on, off)


def test_t6_extension_behaviour_unchanged():
    on = headless_run.node_waits_by_action(headless_run.batch(SEEDS, FRAMES, tsp=True))
    extending = statistics.median(on["extending"])
    tolerance = max(0.05 * PRE_PATCH_EXTENDING_MEDIAN_WAIT, 1e-9)
    assert abs(extending - PRE_PATCH_EXTENDING_MEDIAN_WAIT) <= tolerance
