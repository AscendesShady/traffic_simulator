# Traffic Simulator Callback and Placeholder Audit

**Audit date:** 2026-09-03
**Project:** `C:\Users\ascen\python_projects\traffic_simulator`
**Scope:** Every Tkinter/Pygame callback registration and every intentional UI
placeholder in the current checkout.

Companion documents:

- [Simulator Guide and Source Documentation](TRAFFIC_SIMULATOR_GUIDE_AND_DOCUMENTATION.md)
- [Audit and Step-by-Step Fix Report](TRAFFIC_SIMULATOR_AUDIT_AND_STEP_BY_STEP_FIX_REPORT.md)
- [Gridlock Incident Report](TRAFFIC_SIMULATOR_GRIDLOCK_INCIDENT_REPORT.md)

## 0. Method

This audit re-derived findings from the current source rather than trusting
the prior reports' claims at face value. It:

1. Read `README.md`, the audit/fix report, and the gridlock incident report
   first, specifically to identify configurations that are **intentional**
   before treating anything as a defect. The prior audit report's stated
   documentation-authority order (current source and tests, then the guide,
   then the reports) was followed.
2. Read `control_panel.py`, `main.py`, and the polling/close logic in
   `telemetry_dashboard.py` in full, since those three files own the
   project's entire callback graph.
3. Grepped all seven production modules for `TODO`, `FIXME`, `placeholder`,
   `stub`, and bare `pass` bodies.
4. Ran the compilation check and the full pytest suite fresh against the
   current working tree.

## 1. Executive verdict

No missing, misregistered, or unintentionally dead callback was found. The
two AI/LLM controls are placeholders exactly as documented, and a committed
AST-based test (`tests/test_runtime_and_telemetry.py::test_llm_callbacks_remain_placeholders`)
already prevents them from silently gaining behavior. No undocumented
placeholder, stub, or `TODO`/`FIXME` marker exists anywhere in the seven
production modules — the only source hits for those keywords are in
`tests/generate_teaching_guide.py` and `tests/test_runtime_and_telemetry.py`,
and both are the intentional LLM-placeholder documentation/test, not
unfinished work.

Verification commands run fresh for this audit:

```text
py_compile canvas_gemini.py control_panel.py main.py signal_controller.py
           telemetry_dashboard.py telemetry_exporter.py vehicle.py
Result: PASS

pytest -q --basetemp=./runtime/pytest-temp
Result: 119 passed
```

(The default temp-directory run fails three tests with `PermissionError:
Access is denied` against `C:\Users\ascen\AppData\Local\Temp\pytest-of-ascen`,
which is a Windows temp-directory ACL issue on this machine, not a test or
callback defect. This is the exact workaround README.md already documents;
using `--basetemp=./runtime/pytest-temp` clears it.)

Two hardening gaps flagged in the prior audit (Part II, Section II.8) remain
open in the current source. They are re-confirmed below rather than newly
discovered.

## 2. Confirmed-intentional configurations (not defects)

These are called out first because they read as bugs on a naive pass and the
project documentation explicitly says otherwise.

| Configuration | Where | Why it looks suspicious | Why it is intentional |
|---|---|---|---|
| `on_run_llm()` body is a single `pass` | `control_panel.py:502-504` | A button wired to a function that does nothing looks like dead/unfinished code | README and the audit/fix report both state the LLM controls must remain a no-op; `test_llm_callbacks_remain_placeholders` asserts the function body is exactly `[Pass]` |
| `on_llm_engine_selected()` only updates a label | `control_panel.py:496-498` | A combobox handler that doesn't store the selection anywhere looks incomplete | Same test asserts via AST that this function contains **no** `Assign` nodes — it is required to be display-only and must never write to any config dict |
| `controller.phase` / `controller.timer` setters broadcast one value to both nodes | `signal_controller.py` (legacy compatibility properties) | Two independently-stated per-node state objects sharing a setter looks like the shared-clock bug the project once had | README's "Signal-controller architecture" section and the audit report's canonical verdict both state this is a deliberate legacy setup path, not shared production storage; per-node updates during normal operation never go through it |
| `main.cleanup()` swallows all exceptions from `dashboard_proc.terminate()` | `main.py:388-393` | A bare `except Exception: pass` around a subprocess call looks like an unintended silent failure | This one is **not** fully intentional — see Section 4.1. It is a known, already-documented limitation, not something this audit is newly flagging as hidden. |

