"""The GPU poll must never block the Tk event loop.

poll_gpu_stats() shells out to nvidia-smi with a 2-second timeout. Once the
three windows merge into one, that same event loop also drives the
simulation and pygame canvas, so a synchronous call there would freeze
everything, not just one dashboard process. A daemon thread refreshes a
cached dict on its own clock; the Tk callback only ever reads the cache.
"""
import time

import telemetry_dashboard as dashboard_module


def test_read_cached_gpu_stats_never_calls_subprocess(monkeypatch):
    """Reading the cache must not itself shell out, so it can never block."""
    calls = []
    monkeypatch.setattr(
        dashboard_module.subprocess, "run",
        lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(RuntimeError()),
    )

    result = dashboard_module.read_cached_gpu_stats()

    assert calls == []
    assert isinstance(result, dict)
    assert set(result) == set(dashboard_module._gpu_none())


def test_background_thread_refreshes_the_cache(monkeypatch):
    """The worker thread, not the Tk callback, is what updates the cache."""
    samples = iter(
        [
            {"vram_used_mb": 1000.0, "vram_total_mb": 8192.0,
             "gpu_util_pct": 10.0, "power_w": 50.0, "temp_c": 40.0},
            {"vram_used_mb": 2000.0, "vram_total_mb": 8192.0,
             "gpu_util_pct": 20.0, "power_w": 60.0, "temp_c": 45.0},
        ]
    )
    settled = {"vram_used_mb": 2000.0, "vram_total_mb": 8192.0,
               "gpu_util_pct": 20.0, "power_w": 60.0, "temp_c": 45.0}
    monkeypatch.setattr(
        dashboard_module, "poll_gpu_stats", lambda: next(samples, settled)
    )
    monkeypatch.setattr(dashboard_module, "GPU_POLL_INTERVAL_SECONDS", 0.01)
    # Force a fresh thread even if an earlier test left one running.
    monkeypatch.setattr(dashboard_module, "_gpu_poll_thread", None)

    thread = dashboard_module.start_gpu_poll_thread()
    try:
        deadline = time.monotonic() + 2.0
        seen_vram = set()
        while time.monotonic() < deadline and len(seen_vram) < 2:
            seen_vram.add(
                dashboard_module.read_cached_gpu_stats()["vram_used_mb"]
            )
            time.sleep(0.01)
        assert {1000.0, 2000.0} <= seen_vram
    finally:
        thread.stop_event.set()
        thread.join(timeout=1.0)


def test_start_gpu_poll_thread_is_idempotent():
    """Calling it twice (e.g. two dashboards) must not spawn a second thread."""
    first = dashboard_module.start_gpu_poll_thread()
    second = dashboard_module.start_gpu_poll_thread()

    assert first is second
    assert first.daemon is True


def test_poll_llm_performance_reads_the_cache_not_the_blocking_call(
    monkeypatch,
):
    """The Tk-facing poll must go through the cache, never the raw call."""
    blocking_calls = []
    monkeypatch.setattr(
        dashboard_module,
        "poll_gpu_stats",
        lambda: blocking_calls.append(1) or dashboard_module._gpu_none(),
    )
    cached = {
        "vram_used_mb": 4242.0, "vram_total_mb": 8192.0,
        "gpu_util_pct": 55.0, "power_w": 99.0, "temp_c": 61.0,
    }
    monkeypatch.setattr(
        dashboard_module, "read_cached_gpu_stats", lambda: dict(cached)
    )

    class FakeRoot:
        def after(self, _delay, _callback):
            pass

    dashboard = dashboard_module.TelemetryDashboard.__new__(
        dashboard_module.TelemetryDashboard
    )
    dashboard.root = FakeRoot()
    dashboard.initialize_llm_monitor_state()
    dashboard.safe_read_ai_control = lambda: {"armed": False, "model": "None"}
    dashboard.safe_read_agent_turns = lambda: ([], None)
    dashboard.update_llm_performance_display = lambda: None

    dashboard.poll_llm_performance()

    assert blocking_calls == []
    assert dashboard.latest_gpu == cached
