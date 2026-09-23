"""Three window shapes: compact (fitted window), large (~75% screen) and
maximized (OS zoomed), with the side panes following the shape and the
whole network scaled into the centre pane in every one of them."""
import time
import tkinter as tk
from tkinter import ttk

import pygame
import pytest

import src.ui.canvas_gemini as canvas
import src.ui.control_panel as control_panel
import src.core.main as main


def test_fit_canvas_size_keeps_surface_aspect_with_integer_pixels():
    step = main.CANVAS_ASPECT_STEP
    assert main.fit_canvas_size(canvas.WIDTH, canvas.HEIGHT) == (canvas.WIDTH, canvas.HEIGHT)
    for available in ((1146, 900), (2500, 700), (1500, 2000), (1780, 1068), (1000, 600)):
        width, height = main.fit_canvas_size(*available)
        assert width * canvas.HEIGHT == height * canvas.WIDTH
        assert width <= available[0] and height <= available[1]
        assert width % step == 0
    # Width-limited and height-limited cases land on the tight dimension.
    width, height = main.fit_canvas_size(1146, 3000)
    assert 1146 - step < width <= 1146
    width, height = main.fit_canvas_size(9000, 450)
    assert height <= 450 and height + canvas.HEIGHT // canvas.WIDTH * step + 1 >= 450
    # A transiently tiny pane never yields a degenerate image.
    floor = canvas.WIDTH // 4 - (canvas.WIDTH // 4) % step
    assert main.fit_canvas_size(0, 0) == (floor, floor * canvas.HEIGHT // canvas.WIDTH)


def test_large_window_geometry_is_centered_and_never_narrower_than_the_panes():
    min_width = (
        main.MAX_CONTROL_PANE_WIDTH + main.MAX_TELEMETRY_PANE_WIDTH
        + main.CANVAS_DISPLAY_WIDTH + 2 * main.SIMULATION_PANE_GUTTER
    )
    width, height, x, y = main.large_window_geometry_for(1920, 1080)
    assert width == min_width == 1764
    assert height == 810
    assert x == (1920 - width) // 2
    assert y == (1080 - main.LARGE_WINDOW_SCREEN_MARGIN_Y - height) // 2

    width, height, x, y = main.large_window_geometry_for(2560, 1440)
    assert (width, height) == (1920, 1080)
    assert (x, y) == (320, 140)


def test_side_pane_widths_follow_the_shape():
    assert main.side_pane_widths_for("compact", 2560) == (256, 333)
    for shape in ("large", "maximized"):
        assert main.side_pane_widths_for(shape, 2560) == (
            main.MAX_CONTROL_PANE_WIDTH, main.MAX_TELEMETRY_PANE_WIDTH
        )


def test_classify_window_shape():
    compact = (1613, 624)
    assert main.classify_window_shape("zoomed", 2560, 1369, compact) == "maximized"
    assert main.classify_window_shape("normal", 1613, 624, compact) == "compact"
    assert main.classify_window_shape("normal", 1617, 620, compact) == "compact"
    assert main.classify_window_shape("normal", 1920, 1080, compact) == "large"
    assert main.classify_window_shape("normal", 1613, 700, compact) == "large"


def test_build_main_window_exposes_the_shape_controller():
    root, _control, _sim, _telemetry = main.build_main_window()
    try:
        assert isinstance(root.paned, ttk.PanedWindow)
        controller = root.window_shape
        assert isinstance(controller, main.WindowShapeController)
        assert controller.shape == "compact"
    finally:
        root.destroy()


def test_compact_shape_fits_the_whole_network_into_its_pane():
    root, _control, simulation_pane, _telemetry = main.build_main_window()
    try:
        root.update()
        controller = root.window_shape
        assert controller.shape == "compact"
        fitted = []
        controller._fit_canvas = lambda w, h: fitted.append((w, h))
        controller._fit()
        (width, height), = fitted
        assert width <= simulation_pane.winfo_width()
        assert width < canvas.WIDTH  # never the clipped native 2400
        assert (width, height) == main.fit_canvas_size(
            simulation_pane.winfo_width() - 2 * main.SIMULATION_PANE_GUTTER - 2,
            simulation_pane.winfo_height() - 2 * main.SIMULATION_PANE_GUTTER - 2,
        )
    finally:
        root.destroy()


def test_cycle_walks_compact_large_maximized_and_publishes_it():
    root, _control, _sim, _telemetry = main.build_main_window()
    saved = control_panel.global_config.get("window_shape")
    try:
        root.update()
        controller = root.window_shape
        seen = []
        for _ in range(3):
            controller.cycle()
            root.update()
            seen.append((controller.shape, control_panel.global_config["window_shape"]))
        assert seen == [
            ("large", "large"), ("maximized", "maximized"), ("compact", "compact")
        ]
        assert root.state() == "normal"
        with pytest.raises(ValueError):
            controller.apply("huge")
    finally:
        control_panel.global_config["window_shape"] = saved
        root.destroy()


def test_apply_pane_widths_moves_both_sashes():
    root, control_pane, _sim, telemetry_pane = main.build_main_window()
    try:
        root.geometry("1800x900")
        root.update()
        controller = root.window_shape
        controller._apply_pane_widths(320, 420)
        root.update()
        assert abs(controller.control_outer.winfo_width() - 320) <= 1
        assert abs(controller.telemetry_outer.winfo_width() - 420) <= 1
        controller._apply_pane_widths(256, 333)
        root.update()
        assert abs(controller.control_outer.winfo_width() - 256) <= 1
        assert abs(controller.telemetry_outer.winfo_width() - 333) <= 1
    finally:
        root.destroy()


def test_root_configure_handler_ignores_child_events():
    root, _control, simulation_pane, _telemetry = main.build_main_window()
    try:
        controller = root.window_shape
        root.update()
        controller._after_id = None

        class Event:
            widget = simulation_pane

        controller._on_root_configure(Event())
        assert controller._after_id is None
        Event.widget = root
        controller._on_root_configure(Event())
        assert controller._after_id is not None
    finally:
        root.destroy()


def build_canvas(host):
    pane = tk.Frame(host)
    pane.pack()
    simulation_canvas, push_frame = main.build_simulation_canvas(pane)
    return simulation_canvas, push_frame


def photo_size(simulation_canvas):
    """(width, height) of the image the canvas item shows, queried by name
    so the test never creates (or clobbers) an image of its own."""
    (item,) = simulation_canvas.find_all()
    name = simulation_canvas.itemcget(item, "image")
    call = simulation_canvas.tk.call
    return int(call("image", "width", name)), int(call("image", "height", name))


def test_set_target_size_resizes_canvas_and_photo_in_place():
    host = tk.Tk()
    try:
        simulation_canvas, push_frame = build_canvas(host)
        items_before = simulation_canvas.find_all()
        simulation_canvas.set_target_size(1150, 690)
        host.update_idletasks()
        assert int(simulation_canvas["width"]) == 1150
        assert int(simulation_canvas["height"]) == 690
        assert photo_size(simulation_canvas) == (1150, 690)
        push_frame(pygame.Surface((canvas.WIDTH, canvas.HEIGHT)))
        assert simulation_canvas.find_all() == items_before

        simulation_canvas.set_target_size(canvas.WIDTH, canvas.HEIGHT)
        host.update_idletasks()
        assert int(simulation_canvas["width"]) == canvas.WIDTH
        assert photo_size(simulation_canvas) == (canvas.WIDTH, canvas.HEIGHT)
        assert simulation_canvas.find_all() == items_before
    finally:
        host.destroy()


def test_push_frame_scales_only_when_the_target_differs(monkeypatch):
    host = tk.Tk()
    calls = []
    real = pygame.transform.smoothscale

    def counting(surface, size, dest=None):
        calls.append(size)
        return real(surface, size, dest)

    monkeypatch.setattr(main.pygame.transform, "smoothscale", counting)
    try:
        simulation_canvas, push_frame = build_canvas(host)
        surface = pygame.Surface((canvas.WIDTH, canvas.HEIGHT))
        push_frame(surface)
        assert calls == []
        simulation_canvas.set_target_size(1000, 250)
        push_frame(surface)
        push_frame(surface)
        assert calls == [(1000, 250), (1000, 250)]
    finally:
        host.destroy()


def test_view_zoom_crops_the_surface_and_is_display_only(monkeypatch):
    host = tk.Tk()
    sources = []
    real = pygame.transform.smoothscale

    def recording(surface, size, dest=None):
        sources.append(surface.get_size())
        return real(surface, size, dest)

    monkeypatch.setattr(main.pygame.transform, "smoothscale", recording)
    try:
        simulation_canvas, push_frame = build_canvas(host)
        surface = pygame.Surface((canvas.WIDTH, canvas.HEIGHT))
        surface.fill((1, 2, 3))
        simulation_canvas.set_view_zoom(2.0)
        push_frame(surface)
        assert sources == [(canvas.WIDTH // 2, canvas.HEIGHT // 2)]
        assert simulation_canvas.view_rect().center == (canvas.WIDTH // 2, canvas.HEIGHT // 2)
        # Zooming keeps the world point under the cursor fixed: from zoom 2
        # the top-left corner shows world (600, 150), and it still does at 4.
        simulation_canvas.set_view_zoom(4.0, at=(0, 0))
        assert simulation_canvas.view_rect().topleft == (600, 150)
        simulation_canvas.set_view_zoom(1.0)
        simulation_canvas.set_view_zoom(4.0, at=(0, 0))
        assert simulation_canvas.view_rect().topleft == (0, 0)
        assert simulation_canvas.set_view_zoom(99) is None
        assert simulation_canvas.view_rect().size == (
            int(canvas.WIDTH / main.MAX_VIEW_ZOOM), int(canvas.HEIGHT / main.MAX_VIEW_ZOOM)
        )
        # The physics surface itself is untouched by zooming.
        assert surface.get_size() == (canvas.WIDTH, canvas.HEIGHT)
        # Vehicles: drawn on the native surface while the view is at most
        # 1 screen px per world px, on the scaled frame once zoomed past it.
        simulation_canvas.set_target_size(canvas.WIDTH // 2, canvas.HEIGHT // 2)
        calls = []
        simulation_canvas.set_view_zoom(1.0)
        push_frame(surface, lambda target, view, scale: calls.append((target.get_size(), view, scale)))
        assert calls == [((canvas.WIDTH, canvas.HEIGHT), None, 1.0)]
        calls.clear()
        simulation_canvas.set_view_zoom(4.0)
        push_frame(surface, lambda target, view, scale: calls.append((target.get_size(), view, scale)))
        (size, view, scale), = calls
        assert size == (canvas.WIDTH // 2, canvas.HEIGHT // 2)
        assert view == simulation_canvas.view_rect() and scale == 2.0
        # Left and middle button both pan, in whatever shape the canvas is.
        for button in (1, 2):
            assert simulation_canvas.bind(f"<B{button}-Motion>")
            assert simulation_canvas.bind(f"<ButtonPress-{button}>")
        simulation_canvas.set_view_zoom(1.0)
        simulation_canvas.set_target_size(canvas.WIDTH, canvas.HEIGHT)
        sources.clear()
        push_frame(surface)
        assert sources == []   # native size, zoom 1: no scaling at all
    finally:
        host.destroy()


def test_render_rate_halves_above_the_pixel_threshold():
    """frame_is_due() is the one render gate: the loop calls it before the
    network draw, the DBL-state scan AND the push, so a skipped tick costs
    nothing at all rather than drawing a frame it then throws away."""
    host = tk.Tk()
    try:
        simulation_canvas, _push_frame = build_canvas(host)
        assert 1300 * 325 <= main.FULL_RATE_PUSH_MAX_PIXELS < 1900 * 475
        simulation_canvas.set_target_size(1900, 475)   # maximized on 1080p
        assert sum(simulation_canvas.frame_is_due() for _ in range(4)) == 2
        simulation_canvas.set_target_size(1300, 325)   # large: full rate
        assert sum(simulation_canvas.frame_is_due() for _ in range(4)) == 4
    finally:
        host.destroy()


def test_a_batch_sweep_drops_the_render_rate(monkeypatch):
    """Nobody watches an unattended sweep at 60 fps, and the render side was
    a fifth of every run's wall clock. It must not touch anything the
    simulation or the agent can see -- only how often a frame is painted."""
    host = tk.Tk()
    try:
        simulation_canvas, _push_frame = build_canvas(host)
        simulation_canvas.set_target_size(1300, 325)   # full rate otherwise
        runtime = main.control_panel.global_config.setdefault("batch_runtime", {})
        monkeypatch.setitem(runtime, "active", True)
        ticks = main.BATCH_RENDER_EVERY * 3
        assert sum(simulation_canvas.frame_is_due() for _ in range(ticks)) == 3
        monkeypatch.setitem(runtime, "active", False)
        assert sum(simulation_canvas.frame_is_due() for _ in range(4)) == 4
    finally:
        host.destroy()


def test_shape_button_cycles_through_the_installed_hook(monkeypatch):
    host = tk.Tk()
    calls = []
    monkeypatch.setitem(control_panel.window_shape_hooks, "cycle", lambda: calls.append(1))
    saved = control_panel.global_config.get("window_shape")
    try:
        pane = tk.Frame(host)
        control_panel.create_dashboard_window(pane)

        def find(widget):
            for child in widget.winfo_children():
                if isinstance(child, tk.Button) and "Large" in child.cget("text"):
                    return child
                found = find(child)
                if found is not None:
                    return found

        button = find(pane)
        assert button is not None
        button.invoke()
        assert calls == [1]

        # The label follows the published shape, so an OS maximize/restore
        # relabels it too.
        control_panel.global_config["window_shape"] = "maximized"
        # Pump the event loop with update() rather than mainloop(): a
        # SystemExit that an earlier test's Tk callback raised is stored by
        # _tkinter and re-raised from the next mainloop() in the process.
        for _ in range(8):
            time.sleep(0.05)
            host.update()
        assert "Compact" in button.cget("text")
    finally:
        control_panel.global_config["window_shape"] = saved
        host.destroy()


def test_request_cycle_without_a_hook_is_a_noop(monkeypatch):
    monkeypatch.setitem(control_panel.window_shape_hooks, "cycle", None)
    control_panel.request_window_shape_cycle()
