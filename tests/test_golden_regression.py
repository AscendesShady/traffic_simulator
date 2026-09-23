"""Golden-output regression: canonical scenarios whose complete end state is
pinned, so ANY change to what the simulation produces fails the suite until
someone accepts it on purpose.

SUMO's correctness rests on exactly this -- scenario runs diffed against
checked-in expected output, with an explicit "patch expected results" commit
to accept a change (reverse_engineering/13_TESTING_ARCHITECTURE.md). The
rest of this suite asserts properties (inequalities, invariants); a change
that shifted every result while keeping each property true would pass all of
them. This file is the one place such a shift cannot hide.

To accept an intended change, re-pin and commit the new golden file with the
change that caused it, saying why in the commit message:

    set REPIN_GOLDEN=1
    python -m pytest -c tests/pytest.ini tests/test_golden_regression.py

Floats are rounded to 6 decimals before hashing so the fingerprint is stable
across platforms; a difference that survives that is a real one.
"""
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from src.core import main
from src.experiments import headless_run
from tests.test_step_equivalence import _fingerprint

GOLDEN_PATH = Path(__file__).parent / "golden" / "scenarios.json"
SCENARIOS = {
    # name: (seed, frames, tsp, dbl) on the default configuration
    "baseline_seed234": (234, 3000, False, False),
    "tsp_dbl_seed234": (234, 3000, True, True),
}


def _canonical(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, set):
        return sorted(_canonical(v) for v in value)
    return value


def _run(seed, frames, tsp, dbl):
    state = {}
    headless_run.run(
        seed, frames, tsp=tsp, dbl=dbl,
        on_finish=lambda v, s: state.update(copy.deepcopy(_fingerprint(v, s))),
    )
    canonical = _canonical(state)
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
    t = main.network_throughput
    headline = {
        "vehicles_served": t["vehicles_served_total"],
        "passengers_served": t["passengers_served_total"],
        "person_hours_travel_delay": round(
            (t["bus_passenger_travel_delay_frames"] + t["car_passenger_travel_delay_frames"]) / 216000, 4
        ),
        "person_hours_entry_wait": round(
            (t["bus_passenger_entry_wait_frames"] + t["car_passenger_entry_wait_frames"]) / 216000, 4
        ),
        "vehicles_in_network": len(state["vehicles"]),
        "demand_hash": state["demand_hash"],
    }
    return digest, headline


def _golden():
    if GOLDEN_PATH.exists():
        return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    return {}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_matches_its_golden_output(name):
    digest, headline = _run(*SCENARIOS[name])
    if os.environ.get("REPIN_GOLDEN"):
        golden = _golden()
        golden[name] = {"fingerprint": digest, "headline": headline, "scenario": SCENARIOS[name]}
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip(f"re-pinned {name}")
    expected = _golden().get(name)
    assert expected is not None, f"no golden output for {name}; run with REPIN_GOLDEN=1"
    assert list(expected["scenario"]) == list(SCENARIOS[name]), "scenario definition changed"
    assert digest == expected["fingerprint"], (
        f"{name} output changed.\n  golden:  {expected['headline']}\n  now:     {headline}\n"
        "If intended, re-pin with REPIN_GOLDEN=1 and commit the golden file with the reason."
    )


def test_the_fingerprint_is_deterministic_within_a_process():
    """Pinning is only meaningful if the same run always hashes the same."""
    first = _run(11, 1200, True, False)[0]
    second = _run(11, 1200, True, False)[0]
    assert first == second
