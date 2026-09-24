"""Physical calibration of the "idm" movement engine (audit item 10).

The legacy engine is kept as-is for result continuity and is the default;
these tests select the IDM engine, check its units against accepted
reference values, and put the audit's own numbers for the legacy engine on
record so nobody mistakes them for calibration.
"""
import itertools
import statistics

import pytest

import src.ui.control_panel as control_panel
import src.core.main as main
import src.telemetry.real_world_units as units
from src.core import vehicle as vm
from src.core.vehicle import Vehicle
from src.experiments import headless_run
from src.ui import canvas_gemini as canvas
from tests.helpers import NETWORK_CROSSING_FRAMES, NODE_A, apply_reference_demand
from tests.test_dbl_lane_clearing import rectangles_overlap

H_Y, LANE = canvas.H_Y, canvas.LANE
LANE1_Y = H_Y - 1.5 * LANE
BAR_X = NODE_A - canvas.ROAD_W // 2 - canvas.STOP
ALL_GREEN = {n: {d: "GREEN" for d in ("EB", "WB", "NB", "SB")} for n in canvas.INT_X}
ALL_RED = {n: {d: "RED" for d in ("EB", "WB", "NB", "SB")} for n in canvas.INT_X}


@pytest.fixture
def idm():
    vm.set_movement_model(vm.MOVEMENT_MODEL_IDM)
    yield
    vm.set_movement_model(vm.MOVEMENT_MODEL_LEGACY)


def advance(vehicles, frames, signals=ALL_GREEN):
    for _ in range(frames):
        for v in vehicles:
            v.update(signals, canvas.INT_X, H_Y, canvas.ROAD_W, canvas.STOP, LANE, all_vehicles=vehicles)


def car(x, y=LANE1_Y, speed=0.6, **kw):
    v = Vehicle(x, y, "EB", max_speed=0.6, **kw)
    v.speed = speed
    return v


def test_one_physical_scale_and_the_legacy_numbers_the_audit_measured():
    assert vm.METERS_PER_PX == units.meters_per_pixel()
    assert vm.px_per_frame_to_mps(1.0) == 15.0                       # 54 km/h
    # The legacy ramp and slide really are 45 m/s^2 and 7.5 m/s: not calibrated.
    assert vm.px_per_frame2_to_mps2(vm.LEGACY_ACCEL_PX_PER_FRAME2) == 45.0
    assert vm.px_per_frame_to_mps(vm.LANE_CHANGE_STEP_PX) == 7.5
    assert vm.movement_model() == vm.MOVEMENT_MODEL_IDM              # default
    with pytest.raises(ValueError):
        vm.set_movement_model("krauss")


def test_engine_is_selected_at_reset_and_is_part_of_the_regime(monkeypatch):
    from src.core.signal_controller import SignalController
    monkeypatch.setitem(control_panel.global_config, "movement_model", "idm")
    main.perform_full_reset([], SignalController({"green_time": 100}))
    assert vm.movement_model() == "idm"
    idm_hash = main._config_regime_hash(control_panel.global_config)
    vm.set_movement_model("legacy")
    assert main._config_regime_hash(control_panel.global_config) != idm_hash
    assert main.build_experiment_summary_row(60)["movement_model"] == "legacy"


def test_idm_saturation_flow_matches_hcm_at_arterial_speed(idm):
    """The production calibrator (standing queue, single lane) under IDM:
    1,900 veh/hr/lane at HCM's ~55 km/h discharge speed (scale 1.0), and a
    lower low-speed value at the default 0.5 scale (~32 km/h free flow)."""
    assert 1800 <= main.calibrate_saturation_flow(1.0, 0.0, seed=1) <= 2050
    assert 1350 <= main.calibrate_saturation_flow(0.5, 0.0, seed=1) <= 1600


def test_idm_free_flow_and_acceleration_by_class(idm):
    v = car(-100, speed=0.0)
    frames = 0
    while v.speed < 0.95 * v.max_speed and frames < 3000:
        advance([v], 1)
        frames += 1
    assert 4.0 <= frames / 60 <= 12.0                    # 0 -> 32 km/h, a = 2 m/s^2 tapering
    advance([v], 600)
    assert v.speed == pytest.approx(v.max_speed, rel=0.01)   # free flow = own v0
    truck = Vehicle(-100, LANE1_Y, "EB", max_speed=0.6, is_heavy=True)
    truck.speed = 0.0
    advance([truck], frames)
    assert truck.speed < v.max_speed * 0.95                   # a_truck < a_car


def test_idm_brakes_comfortably_to_a_red_bar(idm):
    v = car(200)
    peak = 0.0
    for _ in range(3000):
        before = v.speed
        advance([v], 1, ALL_RED)
        if before > 0.1:
            peak = max(peak, before - v.speed)
        if v.speed == 0.0:
            break
    b_car = vm.IDM_PARAMS_MPS["car"]["b"]
    assert vm.px_per_frame2_to_mps2(peak) <= b_car * 1.25
    gap = v.distance_to_node_stop_bar(NODE_A, H_Y, canvas.ROAD_W, canvas.STOP)
    assert vm.IDM_S0_PX - 1 <= gap <= vm.IDM_S0_PX + 4


