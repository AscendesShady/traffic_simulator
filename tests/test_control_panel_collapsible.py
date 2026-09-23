"""UI-only regression tests for the control-panel disclosure sections."""

import inspect

import pytest

import src.ui.control_panel as control_panel


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


# Launch order, top to bottom. Every card starts collapsed.
EXPECTED_ORDER = (
    "Approach Traffic",
    "Bus Routes",
    "Single Run",
    "Batch Run",
    "Tuning",
    "Gridlock Discharge",
)
EXPECTED_SECTIONS = {title: False for title in EXPECTED_ORDER}
# Header text colour by tier: configure (white) / run (amber) / intervene (red).
EXPECTED_HEADER_COLOURS = {
    "Approach Traffic": control_panel.COLOR_TEXT_PRIMARY,
    "Bus Routes": control_panel.COLOR_TEXT_PRIMARY,
    "Single Run": control_panel.COLOR_WARNING,
    "Batch Run": control_panel.COLOR_WARNING,
    "Tuning": control_panel.COLOR_DANGER,
    "Gridlock Discharge": control_panel.COLOR_DANGER,
}


def test_every_control_panel_section_is_collapsible():
    """Each card is a disclosure section built by make_section: every card
    launches collapsed, in the documented order, with its tier colour, and
    toggling one never touches another."""
    import tkinter as tk

    host = tk.Tk()
    try:
        control_panel.create_dashboard_window(tk.Frame(host))
        sections = control_panel.control_panel_sections
        assert tuple(sections) == EXPECTED_ORDER == control_panel.SECTION_ORDER
        assert {title: s["state"]["expanded"] for title, s in sections.items()} == (
            EXPECTED_SECTIONS
        )
        host.update_idletasks()
        # Cards are packed top-to-bottom in registry order.
        cards = [section["card"] for section in sections.values()]
        assert cards == [
            card for card in cards[0].master.pack_slaves() if card in cards
        ]
        for title, section in sections.items():
            assert bool(section["body"].winfo_manager()) is EXPECTED_SECTIONS[title]
            arrow = "\u25bc" if EXPECTED_SECTIONS[title] else "\u25b6"
            assert section["header"].cget("text") == f"{arrow}  {title}"
            assert section["header"].cget("fg") == EXPECTED_HEADER_COLOURS[title]

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


def find_buttons(widget, text_fragment):
    import tkinter as tk

    found = []
    for child in widget.winfo_children():
        if isinstance(child, tk.Button) and text_fragment in child.cget("text"):
            found.append(child)
        found.extend(find_buttons(child, text_fragment))
    return found


def test_start_button_reads_calibrating_as_soon_as_it_is_pressed():
    """START's first job is a blocking saturation-flow measurement, so the
    click handler itself must repaint the button (the poller cannot)."""
    import tkinter as tk

    host = tk.Tk()
    config = control_panel.global_config
    saved = {key: config[key] for key in ("is_running", "start_requested")}
    try:
        control_panel.create_dashboard_window(tk.Frame(host))
        run_body = control_panel.control_panel_sections["Single Run"]["body"]
        (start_btn,) = find_buttons(run_body, "Start")
        config["is_running"] = False
        start_btn.invoke()
        assert config["start_requested"] is True
        assert "Calibrating" in start_btn.cget("text")
        assert start_btn.cget("bg") == control_panel.COLOR_WARNING
    finally:
        config.update(saved)
        host.destroy()


