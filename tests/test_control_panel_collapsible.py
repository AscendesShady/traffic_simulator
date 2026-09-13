"""UI-only regression tests for the control-panel disclosure sections."""

import inspect

import pytest

import control_panel


class FakeButton:
    def __init__(self):
        self.options = {}

    def config(self, **kwargs):
        self.options.update(kwargs)


class FakeBody:
    def __init__(self):
        self.visible = False
        self.pack_options = None

    def pack(self, **kwargs):
        self.visible = True
        self.pack_options = kwargs

    def pack_forget(self):
        self.visible = False


class FakeScale:
    def __init__(self, value=5.0, lower=0.0, upper=10.0):
        self.value = value
        self.options = {"from": lower, "to": upper}
        self.bindings = {}
        self.focused = False

    def configure(self, **kwargs):
        self.options.update(kwargs)

    def cget(self, key):
        return self.options[key]

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

    def bind(self, sequence, callback, add=None):
        self.bindings[sequence] = (callback, add)

    def focus_set(self):
        self.focused = True


def test_disclosure_state_hides_and_restores_same_body():
    button = FakeButton()
    body = FakeBody()

    assert control_panel.set_disclosure_state(
        button, body, "Section", False
    ) is False
    assert body.visible is False
    assert button.options["text"] == "▶  Section"

    assert control_panel.set_disclosure_state(
        button, body, "Section", True, {"fill": "x", "padx": 4}
    ) is True
    assert body.visible is True
    assert body.pack_options == {"fill": "x", "padx": 4}
    assert button.options["text"] == "▼  Section"


EXPECTED_SECTIONS = {
    # title: expanded at launch
    "Run Controls": True,
    "Benchmark Test": True,
    "Tuning": False,
    "Gridlock Discharge": False,
    "AI / LLM Control": True,
    "Bus Routes (TSP / DBL)": False,
    "Approach Traffic": False,
}


def test_every_control_panel_section_is_collapsible():
    """Each card is a disclosure section built by make_section: the long
    editors launch collapsed, the run/benchmark/AI cards launch open, and
    toggling one never touches another."""
    import tkinter as tk

    host = tk.Tk()
    try:
        control_panel.create_dashboard_window(tk.Frame(host))
        sections = control_panel.control_panel_sections
        assert {title: s["state"]["expanded"] for title, s in sections.items()} == (
            EXPECTED_SECTIONS
        )
        host.update_idletasks()
        for title, section in sections.items():
            assert bool(section["body"].winfo_manager()) is EXPECTED_SECTIONS[title]
            arrow = "\u25bc" if EXPECTED_SECTIONS[title] else "\u25b6"
            assert section["header"].cget("text") == f"{arrow}  {title}"

        tuning = sections["Tuning"]
        tuning["toggle"]()
        host.update_idletasks()
        assert tuning["state"]["expanded"] is True
        assert tuning["body"].winfo_manager() == "pack"
        # Independent: nothing else changed state.
        for title, section in sections.items():
            if title != "Tuning":
                assert section["state"]["expanded"] is EXPECTED_SECTIONS[title]
        tuning["toggle"]()
        host.update_idletasks()
        assert tuning["state"]["expanded"] is False
        assert not tuning["body"].winfo_manager()
    finally:
        host.destroy()


def test_section_headers_share_one_type_scale():
    """Three sizes only (title / section / body), so nothing is a one-off."""
    import tkinter as tk
    from tkinter import ttk

    host = tk.Tk()
    try:
        pane = tk.Frame(host)
        control_panel.create_dashboard_window(pane)
        sizes = set()

        def walk(widget):
            for child in widget.winfo_children():
                # ttk widgets (comboboxes, scales) carry their font through
                # the style; the classic widgets are what this checks.
                if isinstance(child, (tk.Label, tk.Button, tk.Entry)) and not isinstance(
                    child, ttk.Widget
                ):
                    parts = host.tk.splitlist(child.cget("font"))
                    sizes.add(int(parts[1]))
                walk(child)

        walk(pane)
        assert sizes == {
            control_panel.FONT_TITLE[1],
            control_panel.FONT_SECTION[1],
            control_panel.FONT_BODY[1],
        }
        for section in control_panel.control_panel_sections.values():
            parts = host.tk.splitlist(section["header"].cget("font"))
            assert int(parts[1]) == control_panel.FONT_SECTION[1]
    finally:
        host.destroy()


