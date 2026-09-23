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

import src.ui.control_panel as control_panel
from src.core.signal_controller import (
    SignalController,
    TSP_ACTION_EXTENDING,
    TSP_MAX_ADJUST_FRACTION,
)
from src.experiments import headless_run

pytestmark = pytest.mark.skipif(
    not os.environ.get("TSP_BATCH_TESTS"),
    reason="slow seed batch; set TSP_BATCH_TESTS=1",
)

SEEDS = (1, 2, 3, 4, 5)
FRAMES = 5 * 3600


def _median_all(waits):
    return statistics.median(w for values in waits.values() for w in values)


def test_t5_tsp_on_does_not_make_buses_wait_longer_than_tsp_off():
    off = headless_run.node_waits_by_action(headless_run.batch(SEEDS, FRAMES, tsp=False))
    on = headless_run.node_waits_by_action(headless_run.batch(SEEDS, FRAMES, tsp=True))
    assert "early_green" in on and "extending" in on
    assert _median_all(on) <= _median_all(off), (on, off)


def test_t6_extensions_are_granted_only_to_buses_that_can_use_them():
    """T6 was first pinned as "median extension wait == 0 frames" from the
    400 px link. That pin was a knife-edge (19 of 36 extended crossings at
    zero, the rest in the hundreds) and measures the wrong thing:
    node_wait_frames counts every stopped frame of the approach leg --
    queue discharge before the green, a permissive left's gap wait, a
    spillback hold at the bar -- none of which the signal can remove. The
    200 m link doubled both the leg and the eligibility zone, and the
    replay (2026-09-21) showed what actually broke: extensions were granted
    to any armed bus at green end, so buses 230-390 px out at 0.5 px/frame
    held the cross street red for the whole 265-frame cap and still met the
    red (27 of 42 extensions hit the cap). The arrival gate on extensions
    (_extension_is_feasible) is what T6 now pins: most extended buses
    cross inside the cap, and a bus a green was held for never waits more
    than an untreated one on the same seeds.
    """
    records = headless_run.batch(SEEDS, FRAMES, tsp=True)
    on = headless_run.node_waits_by_action(records)
    controller = SignalController(global_config=control_panel.global_config)
    capped = crossings = 0
    for record in records:
        for node in record.get("nodes", []):
            if node.get("tsp_action") != TSP_ACTION_EXTENDING or node.get("node_clear_frame") is None:
                continue
            crossings += 1
            cap = int(controller.get_green_time(node["node_x"], 0) * TSP_MAX_ADJUST_FRACTION)
            capped += int(node["tsp_adjust_frames"]) >= cap
    assert crossings >= 5
    assert capped <= crossings / 2, (capped, crossings)
    assert statistics.median(on["extending"]) <= statistics.median(on["none"]), on
