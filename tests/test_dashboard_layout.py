"""Dense summary layout: the dashboard must stop claiming unused screen.

The summary tab was redesigned into a single KPI strip plus a recovery panel
that only expands when it has something to say, so the whole tab fits in a
430px window instead of 780px (where the intersection panel was clipped).
"""
import inspect
from types import SimpleNamespace

import telemetry_dashboard as telemetry_dashboard_module
from telemetry_dashboard import TelemetryDashboard


class FakeWidget:
    """Records pack state the way tk does, without needing a display."""

    def __init__(self, name="widget"):
        self.name = name
        self.packed = False
        self.config_calls = []

    def pack(self, **kwargs):
        self.packed = True

    def pack_forget(self):
        self.packed = False

    def pack_configure(self, **kwargs):
        # tk re-packs a forgotten widget on pack_configure; mirror that so a
        # regression in the responsive profile is visible here.
        self.packed = True

    def config(self, **kwargs):
        self.config_calls.append(kwargs)


def collapsible_dashboard():
    dashboard = TelemetryDashboard.__new__(TelemetryDashboard)
    dashboard.recovery_card = FakeWidget("card")
    dashboard.discharge_compact_lbl = FakeWidget("compact")
    dashboard.recovery_title_lbl = FakeWidget("title")
    dashboard.discharge_selected_lbl = FakeWidget("selected")
    dashboard.discharge_status_lbl = FakeWidget("status")
    dashboard.discharge_reason_lbl = FakeWidget("reason")
    dashboard.discharge_recommendation_lbl = FakeWidget("recommendation")
    dashboard.discharge_detail_widgets = [
        dashboard.recovery_title_lbl,
        dashboard.discharge_selected_lbl,
        dashboard.discharge_status_lbl,
        dashboard.discharge_reason_lbl,
        dashboard.discharge_recommendation_lbl,
    ]
    dashboard.discharge_expanded = True
    return dashboard


def test_summary_fits_a_short_window():
    """The dashboard must not reserve height the summary no longer needs."""
    assert telemetry_dashboard_module.WINDOW_DEFAULT_HEIGHT == 430
    assert TelemetryDashboard.initial_window_size(1920, 1080) == (900, 430)
    # A small laptop still gets a usable window rather than a clipped one.
    assert TelemetryDashboard.initial_window_size(640, 480) == (560, 360)


def test_kpi_grid_carries_every_summary_metric():
    """Every value update_metrics writes must have a card in the grid."""
    keys = {key for _label, key in telemetry_dashboard_module.SUMMARY_KPIS}
    assert keys == {
        "vehicles",
        "buses",
        "passengers",
        "queued",
        "delay",
        "congestion",
        "tsp",
        "dbl",
        "timer",
    }
    # Portrait layout: two cards per row, full titles rather than
    # abbreviations since each card has its own line for the title.
    assert telemetry_dashboard_module.SUMMARY_KPI_COLUMNS == 2
    source = inspect.getsource(TelemetryDashboard.build_ui)
    assert "divmod(index, SUMMARY_KPI_COLUMNS)" in source


def test_every_summary_abbreviation_has_hover_help():
    keys = {key for _label, key in telemetry_dashboard_module.SUMMARY_KPIS}

    assert set(telemetry_dashboard_module.KPI_TOOLTIPS) == keys
    assert "Transit Signal Priority" in telemetry_dashboard_module.KPI_TOOLTIPS["tsp"]
    assert "Dynamic Bus Lane" in telemetry_dashboard_module.KPI_TOOLTIPS["dbl"]
    assert "Queued road vehicles" in telemetry_dashboard_module.KPI_TOOLTIPS["queued"]
    for abbreviation, key in telemetry_dashboard_module.SUMMARY_KPIS:
        assert not telemetry_dashboard_module.KPI_TOOLTIPS[key].startswith(
            f"{abbreviation} —"
        )


def test_summary_build_attaches_one_tooltip_per_kpi():
    source = inspect.getsource(TelemetryDashboard.build_ui)

    assert "self.metric_tooltips = []" in source
    assert "HoverTooltip(cell, KPI_TOOLTIPS[key])" in source
    assert "tooltip.add_target(value)" in source
    assert "tooltip.add_target(title_label)" in source


def test_gridlock_panel_collapsed_while_idle():
    dashboard = collapsible_dashboard()

    dashboard.set_discharge_expanded(False)

    assert dashboard.discharge_expanded is False
    assert dashboard.discharge_compact_lbl.packed is True
    assert all(
        not widget.packed for widget in dashboard.discharge_detail_widgets
    )