## 3. Callback inventory

### 3.1 `control_panel.py` — verified complete and correctly bound

Every control listed in the audit/fix report's "Callback requirements"
section was located and confirmed wired in the current source:

| Control | Registration | Verified behavior |
|---|---|---|
| Window close | `root.protocol("WM_DELETE_WINDOW", on_close)` (line 176) | Sets `is_running=False`, destroys the window, exits |
| Pause/Resume | `pause_btn.config(command=toggle_pause)` | Flips `global_config["is_paused"]` and updates button/status labels |
| Reset | `reset_btn.config(command=trigger_reset)` | Sets `reset_triggered=True`, consumed once by `main.py`'s loop |
| Green-time slider | `command=update_green_time` | Writes `global_config["green_time"]` |
| Speed slider | `command=update_speed` | Writes `global_config["sim_speed"]` |
| Discharge mode combobox | `discharge_mode_box.bind("<<ComboboxSelected>>", on_discharge_mode_selected)` | Writes `global_config["discharge_selection"]` |
| Start Discharge | `command=start_discharge` | Sets discharge request flags and seeds `discharge_runtime` |
| Safe Stop | `command=safe_stop_discharge` | Sets `discharge_stop_requested` |
| Discharge status refresh | `root.after(250, refresh_discharge_status)`, rescheduled at the end of its own body | Self-perpetuating poll; reads `discharge_runtime` and updates three labels |
| LLM engine selector | `llm_engine_box.bind(...)` | UI-only, see Section 2 |
| RUN LLM | `command=on_run_llm` | No-op, see Section 2 |
| Manual bus dispatch | `manual_btn.config(command=trigger_manual_dispatch)` | Sets `manual_dispatch=True` on the selected route only |
| 6× route activation toggle | `t_btn.config(command=make_route_toggle(r_id, t_btn))` | Factory closes over `r_id` by parameter — no late-binding risk |
| 6× headway slider | `command=make_hw_slider(r_id, hw_val_lbl)` | Same factory pattern |
| 6× TSP toggle | `command=make_tsp_toggle(r_id, tsp_btn)` | Same factory pattern |
| 6× DBL toggle | `command=make_dbl_toggle(r_id, dbl_btn)` | Same factory pattern |
| 6× source activation toggle | `command=make_toggle(key, t_btn, dot)` | Same factory pattern |
| 6× generation-model selector | `model_box.bind("<<ComboboxSelected>>", make_model_change(key, model_box))` | Same factory pattern |
| 6× inflow-rate slider | `command=make_rate_slider(key, rate_val)` | Same factory pattern |
| 6× straight/left split slider | `command=make_split_slider(key, split_val)` | Same factory pattern |
| 6× heavy-vehicle slider | `command=make_heavy_slider(key, heavy_val)` | Same factory pattern |

All 26 per-route/per-approach controls use a `make_X(key, ...)` closure
factory that binds `key` as a parameter rather than capturing the loop
variable directly. This is the correct pattern — a direct closure over a
`for` loop's variable in Tkinter is a classic late-binding bug (every button
would end up controlling the last route/approach in the loop). No instance
of the unsafe pattern exists in this file.

### 3.2 `main.py` — runtime callbacks verified

| Callback | Location | Verified behavior |
|---|---|---|
| Initial `simulation_step()` scheduling | `root.after(16, simulation_step)` (line 519) | Starts the ~60 Hz Tk-driven loop |
| Recurring `simulation_step()` scheduling | `root.after(16, simulation_step)` at the end of the function body (line 517) | Self-perpetuating; not conditioned on success, so a mid-frame exception would stop scheduling — no such exception path was found in this pass |
| Pygame close | `for event in pygame.event.get(): if event.type == pygame.QUIT: ...` (lines 418-424) | Sets `is_running=False`, destroys the Tk root, exits |
| `atexit` dashboard cleanup | `atexit.register(cleanup)` (line 393) | Terminates the dashboard subprocess |
| Reset consumption | `if control_panel.global_config["reset_triggered"]:` (line 426) | Clears vehicles/spawner/discharge state, then resets the flag in the same block — consumed exactly once |
| Pause gate | `if not is_paused: time_accumulator += ...` / `else: time_accumulator = 0.0` | Confirmed: pausing prevents physics accumulation and does not build a catch-up backlog on resume |
| Catch-up bound | `while ... steps_this_callback < max_steps_per_callback` (`max_steps_per_callback = 6`) | Matches the documented A-05 fix; excess `time_accumulator` is discarded, not replayed |