def test_legacy_stops_dead_which_is_what_the_audit_found():
    v = car(BAR_X - 20)                                         # 11 px from the bar
    advance([v], 1, ALL_RED)
    assert v.speed == 0.0                                       # 0.6 px/frame -> 0 in one frame


def test_idm_queue_spacing_and_shockwave_speed(idm):
    """A platoon at free flow running into a stopped car: queue tail moves
    upstream at a plausible urban wave speed and the standing queue keeps
    HCM's ~7.5 m spacing."""
    lead = car(600, speed=0.0)
    platoon = [lead] + [car(600 - 40 - i * 72) for i in range(1, 16)]
    tail = []
    for frame in range(2400):
        lead.speed = 0.0
        advance(platoon, 1)
        lead.speed = 0.0
        stopped = [c for c in platoon if c.speed < 0.05]
        tail.append((frame, min(c.x for c in stopped)))
    (f0, x0), (f1, x1) = tail[300], tail[-1]
    wave_kmh = vm.px_per_frame_to_mps((x1 - x0) / (f1 - f0)) * 3.6
    assert -20.0 <= wave_kmh <= -6.0, wave_kmh
    spacing_m = statistics.mean(
        abs(platoon[i].x - platoon[i + 1].x) for i in range(5)
    ) * vm.METERS_PER_PX
    assert 7.0 <= spacing_m <= 8.0


def test_idm_lane_change_takes_three_seconds(idm):
    v = car(100, y=H_Y - 0.5 * LANE)
    v.lane_index = 0
    v.lane_vacate_target = 1
    frames = 0
    while v.lane_vacate_target is not None and frames < 1000:
        v.step_lane_vacate(canvas.INT_X, H_Y, canvas.ROAD_W, LANE, [v])
        frames += 1
    assert 2.8 <= frames / 60 <= 3.1
    assert v.lane_index == 1


def test_mobil_refuses_a_change_that_brakes_the_new_follower(idm, monkeypatch):
    """Full MOBIL. Inward move: the subject gains (slow leader) but the new
    follower would have to brake past the safe limit -> refused; further
    back it is safe and the gain carries. Outward (keep-outer) move with no
    gain of its own: a faster follower's disadvantage refuses it at
    p = 0.3 and not at p = 0 -- the politeness term MOBIL-lite lacked."""
    lane0, lane1 = H_Y - 0.5 * LANE, H_Y - 1.5 * LANE
    subject = car(300, y=lane0)
    subject.lane_index = 0
    slow_leader = car(340, y=lane0, speed=0.2)
    slow_leader.max_speed = 0.2
    follower = car(250, y=lane1, speed=0.7)
    follower.max_speed = 0.7
    follower.lane_index = 1
    assert subject.choose_discretionary_lane(H_Y, LANE, [subject, slow_leader, follower]) is None
    follower.x = 200
    assert subject.choose_discretionary_lane(H_Y, LANE, [subject, slow_leader, follower]) == 1

    subject = car(300, y=lane1)
    subject.lane_index = 1
    follower = car(200, y=lane0, speed=0.7)
    follower.max_speed = 0.7
    follower.lane_index = 0
    assert subject.choose_discretionary_lane(H_Y, LANE, [subject, follower]) is None
    monkeypatch.setattr(vm, "MOBIL_POLITENESS", 0.0)
    assert subject.choose_discretionary_lane(H_Y, LANE, [subject, follower]) == 0


def test_idm_engine_drives_the_whole_network_without_overlaps(monkeypatch):
    monkeypatch.setitem(control_panel.global_config, "movement_model", "idm")
    apply_reference_demand()   # a 3000-frame soak needs a cycle that ends inside it
    overlaps = []
    real_step = main.step_simulation

    def checked(vehicles, signals, frame, decide=None):
        real_step(vehicles, signals, frame, decide=decide)
        if frame % 10 == 0:
            overlaps.extend(
                (frame, a.x, b.x) for a, b in itertools.combinations(vehicles, 2)
                if rectangles_overlap(a, b)
            )
    monkeypatch.setattr(main, "step_simulation", checked)
    try:
        headless_run.run(7, NETWORK_CROSSING_FRAMES * 2 // 3, tsp=True, dbl=True)
        assert vm.movement_model() == "idm"
        assert main.network_throughput["vehicles_served_total"] > 0
        assert overlaps == []
    finally:
        vm.set_movement_model(vm.MOVEMENT_MODEL_LEGACY)


def test_the_canvas_scale_is_the_engine_scale():
    """canvas_gemini builds the network from lengths in metres at PX_PER_M;
    it must be the same scale the engine and the unit conversions use."""
    from src.ui import canvas_gemini as canvas
    assert canvas.PX_PER_M * vm.METERS_PER_PX == 1.0
    assert (canvas.INT_X[1] - canvas.INT_X[0]) * vm.METERS_PER_PX == canvas.LINK_M
    assert canvas.INT_X[0] * vm.METERS_PER_PX == canvas.APPROACH_M
    assert canvas.H_Y * vm.METERS_PER_PX == canvas.APPROACH_M
