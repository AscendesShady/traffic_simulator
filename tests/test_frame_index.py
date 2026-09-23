"""The per-frame spatial index (vehicle._FrameIndex) is a pure accelerator.

It narrows WHICH vehicles a neighbour query considers; it must never change
the answer. Every assertion here is equivalence against the full scan the
index replaced, because a movement change would invalidate every pinned
result in results/ while a speed change invalidates nothing.
"""
import copy

import pytest

from src.core import main
from src.core import vehicle as vehicle_module
from src.core.vehicle import Bus, Vehicle
from src.experiments import headless_run
from src.ui.canvas_gemini import LANE

from tests.test_step_equivalence import _fingerprint

# Sets of id() -- raw CPython addresses, which are reused as vehicles are
# freed, so their contents (and even their size) depend on the allocator
# rather than on the simulation. Compared apart, by the counters they feed.
ADDRESS_KEYED = (
    "cross_street_vehicle_ids",
    "cross_street_passenger_ids",
    "cross_street_unique_pax",
)


def _run(seed, frames, indexed):
    vehicle_module.USE_FRAME_INDEX = indexed
    try:
        state = {}
        headless_run.run(
            seed, frames, tsp=True, dbl=True,
            on_finish=lambda v, s: state.update(copy.deepcopy(_fingerprint(v, s))),
        )
    finally:
        vehicle_module.USE_FRAME_INDEX = True
    for key in ADDRESS_KEYED:
        state["run_metrics"].pop(key, None)
    return state


@pytest.mark.parametrize("seed", [11, 234])
def test_indexed_engine_is_the_scanned_engine(seed):
    """A whole run, frame for frame: same vehicles, same controller state,
    same throughput, same warm-up snapshot, same offered demand."""
    assert _run(seed, 2400, indexed=True) == _run(seed, 2400, indexed=False)


def test_get_lead_vehicle_agrees_with_the_full_scan_on_a_busy_frame():
    """Every vehicle's leader and gap, indexed vs scanned, on a populated
    network -- including the at_y lookahead a lane change uses."""
    vehicles, signals, _ = _busy_network(1800)
    vehicle_module.index_frame(vehicles)
    for v in vehicles:
        for at_y in (None, v.y - LANE, v.y + LANE):
            vehicle_module.USE_FRAME_INDEX = True
            fast = v.get_lead_vehicle(vehicles, at_y=at_y)
            vehicle_module.USE_FRAME_INDEX = False
            slow = v.get_lead_vehicle(vehicles, at_y=at_y)
            vehicle_module.USE_FRAME_INDEX = True
            assert fast[0] == slow[0], f"{v.direction} gap at y={at_y}"
            assert fast[1] is slow[1], f"{v.direction} leader at y={at_y}"


def test_index_covers_vehicles_past_the_canvas_edge():
    """A vehicle stays in the list for 60 px beyond the surface before the
    frame removes it. Bounding the cell walk by the canvas size once lost
    those leaders, and the vehicle behind drove through the gap."""
    from src.ui.canvas_gemini import HEIGHT, H_Y, LANE
    lane_x = 1655.0
    leader = Vehicle(lane_x, HEIGHT + 40.0, "SB", max_speed=1.0, lane_index=2)
    follower = Vehicle(lane_x, HEIGHT - 30.0, "SB", max_speed=1.0, lane_index=2)
    vehicles = [leader, follower]
    vehicle_module.index_frame(vehicles)
    assert follower.get_lead_vehicle(vehicles)[1] is leader


def test_a_foreign_list_falls_back_to_the_full_scan():
    """Telemetry, the saturation calibrator and the tests all pass lists the
    index was not built from; they must still get correct answers."""
    indexed = [Vehicle(100.0, 289.0, "EB", max_speed=1.0, lane_index=0)]
    vehicle_module.index_frame(indexed)
    other = [
        Vehicle(100.0, 289.0, "EB", max_speed=1.0, lane_index=0),
        Vehicle(300.0, 289.0, "EB", max_speed=1.0, lane_index=0),
    ]
    assert vehicle_module._index_for(other) is None
    assert other[0].get_lead_vehicle(other)[1] is other[1]
    assert vehicle_module.buses_in(other) == []


def test_removing_a_vehicle_mid_frame_keeps_the_index_honest():
    """step_simulation removes vehicles while the frame is still reading
    neighbours; a dropped vehicle must never come back as a leader."""
    a = Vehicle(100.0, 289.0, "EB", max_speed=1.0, lane_index=0)
    b = Vehicle(300.0, 289.0, "EB", max_speed=1.0, lane_index=0)
    c = Vehicle(500.0, 289.0, "EB", max_speed=1.0, lane_index=0)
    vehicles = [a, b, c]
    vehicle_module.index_frame(vehicles)
    assert a.get_lead_vehicle(vehicles)[1] is b
    vehicles.remove(b)
    vehicle_module.drop_from_index(b)
    assert vehicle_module._index_for(vehicles) is not None  # fast path survives
    assert a.get_lead_vehicle(vehicles)[1] is c


def test_buses_in_keeps_list_order():
    """should_yield_for_route_merge and dbl_merge_bus_blocked_by both take
    the first match, so the cached bus list must be in list order."""
    vehicles, _, _ = _busy_network(2400)
    vehicle_module.index_frame(vehicles)
    assert vehicle_module.buses_in(vehicles) == [v for v in vehicles if isinstance(v, Bus)]


def _busy_network(frames):
    """A seeded headless run stopped mid-flight, so the assertions run
    against a real congested frame rather than a hand-placed one."""
    captured = {}
    headless_run.run(
        7, frames, tsp=True, dbl=True,
        on_finish=lambda v, s: captured.update({"v": list(v), "s": s}),
    )
    return captured["v"], captured["s"], frames
