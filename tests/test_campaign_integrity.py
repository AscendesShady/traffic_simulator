"""The properties a paired-DV campaign depends on.

Each of these was a real defect in campaign 2026-09-22 that silently cost
arms rather than failing loudly: 13 of 29 arms refused pairing, one whole
model arm scored as a total failure, and S differing by seed put two signal
regimes inside one sweep.
"""
import hashlib

import pytest

from src.agents import agent
from src.core import main
from src.core import vehicle as vehicle_module
from src.core.signal_controller import SignalController
from src.experiments import headless_run
from src.ui import control_panel

EMPTY_SCHEDULE = hashlib.sha256().hexdigest()[:16]


def _reset():
    signals = SignalController(control_panel.global_config)
    main.perform_full_reset([], signals, None)
    return signals


def test_the_demand_fingerprint_excludes_the_calibration_run():
    """Calibration spawns through the same sources. Clearing the digest
    before it ran left every run fingerprinting 'calibration plus run'."""
    _reset()
    assert main._demand_draw_hash_hex() == EMPTY_SCHEDULE


def test_the_demand_fingerprint_is_taken_by_frame_not_by_export_moment():
    """The campaign's 13 refused pairs were arms one frame apart, whose
    offered demand was in fact identical. A fingerprint keyed by frame
    cannot move when an export lands a frame late."""
    _reset()
    headless_run.run(234, 1200, tsp=False, dbl=False)
    at_mark = main._demand_draw_hash_hex(up_to_frame=600)
    offers_after = [f for f, _ in main._demand_draw_state["offers"] if f > 600]
    assert offers_after, "need arrivals past the mark for this to mean anything"
    # Running on past the mark cannot change the mark's own fingerprint.
    assert main._demand_draw_hash_hex(up_to_frame=600) == at_mark
    assert main._demand_draw_hash_hex() != at_mark


def test_identical_demand_across_arms_at_one_seed():
    """Common random numbers: the controller must not be able to change
    what any source offers."""
    hashes = []
    for tsp, dbl in ((False, False), (True, False), (True, True)):
        headless_run.run(234, 3000, tsp=tsp, dbl=dbl)
        hashes.append(main._demand_draw_hash_hex(up_to_frame=3000))
    assert len(set(hashes)) == 1, hashes


def test_saturation_flow_describes_the_regime_not_the_seed():
    """S seeded from the run seed gave 1887 veh/hr at seed 234 and 2338 at
    seed 764 -- a 24% swing that moved the Webster cycle from 81.6 s to its
    40 s floor and put two config_hash values in one campaign."""
    measured = {}
    for seed in (234, 764):
        control_panel.global_config["random_seed"] = seed
        main._saturation_cache.clear()          # force a real measurement
        _reset()
        measured[seed] = control_panel.global_config["measured_saturation_flow"]
    assert measured[234] == measured[764], measured


def test_saturation_flow_is_measured_once_per_regime(monkeypatch):
    """Every run of a sweep shares one S, so a sweep cannot disagree with
    itself and each run gets its calibration time back."""
    main._saturation_cache.clear()
    calls = []
    monkeypatch.setattr(
        main, "calibrate_saturation_flow",
        lambda *a, **k: calls.append(1) or 1800.0,
    )
    signals = SignalController(control_panel.global_config)
    for _ in range(3):
        main.calibrate_and_apply_webster(signals)
    assert len(calls) == 1


def test_a_busy_provider_is_a_skipped_tick_not_a_rejected_decision():
    """A call that never reached the model is not a model failure. It used
    to publish an all-off decision and count as a guard reject: 611 of
    grok-4.6's 787 rejects, scoring a working arm at decisions_effective 0."""
    assert issubclass(agent.ModelBusyError, RuntimeError)

    def busy(*_args, **_kwargs):
        raise agent.ModelBusyError("previous Grok request is still running")

    state = {"model": "grok-4.6", "minimap": "", "turn": 1, "telemetry": {},
             "control_mode": "assisted"}
    # Every other failure is caught into an INVALID response the guard turns
    # into HELD_ALL_OFF; this one has to escape so the tick can be skipped.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(agent, "_call_grok", busy)
        with pytest.raises(agent.ModelBusyError):
            agent.ai_turn(state)

        patch.setattr(agent, "_call_grok",
                      lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
        invalid = agent.ai_turn(state)
        assert invalid["status"] == "INVALID"     # an ordinary failure still holds


def test_vehicle_serials_are_unique_and_reproducible():
    """Counting distinct vehicles by id() undercounts: CPython reuses the
    address of a freed vehicle. Measured 147-150 against 168 actual, and the
    figure moved with unrelated allocations."""
    counts = []
    for _ in range(2):
        captured = {}
        headless_run.run(
            234, 2400, tsp=True, dbl=True,
            on_finish=lambda v, s: captured.update(serials=[x.serial for x in v]),
        )
        assert len(set(captured["serials"])) == len(captured["serials"])
        counts.append(captured["serials"])
    assert counts[0] == counts[1]      # same seed, same numbering


def test_cross_street_counters_use_serials_not_addresses():
    for key in ("cross_street_vehicle_ids", "cross_street_passenger_ids",
                "spillback_active_ids"):
        assert key in main._new_run_metrics()
    headless_run.run(234, 2400, tsp=True, dbl=True)
    counted = main.run_metrics["cross_street_vehicle_ids"]
    assert all(isinstance(v, int) for v in counted)
    # Serials are small ordinals; raw id() values are machine addresses.
    assert not counted or max(counted) < 10 ** 6
