# DBL + TSP Simultaneous Activation — Blocked Middle Lane Check — 2026-09-07

**Check performed:** 2026-09-07 (Asia/Dhaka)
**Project:** `traffic_simulator`
**Scope:** A route with both TSP and DBL enabled at the same time, whose DBL merge lane (the middle lane, lane index `2`) is permanently occupied by a stationary blocking vehicle
**Method:** Source trace of `signal_controller.py` and `vehicle.py`, followed by a targeted, code-level simulation (`SignalController` + `Vehicle`/`Bus` driven directly, no GUI) reproducing the scenario for 6,000 simulated frames
**Result:** **No gridlock.** The scenario is already handled safely by the priority-gridlock fix committed in `467fbc7` (`INFEASIBLE_GRANT` feasibility gate + suppression). No code change made.

## 1. Scenario definition

`bus_routes_config` allows a route to have `tsp_enabled` and `dbl_enabled` set
independently (`control_panel.py:122-195`). Nothing in configuration prevents
both being `True` on the same route simultaneously, and the panel exposes
both as independent toggles.

The DBL lane is a fixed constant, `DBL_LANE_INDEX = 2` (`vehicle.py:7`) — the
left-most of the three lanes (`0`, `1`, `2`) on each approach. A bus on a
DBL-enabled leg is steered toward this lane regardless of its route's
*configured* entry lane, as soon as it is on that leg
(`vehicle.py:387-405`, `Bus.update`):

```python
if turn_lane_change_due:
    target_lane = required_lane
elif dbl_enabled_for_leg:
    target_lane = DBL_LANE_INDEX
else:
    target_lane = required_lane
```

If the target lane is not physically clear (`is_target_lane_clear` finds a
same-direction vehicle overlapping the merge corridor,
`vehicle.py:352-361`), the bus sets `must_hold_for_lane = True` and holds
station upstream of the stop bar (`vehicle.py:207`) rather than forcing the
merge. This is the "blocked middle lane" condition: a straight-through DBL
route (e.g. `R3_EB_ONLY`, configured lane `1`) whose lane `2` is permanently
occupied by another vehicle (a stalled car, or a left-turning truck, which
also uses lane `2` per `vehicle.py:311`) can never complete its DBL merge.

The concern this check answers: with **both** TSP and DBL requesting
priority for the same bus at the same node, does the controller ever grant
(or get stuck attempting to grant) a green phase to a bus that physically
cannot use it — producing a stuck `ALL_RED_CLEARANCE` / `PRIORITY_ACTIVE`
state and starving the other approaches, the same failure class documented in
`GRIDLOCK_PRIORITY_STARVATION_INCIDENT_2026-09-07.md`?

## 2. Why TSP and DBL can disagree on lane at request time

`_collect_priority_requests` (`signal_controller.py:1322-1352`) evaluates TSP
and DBL eligibility independently for every bus, every frame:

- `is_bus_tsp_eligible` only checks route config + distance to stop bar
  (`signal_controller.py:1257-1266`) — **lane-independent**.
- `is_bus_dbl_eligible` additionally requires
  `bus.lane_index == DBL_LANE_INDEX` (`signal_controller.py:1279-1287`) —
  **lane-dependent**.

So while a bus is blocked out of lane `2`, `tsp_requested` can be `True`
while `dbl_requested` is `False` on the very same route/leg, even though DBL
is enabled in config. The request is still built
(`_build_request`, `signal_controller.py:1298-1321`) with
`entry_lane = DBL_LANE_INDEX if dbl_requested else leg["entry_lane"]` — i.e.
`entry_lane` falls back to the route's *configured* lane (`1` for `R3`), not
the physical lane the bus is trying (and failing) to reach.

## 3. Existing safeguard

This is exactly the mismatch the feasibility gate added in `467fbc7`
(`fix: prevent priority-green gridlock via grant feasibility check and
active-stall watchdog`) is built to catch. `_bus_can_use_grant`
(`signal_controller.py:1437-1448`) is evaluated once `ALL_RED_CLEARANCE`
would otherwise transition to `PRIORITY_ACTIVE`:

```python
bus.lane_index == request.entry_lane
and not getattr(bus, "must_hold_for_lane", False)
```

A bus still holding for a blocked DBL merge has `must_hold_for_lane = True`,
so the grant is refused via `_deny_active_grant(node, request,
INFEASIBLE_GRANT)` instead of being activated. The node proceeds through its
existing `RECOVERY_ALL_RED` path back to `NORMAL`, and
`_collect_priority_requests`'s suppression
(`node.suppressed_keys.intersection_update(eligible_keys[node_x])`,
`signal_controller.py:1350-1352`) prevents the same bus from immediately
re-queuing a new request while it remains stuck in the eligibility zone.

## 4. Verification

A direct, code-level reproduction (no GUI) was run: `R3_EB_ONLY` (straight
route, configured entry lane `1` at Node A / x=300) with `tsp_enabled` and
`dbl_enabled` both set `True`, a bus placed 150px upstream of the stop bar
(inside the 250px priority-eligibility zone, outside the 35px merge-hold
zone), and a stationary blocking vehicle parked in lane `2` at the same
x-position for the entire run, so the bus's DBL merge can never succeed.

| Metric (6,000-frame run) | Observed |
|---|---|
| `PRIORITY_ACTIVE` ever reached | No — grant never activated |
| Terminal request outcome | `DENIED`, reason `INFEASIBLE_GRANT`, at frame 91 |
| `tsp_requested` / `dbl_requested` on the denied request | `True` / `False` (confirms the lane mismatch described in §2) |
| Node priority-state transitions over the full run | `CONFLICT_YELLOW → ALL_RED_CLEARANCE → RECOVERY_ALL_RED → NORMAL`, exactly once |
| Repeat denials / request-retry thrash | None — 1 terminal-history entry total, no further requests while bus remains in zone |
| Node phase cycling after recovery | Resumed normal signal service (`phase=3`, `timer` advancing) |
| Bus final position | Held upstream of the stop bar, never crossed into the intersection, never reached lane `2` |

No stuck state, no repeated all-red thrashing, and no starvation of the other
approaches was observed. The bus itself remains queued behind the permanent
blocker indefinitely — expected physical behavior given the synthetic
scenario (a real blocker that never moves is not something signal control
can resolve) — but this is a lane-congestion outcome local to that bus, not
an intersection-wide gridlock.

## 5. Conclusion

Simultaneous TSP + DBL activation on a route, combined with a permanently
blocked DBL/middle lane, **does not reproduce a gridlock** on the current
`main` branch. The `INFEASIBLE_GRANT` feasibility gate added for the
priority-green starvation incident already covers this case because it
checks the bus's *actual* lane and hold state at grant time, independent of
which feature (TSP, DBL, or both) requested the priority.

**No code change was made as a result of this check.** This report exists as
a documented verification, per the incident-report convention in this
`audits/` directory, that the fix in `467fbc7` generalizes to the
simultaneous-DBL+TSP case and not only to the single-feature TSP scenario
originally captured.

## 6. Scope note

This check covers the *entry-time* mismatch (bus never reaches the DBL lane
before a grant would activate). It does not re-verify the *in-progress*
stall case (bus already granted and moving, then blocked mid-approach) —
that path is covered by `ACTIVE_STALL_TIMEOUT` and is already exercised by
the existing test suite (`tests/test_signal_priority.py`,
`tests/test_signal_controller_reset.py`). Recommend adding the scenario in
§4 as a permanent regression test if this configuration (TSP+DBL together
with a lane-blocking left-turner) is expected in production traffic mixes.
