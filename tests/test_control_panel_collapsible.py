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


def test_control_panel_has_two_independent_collapsed_sections():
    source = inspect.getsource(control_panel.create_dashboard_window)

    assert "toggle_transit_section" in source
    assert "toggle_approaches_section" in source
    assert 'transit_state = {"expanded": False}' in source
    assert 'approaches_state = {"expanded": False}' in source
    assert source.count("set_disclosure_state(") == 2


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

    # Eight construction sites cover simulation speed, eligibility, vehicle
    # speed, LLM interval, bus headway, flow, straight %, and trucks %. Signal
    # cycles and green splits are derived by Webster and have no UI slider.
    assert source.count("enable_scale_keyboard(") == 8
    for step in ("step=0.05", "step=0.1", "step=1", "step=10"):
        assert step in source


def test_zero_keyboard_step_is_rejected():
    with pytest.raises(ValueError):
        control_panel.enable_scale_keyboard(FakeScale(), step=0)