def test_merged_run_sections_keep_all_actions_and_scroll_model_picker(monkeypatch):
    """The two new cards retain both workflows and bound a long model list."""
    import tkinter as tk
    from tkinter import ttk

    monkeypatch.setattr(
        control_panel,
        "get_batch_model_choices",
        lambda: [f"model-{index:02d}" for index in range(40)],
    )
    host = tk.Tk()
    mounted = tk.Frame(host)
    try:
        control_panel.create_dashboard_window(mounted)
        single_body = control_panel.control_panel_sections["Single Run"]["body"]
        batch_body = control_panel.control_panel_sections["Batch Run"]["body"]
        assert find_buttons(single_body, "Arm strategy")
        assert find_buttons(single_body, "Start")
        # A single timed run is a batch of one model x one seed, so the card
        # offers only "Run batch" -- there is no separate "Start test".
        assert not find_buttons(batch_body, "Start test")
        assert find_buttons(batch_body, "Run batch")

        (picker_button,) = find_buttons(batch_body, "Select models")
        picker_button.invoke()
        host.update_idletasks()

        def descendants(widget):
            result = []
            for child in widget.winfo_children():
                result.append(child)
                result.extend(descendants(child))
            return result

        widgets = descendants(mounted)
        pickers = [widget for widget in widgets if isinstance(widget, tk.Toplevel)]
        assert len(pickers) == 1
        picker_widgets = descendants(pickers[0])
        assert any(isinstance(widget, tk.Canvas) for widget in picker_widgets)
        assert any(isinstance(widget, ttk.Scrollbar) for widget in picker_widgets)
        assert len(
            [widget for widget in picker_widgets if isinstance(widget, tk.Checkbutton)]
        ) == 40
        pickers[0].destroy()
    finally:
        host.destroy()


def test_discharge_buttons_raise_the_controller_flags():
    """Start discharge / Safe stop set the request flags SignalController
    consumes on its next update, and publish the REQUESTED status the panel
    shows meanwhile."""
    import tkinter as tk

    host = tk.Tk()
    config = control_panel.global_config
    saved = {
        key: config[key]
        for key in ("discharge_start_requested", "discharge_stop_requested")
    }
    saved_runtime = dict(config["discharge_runtime"])
    try:
        control_panel.create_dashboard_window(tk.Frame(host))
        body = control_panel.control_panel_sections["Gridlock Discharge"]["body"]
        (start_btn,) = find_buttons(body, "Start discharge")
        (stop_btn,) = find_buttons(body, "Safe stop")
        host.update_idletasks()
        # Both are mapped: an unmapped button cannot be clicked.
        assert start_btn.winfo_manager() and stop_btn.winfo_manager()

        start_btn.invoke()
        assert config["discharge_start_requested"] is True
        assert config["discharge_stop_requested"] is False
        assert config["discharge_runtime"]["status"] == "REQUESTED"
        assert config["discharge_runtime"]["selected"] == control_panel.DISCHARGE_AUTO

        stop_btn.invoke()
        assert config["discharge_start_requested"] is False
        assert config["discharge_stop_requested"] is True
    finally:
        config.update(saved)
        config["discharge_runtime"] = saved_runtime
        host.destroy()


def widgets_of_type(widget, kind):
    found = []
    for child in widget.winfo_children():
        if isinstance(child, kind):
            found.append(child)
        found.extend(widgets_of_type(child, kind))
    return found


def approach_cards(body):
    """The six bordered item cards, in APPROACH_NAMES order."""
    import tkinter as tk

    return [
        child for child in body.winfo_children()
        if isinstance(child, tk.Frame) and int(child.cget("highlightthickness")) == 1
    ]