def test_disclosure_height_stays_on_screen():
    fit = control_panel.bounded_control_panel_height

    assert fit(700, 1080, 10) == 700
    assert fit(1400, 1080, 10) == 1030
    assert fit(100, 1080, 10) == control_panel.CONTROL_PANEL_MIN_HEIGHT


def test_benchmark_countdown_reserves_longest_runtime_text():
    source = inspect.getsource(control_panel.create_dashboard_window)

    assert control_panel.BENCHMARK_COUNTDOWN_WIDTH >= len("00:00 PAUSED")
    assert "width=BENCHMARK_COUNTDOWN_WIDTH" in source
    assert 'anchor="e"' in source


def test_scale_click_focuses_and_arrows_make_exact_clamped_steps():
    scale = FakeScale(value=5.0, lower=2.0, upper=6.0)

    assert control_panel.enable_scale_keyboard(scale, step=0.1) is scale
    assert scale.options["takefocus"] is True
    assert all(add == "+" for _callback, add in scale.bindings.values())

    scale.bindings["<Button-1>"][0]()
    assert scale.focused is True
    assert scale.bindings["<Right>"][0]() == "break"
    assert scale.value == pytest.approx(5.1)
    assert scale.bindings["<Left>"][0]() == "break"
    assert scale.value == pytest.approx(5.0)

    scale.value = 6.0
    scale.bindings["<Right>"][0]()
    assert scale.value == 6.0
    scale.value = 2.0
    scale.bindings["<Left>"][0]()
    assert scale.value == 2.0


def test_every_control_panel_scale_enables_keyboard_adjustment():
    source = inspect.getsource(control_panel.create_dashboard_window)

    # Eight slider rows cover simulation speed, eligibility, vehicle speed,
    # LLM interval, bus headway, flow, straight %, and trucks %. Signal
    # cycles and green splits are derived by Webster and have no UI slider.
    # add_slider_row is the only slider constructor and always wires
    # enable_scale_keyboard.
    assert source.count("add_slider_row(") == 8
    assert "ttk.Scale(" not in source
    assert "enable_scale_keyboard(" in inspect.getsource(control_panel.add_slider_row)
    for step in ("step=0.05", "step=0.1", "step=1", "step=10"):
        assert step in source


def test_keyboard_steps_snap_to_the_value_grid():
    """A pointer drag leaves a fractional value; the next arrow press lands
    on the grid so labels, config and export agree."""
    scale = FakeScale(value=63.4, lower=0.0, upper=100.0)
    control_panel.enable_scale_keyboard(scale, step=1)

    assert scale.bindings["<Right>"][0]() == "break"
    assert scale.value == 64.0
    scale.bindings["<Left>"][0]()
    scale.bindings["<Down>"][0]()
    assert scale.value == 62.0
    scale.bindings["<Up>"][0]()
    assert scale.value == 63.0
    scale.bindings["<End>"][0]()
    assert scale.value == 100.0
    scale.bindings["<Home>"][0]()
    assert scale.value == 0.0

    assert control_panel.snap_scale_value(0.57, 0.5, 3.0, 0.1) == 0.5
    assert control_panel.snap_scale_value(2.999999, 0.5, 3.0, 0.1) == 2.9
    assert control_panel.snap_scale_value(9.0, 0.5, 3.0, 0.1) == 3.0


def test_zero_keyboard_step_is_rejected():
    with pytest.raises(ValueError):
        control_panel.enable_scale_keyboard(FakeScale(), step=0)