### 3.3 `telemetry_dashboard.py` — polling callback verified

| Callback | Location | Verified behavior |
|---|---|---|
| Initial poll | `self.poll_telemetry()` in `__init__` (line 53) | Starts polling immediately on window construction |
| Recurring poll | `self.root.after(250, self.poll_telemetry)` inside a `finally:` block (lines 796-797) | Reschedules unconditionally, including after a caught read/schema/UI exception — matches the documented F-09 fix |
| Mouse wheel scrolling | `root.bind("<MouseWheel>"/"<Button-4>"/"<Button-5>", self.on_mousewheel, add="+")` | Present for both scroll directions on both tabs |
| Metric card population | `for row in metrics_layout: for title, key in row: ...` (lines 148-174) | Stores widgets in `self.vars[key]`, a dict keyed by string — not a closure over the loop variable, so no late-binding risk despite the nested loop |

No `WM_DELETE_WINDOW` override exists for the dashboard window; closing it
uses Tk's default destroy behavior. This is not a defect — the dashboard is
a read-only, in-memory-only child process with no shared resource to release
on close (README already documents that closing it simply clears its
session-only history).

## 4. Open items (carried forward, independently re-confirmed)

These were already identified as hardening recommendations in the prior
Part II audit. This pass re-checked the current source and confirms both are
still true — they are not new findings, but they are still open and worth
keeping on record since the user asked specifically about callback/placeholder
health.

### 4.1 `main.cleanup()` does not confirm dashboard-process exit

```python
def cleanup():
    try:
        dashboard_proc.terminate()
    except Exception:
        pass
atexit.register(cleanup)
```

`terminate()` sends a termination signal but the function returns
immediately without `wait()`. Combined with the bare `except Exception: pass`,
a failure to terminate the child is silent. This matches Section II.8, item 4
of the prior audit report verbatim; it has not been addressed since. It is
low severity — the process is a passive telemetry viewer, not something that
can corrupt shared state — but it can leave an orphaned `python.exe` process
if `terminate()` itself raises or the child ignores the signal.

### 4.2 The full callback fake-widget harness is still not committed

The prior audit's Section II.6 reports a 62-callback mocked-Tkinter harness
that exercised every button/scale/combobox with zero exceptions, and
explicitly recommends committing it as a regression test. The current
`tests/` directory (`test_adversarial_simulation.py`,
`test_network_discharge.py`, `test_route_completion.py`,
`test_runtime_and_telemetry.py`, `test_signal_priority.py`,
`test_vehicle_safety.py`, plus `helpers.py`/`conftest.py`) still has no file
matching that description. The narrower
`test_llm_callbacks_remain_placeholders` test does guard the placeholder
contract specifically (Section 3 above), but nothing in the committed suite
would catch a newly introduced late-binding closure bug or an unregistered
callback across the full widget set automatically. This gap is worth closing
given how much of this file's correctness depends on the `make_X(key, ...)`
pattern being applied consistently every time a new per-route/per-approach
control is added.

## 5. Summary

| Area | Status |
|---|---|
| LLM selector / RUN LLM placeholders | Confirmed inert, AST-tested, matches documentation |
| `controller.phase`/`controller.timer` broadcast setters | Confirmed intentional legacy compatibility path, not shared production state |
| All 26 per-route/per-approach factory callbacks | Correctly parameterized, no late-binding bugs found |
| All fixed (non-factory) control-panel callbacks | Registered and behave as documented |
| `main.py` scheduling, pause gate, reset consumption, catch-up bound | Verified against current source, matches documentation |
| `telemetry_dashboard.py` polling reschedule | Confirmed inside `finally`, matches documented F-09 fix |
| Undocumented placeholders/stubs/TODOs in production code | None found |
| `main.cleanup()` child-process confirmation | Still open (Section 4.1) |
| Committed 62-callback harness | Still open (Section 4.2) |
| Compilation | PASS |
| Full test suite | 119 passed |