def test_compact_approach_cards_keep_every_control_working():
    """Inflow is a 0-60 v/m stepper; turning movements are one split bar
    and Trucks a thin slider; ON/OFF and the model selector still write
    approach_configs; and each card stays compact."""
    import tkinter as tk
    from tkinter import ttk

    host = tk.Tk()
    try:
        pane = tk.Frame(host)
        pane.pack(fill="both", expand=True)   # mapped, so key events deliver
        control_panel.create_dashboard_window(pane)
        section = control_panel.control_panel_sections["Approach Traffic"]
        section["toggle"]()
        host.update()
        cards = approach_cards(section["body"])
        assert len(cards) == len(control_panel.APPROACH_NAMES)
        # 174 px: the split-bar caption stacks value under label so three
        # cells fit the narrowest (256 px) control pane without clipping.
        for card in cards:
            assert card.winfo_reqheight() <= 180

        eb_card = cards[0]
        (rate_box,) = widgets_of_type(eb_card, tk.Spinbox)
        assert (float(rate_box.cget("from")), float(rate_box.cget("to"))) == (0.0, 60.0)
        assert rate_box.get() == str(control_panel.approach_configs["EB"]["rate"])

        def type_rate(text):
            rate_box.focus_force()
            rate_box.delete(0, "end")
            rate_box.insert(0, text)
            rate_box.event_generate("<Return>")
            host.update()

        for text, expected in (("60", 60), ("0", 0), ("99", 60), ("-5", 0), ("37", 37)):
            type_rate(text)
            assert control_panel.approach_configs["EB"]["rate"] == expected
            assert rate_box.get() == str(expected)
        type_rate("abc")   # junk on commit: config kept, field restored
        assert control_panel.approach_configs["EB"]["rate"] == 37
        assert rate_box.get() == "37"
        rate_box.invoke("buttonup")
        assert control_panel.approach_configs["EB"]["rate"] == 38

        (model_box,) = widgets_of_type(eb_card, ttk.Combobox)
        model_box.set("Binomial")
        model_box.event_generate("<<ComboboxSelected>>")
        host.update_idletasks()
        assert control_panel.approach_configs["EB"]["model"] == "Binomial"

        (heavy_scale,) = widgets_of_type(eb_card, ttk.Scale)
        assert str(heavy_scale.cget("style")) == "Thin.Horizontal.TScale"
        heavy_scale.set(20)
        host.update_idletasks()
        assert control_panel.approach_configs["EB"]["heavy_ratio"] == 0.20

        # Turning movements: one bar, two thumbs (Straight | Left @A | Left @B).
        (split_bar,) = widgets_of_type(eb_card, control_panel.SplitBar)
        eb = control_panel.approach_configs["EB"]
        straight0, far0 = round(eb["turn_split"] * 100), round(eb["left_far_share"] * 100)
        assert split_bar.segments() == [straight0, 100 - straight0 - far0, far0]
        split_bar._set(0, 55)
        assert control_panel.approach_configs["EB"]["turn_split"] == 0.55
        assert control_panel.approach_configs["EB"]["left_far_share"] == far0 / 100
        split_bar._set(1, 70)
        assert split_bar.segments() == [55, 15, 30]
        assert control_panel.approach_configs["EB"]["left_far_share"] == 0.30
        # Thumbs never cross.
        split_bar._set(0, 95)
        assert split_bar.values == [70, 70]
        # Keyboard: the focused thumb moves by one percent per arrow.
        split_bar.focus_force()
        split_bar.active = 1
        split_bar.event_generate("<Right>")
        host.update()
        assert split_bar.values == [70, 71]

        # A single-node approach has one thumb and no far share.
        a_nb_card = cards[list(control_panel.APPROACH_NAMES).index("A_NB")]
        (a_nb_bar,) = widgets_of_type(a_nb_card, control_panel.SplitBar)
        assert len(a_nb_bar.values) == 1
        a_nb_bar._set(0, 60)
        assert control_panel.approach_configs["A_NB"]["turn_split"] == 0.60
        assert control_panel.approach_configs["A_NB"]["left_far_share"] == 0.0

        (on_off,) = [
            button for button in widgets_of_type(eb_card, tk.Button)
            if button.cget("text") in ("ON", "OFF")
        ]
        assert control_panel.approach_configs["EB"]["active"] is True
        on_off.invoke()
        assert control_panel.approach_configs["EB"]["active"] is False
        assert on_off.cget("text") == "OFF"
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

    # Seven slider rows cover simulation speed, eligibility, vehicle speed,
    # Single Run LLM interval, Batch Run LLM interval, bus headway and
    # trucks %. Turning movements are one SplitBar per approach (keyboard-
    # adjustable itself); inflow is an exact integer and uses a stepper
    # (make_spinbox). Signal cycles and green splits are derived by Webster
    # and have no UI slider. add_slider_row is the only slider constructor
    # and always wires enable_scale_keyboard.
    assert source.count("add_slider_row(") == 7
    assert source.count("add_split_bar_row(") == 1
    assert source.count("make_spinbox(") == 1
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