def test_gridlock_panel_expands_when_recovery_is_working():
    """Reason and recommendation must reappear exactly when they matter."""
    dashboard = collapsible_dashboard()
    dashboard.set_discharge_expanded(False)

    dashboard.set_discharge_expanded(True)

    assert dashboard.discharge_expanded is True
    assert dashboard.discharge_compact_lbl.packed is False
    assert all(widget.packed for widget in dashboard.discharge_detail_widgets)

    # And it collapses again once recovery finishes.
    dashboard.set_discharge_expanded(False)
    assert dashboard.discharge_compact_lbl.packed is True
    assert dashboard.discharge_reason_lbl.packed is False


def test_quiet_states_cover_the_normal_operating_cases():
    quiet = TelemetryDashboard.DISCHARGE_QUIET_STATES
    for state in ("IDLE", "INACTIVE", "COMPLETED", ""):
        assert state in quiet
    # Anything that needs operator attention must force the panel open.
    for state in ("DISCHARGING", "WAITING", "RECOVERY_FAILED", "REQUESTED"):
        assert state not in quiet


def test_height_heavy_tabs_use_vertical_overflow_containers():
    """Trends and LLM remain reachable when the dashboard is made short."""
    source = inspect.getsource(TelemetryDashboard.build_ui)

    assert "self.trends_scroll_canvas" in source
    assert "self.llm_scroll_canvas" in source
    assert source.count("self.create_overflow_tab()") == 3
    # The already-dense summary remains responsive without gaining a scrollbar.
    assert "self.summary_tab, self.summary_content = self.create_responsive_tab()" in source
    overflow = inspect.getsource(TelemetryDashboard.create_overflow_tab)
    assert "self.create_scrollable_tab()" in overflow
    assert "self.embedded" in overflow


def test_embedded_dashboard_never_nests_a_scrollbar():
    """Mounted in a MainWindow pane the pane scrolls, so tabs must not add
    a second scroll canvas inside it; standalone they still scroll."""
    import tkinter as tk
    from tkinter import ttk

    def scrollbars_under(widget):
        found = [c for c in widget.winfo_children() if isinstance(c, ttk.Scrollbar)]
        for child in widget.winfo_children():
            found += scrollbars_under(child)
        return found

    host = tk.Tk()
    try:
        embedded = TelemetryDashboard(tk.Frame(host))
        assert embedded.embedded is True
        assert embedded.trends_scroll_canvas is None
        assert embedded.llm_scroll_canvas is None
        assert scrollbars_under(embedded.notebook) == []

        # A Toplevel is a window of its own, so the dashboard owns it the
        # same way it owns a standalone tk.Tk().
        standalone = TelemetryDashboard(tk.Toplevel(host))
        assert standalone.embedded is False
        assert isinstance(standalone.trends_scroll_canvas, tk.Canvas)
        assert len(scrollbars_under(standalone.notebook)) == 3
    finally:
        host.destroy()


def test_llm_metric_grids_reflow_to_two_columns_in_a_portrait_column():
    import tkinter as tk

    host = tk.Tk()
    try:
        dashboard = TelemetryDashboard(tk.Frame(host))
        dashboard.apply_responsive_layout(width=440, height=1200)
        assert {s["active_columns"] for s in dashboard.llm_sections} == {2}
        for section in dashboard.llm_sections:
            for card in section["cards"]:
                assert int(card.grid_info()["column"]) < 2
        dashboard.apply_responsive_layout(width=1100, height=800)
        assert [s["active_columns"] for s in dashboard.llm_sections] == [
            s["columns"] for s in dashboard.llm_sections
        ]
    finally:
        host.destroy()


def test_scrollable_tab_tracks_width_and_vertical_overflow():
    source = inspect.getsource(TelemetryDashboard.create_scrollable_tab)

    assert 'orient="vertical"' in source
    assert "yscrollcommand=scrollbar.set" in source
    assert 'scrollregion=canvas.bbox("all")' in source
    assert "width=max(1, event.width)" in source


def test_mouse_wheel_scrolls_both_directions():
    class FakeCanvas:
        def __init__(self):
            self.calls = []

        def yview_scroll(self, amount, unit):
            self.calls.append((amount, unit))

    canvas = FakeCanvas()

    assert TelemetryDashboard.scroll_tab_with_wheel(
        SimpleNamespace(delta=-120, num=None), canvas
    ) == "break"
    assert TelemetryDashboard.scroll_tab_with_wheel(
        SimpleNamespace(delta=120, num=None), canvas
    ) == "break"
    assert canvas.calls == [(3, "units"), (-3, "units")]
