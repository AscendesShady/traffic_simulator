# Gridlock Incident Report — 2026-09-07

**Capture source:** `traffic_state_telemetry.json` (schema 3), `decision.json`, `ai_control.json`, `agent_turn_log.jsonl`, `agent_rejects.log`
**Frame at capture:** 46,924 (`simulation_time_seconds = 782.1`)
**Simulation state:** paused (`simulation_paused: true`)
**Severity:** Critical — both signalized intersections are locked, EB demand is fully starved, and the AI signal-priority controller has failed safe and gone dormant.

## 1. Executive summary

Both intersections (node `300` and node `700`) have been stuck showing **WB green / EB+NB+SB red** continuously for roughly **36,900 frames (~10.3 minutes of sim time)** — since frame ~21,230 and ~22,117 respectively, against a current frame of 46,924. In that entire window, every eastbound priority request from route `R1_EB_A_NB` has failed: 29+ attempts cycling `REQUESTED → DENIED (REQUEST_TIMEOUT)` or `→ CANCELLED (FEATURE_DISABLED)`, with 3 buses currently re-queued and still unserved. No auto-discharge recovery has engaged (`discharge_active: false`, `discharge_plan: null` at both nodes).

Independently, the LLM signal-priority agent (`llama3.1:8b` via Ollama) stopped producing valid decisions three turns ago. `guard.py`'s fail-safe (`safe_decision`) tripped `HELD_ALL_OFF`, and `ai_control.json` now shows the control loop disarmed (`"armed": false`). So even if the stuck priority state could self-clear, nothing is currently governing it.

This is the same structural failure mode documented in `audits/AUTO_DISCHARGE_GRIDLOCK_INCIDENT_AUDIT_2026-09-05.md`: a controller state that grants a bus the right-of-way with **no independent timeout**, gated only on that bus physically clearing the intersection — and no recovery branch fires when it doesn't.

## 2. Evidence

### 2.1 Stuck priority state

| Node | Holding approach | Bus | Requested at frame | `expires_at_frame` | Current frame | Held for |
|---|---|---|---:|---:|---:|---:|
| 300 | WB (left → SB) | `BUS_R4_WB_A_SB_016` | 21,230 | 22,130 | 46,924 | ~36,994 frames (~617 s) |
| 700 | WB (straight) | `BUS_R4_WB_A_SB_020` | 22,117 | 23,017 | 46,924 | ~36,107 frames (~602 s) |

Both requests are **~16,700 frames past their own `expires_at_frame`** and still show `state: "PRIORITY_ACTIVE"`. This confirms `expires_at_frame` is only enforced while a request is queued/waiting (producing the `REQUEST_TIMEOUT` denials seen below) — once a request transitions into `PRIORITY_ACTIVE`, `signal_controller.py`'s `_priority_update()` (around [signal_controller.py:1470](signal_controller.py#L1470)) holds green **until the bus's front bumper physically passes the stop line** (`is_front_bumper_upstream`), with no elapsed-time ceiling. If that bus is itself stalled — e.g. blocked by queued traffic downstream — the signal simply never releases.

### 2.2 Starved approach

Route `R1_EB_A_NB` at node 300 has produced 29 sequential priority attempts since frame 37,160, none successful:

- Attempts alternate `DENIED` (`REQUEST_TIMEOUT`, ~901-frame wait) and `CANCELLED` (`FEATURE_DISABLED`, ~900-frame wait), roughly every ~1,900 frames — three buses (`_016`, `_019`, `_021`, `_024`) retrying in lockstep.
- 3 buses (`BUS_R1_EB_A_NB_019/021/024`) are currently re-queued at attempt 26–29 with `wait_frames: 364` and climbing.
- `queues_passengers_est` in the last valid LLM turn (turn 64) shows `EB: 80` passengers queued, static across turns 53–64 — throughput on this approach has been at zero for the whole window.

The `FEATURE_DISABLED` cancellations indicate the priority *feature* for `R1_EB_A_NB` was being toggled off mid-cycle even while the LLM was nominally granting it `tsp: true` — consistent with the controller repeatedly trying and failing to interrupt the already-active WB hold at node 300.

### 2.3 AI controller failure (compounding, not root cause)

`agent_turn_log.jsonl` turns 53–64: the LLM (`llama3.1:8b`) granted `tsp: true` to `R1_EB_A_NB` and `R4_WB_A_SB` on **every single turn**, always with `R4_WB_A_SB` flagged `LOCKED_ROUTES` and `priority=GRANTED - do not change` — i.e. the controller itself reports it is not permitted to revoke the WB hold that is causing the starvation, and kept re-approving the EB route that can never actually acquire it.