def test_control_strategy_selector_is_two_level_and_drives_the_model(monkeypatch):
    """Single Run: a Strategy dropdown (four families) over a Decider
    dropdown that lists only that family's deciders; the batch picker shows
    the same headings; Stop batch splits into Resume | End once stopped."""
    import tkinter as tk
    from tkinter import ttk

    monkeypatch.setattr(control_panel, "write_ai_control", lambda *a, **k: None)
    monkeypatch.setattr(control_panel, "get_ollama_models", lambda: ["None", "llama3.1:8b"])
    monkeypatch.setattr(control_panel, "get_api_models", lambda: ["None"])
    monkeypatch.setitem(
        control_panel.global_config, "ai_runtime",
        {"armed": False, "model": "None", "tick_seconds": 5, "last_status": "INACTIVE", "last_turn": 0},
    )
    host = tk.Tk()
    mounted = tk.Frame(host)
    try:
        control_panel.create_dashboard_window(mounted)
        single_body = control_panel.control_panel_sections["Single Run"]["body"]

        def combos(widget):
            out = []
            for child in widget.winfo_children():
                if isinstance(child, ttk.Combobox):
                    out.append(child)
                out.extend(combos(child))
            return out

        strategy_box, decider_box = combos(single_body)[:2]
        assert list(strategy_box.cget("values")) == list(control_panel.CONTROL_STRATEGIES)
        assert strategy_box.get() == control_panel.STRATEGY_BASELINE
        assert str(decider_box.cget("state")) == "disabled"

        strategy_box.set(control_panel.STRATEGY_RULE)
        strategy_box.event_generate("<<ComboboxSelected>>")
        assert control_panel.global_config["ai_runtime"]["model"] == control_panel.RULE_BASED_MODEL
        assert list(decider_box.cget("values")) == [
            control_panel.RULE_BASED_MODEL, control_panel.MAX_PRESSURE_MODEL
        ]
        decider_box.set(control_panel.MAX_PRESSURE_MODEL)
        decider_box.event_generate("<<ComboboxSelected>>")
        assert control_panel.global_config["ai_runtime"]["model"] == control_panel.MAX_PRESSURE_MODEL

        strategy_box.set(control_panel.STRATEGY_LLM_ASSISTED)
        strategy_box.event_generate("<<ComboboxSelected>>")
        assert control_panel.global_config["ai_runtime"]["model"] == "llama3.1:8b"
        assert control_panel.global_config["ai_runtime"]["control_mode"] == "assisted"
        assert list(decider_box.cget("values")) == ["llama3.1:8b"]
        # The fifth strategy is the same LLM family in the other control mode.
        strategy_box.set(control_panel.STRATEGY_LLM_DECIDED)
        strategy_box.event_generate("<<ComboboxSelected>>")
        assert control_panel.global_config["ai_runtime"]["control_mode"] == "configured"
        assert control_panel.global_config["ai_runtime"]["tick_seconds"] >= control_panel.CONFIGURED_TICK_SECONDS
        strategy_box.set(control_panel.STRATEGY_RULE)
        strategy_box.event_generate("<<ComboboxSelected>>")
        assert control_panel.global_config["ai_runtime"]["control_mode"] == "assisted"

        batch_body = control_panel.control_panel_sections["Batch Run"]["body"]
        assert find_buttons(batch_body, "Stop batch")
        (resume,) = find_buttons(batch_body, "Resume")
        (end,) = find_buttons(batch_body, "End")
        split_row = resume.outline_frame.master
        assert end.outline_frame.master is split_row and not split_row.winfo_manager()
        control_panel.global_config["batch_runtime"] = dict(
            control_panel.DEFAULT_BATCH_RUNTIME, active=True, paused=True, total=1,
            current={"model": "rule-based", "seed": 1},
        )
        host.update()
        host.after(400, host.quit)
        host.mainloop()
        assert split_row.winfo_manager() == "pack"
        assert not find_buttons(batch_body, "Stop batch")[0].winfo_manager()
        control_panel.global_config["batch_runtime"] = dict(control_panel.DEFAULT_BATCH_RUNTIME)
    finally:
        host.destroy()