At turn 65 (`timestamp 1788727957.18`), `status` flips to `HELD_ALL_OFF` with an empty `raw_output` — the model call failed to return parseable JSON. `agent_rejects.log` shows a long history of backend failures (`Ollama request exceeded 0.02s/45s timeout`, `Gemini cloud unavailable`) that pre-date this run. `guard.py:143` (`safe_decision`) correctly failed safe — all TSP/DBL flags forced to `false` — per its "malformed output ⇒ all-off airlock" design ([guard.py:156-160](guard.py#L156-L160)). `ai_control.json` confirms the loop is now `"armed": false`, `decision.json` confirms turn 67 is still `HELD_ALL_OFF`. The simulation is also paused, so no further frames are advancing to let anything recover on its own.

### 2.4 Causal chain

```text
WB bus (R4_WB_A_SB) granted PRIORITY_ACTIVE at node 300/700
        |
        v
Bus does not clear the stop line (stalled / blocked downstream)
        |
        v
priority_state has no time-based ceiling once ACTIVE — only
"front bumper has passed" advances it (signal_controller.py:1470)
        |
        v
WB stays green indefinitely; EB/NB/SB signals locked RED
        |
        v
R1_EB_A_NB requests time out every ~1,900 frames, forever
(REQUEST_TIMEOUT / FEATURE_DISABLED), 0 pax/min on EB
        |
        v
LOCKED_ROUTES=R4_WB_A_SB tells the LLM it cannot revoke the hold,
so it re-grants the same deadlocked pair every turn
        |
        v
Ollama backend degrades -> guard.py trips HELD_ALL_OFF -> ai_control
disarmed -> no controller is now attempting recovery at all
```

## 3. Countermeasures

### 3.1 Immediate — recover the running simulation (control panel, no code change)

1. **Run GRIDLOCK DISCHARGE now.** Open the control panel's recovery section, leave the mode at `Auto (Recommended)`, and press **START DISCHARGE**. This is the mechanism built for exactly this state (`control_panel.py:555`, `signal_controller.py:505 _start_discharge`) — it force-cancels stuck priority (`_cancel_priority_for_discharge`) and sequences an empty-and-drain cycle through both nodes rather than waiting on the stalled bus.
2. **Unpause after discharge starts**, not before — starting discharge while paused just arms it; the simulation needs to run frames for `_update_discharge` to progress.
3. **Do not re-arm the LLM controller yet.** With `LOCKED_ROUTES=R4_WB_A_SB` baked into its last-seen state and the Ollama backend still unreliable (per `agent_rejects.log`), re-arming immediately will likely re-request the same WB priority and fight the discharge. Re-arm only after discharge reports `discharge_active: false` again and queues are draining.
4. **Verify the Ollama backend before re-arming.** The timeout errors in `agent_rejects.log` predate this gridlock; confirm `ollama serve` / the configured model endpoint is responsive, or switch the active model in the control panel, before turning `RUN LLM` back on — otherwise it will trip `HELD_ALL_OFF` again on the next turn.

### 3.2 Root-cause fix — close the no-timeout gap (code change, for follow-up)

1. **Give `PRIORITY_ACTIVE` a hard ceiling independent of vehicle position.** In `signal_controller.py` around [signal_controller.py:1470](signal_controller.py#L1470), track elapsed frames since the request entered `PRIORITY_ACTIVE` and force a transition to `PRIORITY_CLEARING`/`RECOVERY_ALL_RED` (with a `DENIED`/`TIMED_OUT` reason) if it exceeds a max-hold threshold, regardless of `is_front_bumper_upstream`. This is the same class of gap flagged in the 2026-09-05 audit for the discharge `WAITING` state — it needs to be fixed at both call sites, not just discharge.
2. **Detect and report stalled priority holders.** If a bus holding `PRIORITY_ACTIVE` hasn't advanced its position in N frames, that's itself an anomaly worth surfacing in telemetry/dashboard (e.g. a `stalled_priority_holder` flag) so it's visible before it accumulates 30,000+ frames of starvation.
3. **Stop treating `LOCKED_ROUTES` as unconditionally sticky.** The controller told the LLM `R4_WB_A_SB` was locked and "do not change" every turn even as it starved another route for 10+ minutes. A lock that can starve a competing approach indefinitely needs its own expiry or an override path (e.g. auto-discharge should always be able to break a lock; right now it can, but nothing triggered it automatically).
4. **Make `guard.py`'s `HELD_ALL_OFF` self-healing.** Right now a bad LLM response permanently zeroes priority until a human notices and re-arms. Consider an automatic fallback to a secondary model or a bounded retry before surfacing `HELD_ALL_OFF`, since a silent all-off state during an active gridlock removes the one automated actor that could otherwise help clear it.

## 4. Suggested priority order

1. Start Auto discharge now, unpause, let it drain.
2. Confirm the Ollama/model backend is healthy.
3. Re-arm the LLM controller only after discharge completes and queues are visibly falling.
4. File the code-level timeout fix (§3.2.1) — this is the second time (2026-09-05 discharge `WAITING`, now `PRIORITY_ACTIVE`) the same "no ceiling on a held state" pattern has produced a live gridlock.
