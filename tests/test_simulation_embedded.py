"""Checkpoint 1, commit 5: the pygame simulation renders into MainWindow's
center pane via an offscreen surface, instead of its own native window.
"""
import inspect

import pygame

import src.ui.canvas_gemini as canvas
import src.core.main as main


def test_build_simulation_canvas_creates_a_fixed_size_canvas():
    root, _control, simulation_pane, _telemetry = main.build_main_window()
    try:
        simulation_canvas, push_frame = main.build_simulation_canvas(
            simulation_pane
        )

        assert int(simulation_canvas["width"]) == canvas.WIDTH
        assert int(simulation_canvas["height"]) == canvas.HEIGHT
        assert callable(push_frame)
    finally:
        root.destroy()


def test_push_frame_mutates_the_same_photoimage_never_recreates_it():
    """The image item must be itemconfig'd in place, per the task's own
    "never recreate it" requirement -- otherwise a stray reference drop
    mid-frame would blank the canvas."""
    root, _control, simulation_pane, _telemetry = main.build_main_window()
    try:
        pygame.init()
        simulation_canvas, push_frame = main.build_simulation_canvas(
            simulation_pane
        )
        image_ids_before = simulation_canvas.find_all()

        surface = pygame.Surface((canvas.WIDTH, canvas.HEIGHT))
        surface.fill((30, 80, 30))
        push_frame(surface)
        push_frame(surface)
        push_frame(surface)

        image_ids_after = simulation_canvas.find_all()
        assert image_ids_before == image_ids_after
        assert len(image_ids_after) == 1
    finally:
        root.destroy()


def test_push_frame_actually_carries_real_pixel_data():
    """Not just a no-crash smoke test: a distinctive fill must survive the
    Surface -> PPM -> PhotoImage round trip."""
    root, _control, simulation_pane, _telemetry = main.build_main_window()
    try:
        pygame.init()
        _canvas_widget, push_frame = main.build_simulation_canvas(
            simulation_pane
        )
        surface = pygame.Surface((canvas.WIDTH, canvas.HEIGHT))
        surface.fill((12, 34, 56))
        push_frame(surface)
        root.update_idletasks()

        photo = _canvas_widget.itemcget(_canvas_widget.find_all()[0], "image")
        assert photo
    finally:
        root.destroy()


def test_no_pygame_display_window_is_created():
    """Task-specified method: offscreen Surface, not SDL_WINDOWID
    reparenting and not a real display window."""
    source = inspect.getsource(main.main)

    assert "pygame.display.set_mode(" not in source
    assert "pygame.display.set_caption(" not in source
    assert "pygame.display.flip()" not in source
    assert "pygame.Surface((canvas.WIDTH, canvas.HEIGHT))" in source


def test_startup_window_positioning_env_var_is_gone():
    """SDL_VIDEO_WINDOW_POS positioned the old native pygame window; there
    is nothing left to position."""
    source = inspect.getsource(main.main)

    assert "SDL_VIDEO_WINDOW_POS" not in source
    assert "calculate_startup_window_layout(" not in source
    # The function itself stays defined and independently tested -- only
    # main() stopped calling it, since main.py is the only thing that used
    # to consume its per-window geometry for the three old windows.
    assert hasattr(main, "calculate_startup_window_layout")


def test_every_tick_pushes_an_interpolated_frame():
    """Each tick pushes one frame (the pushed size is the pane, so it is
    cheap; build_simulation_canvas halves the rate itself past the pixel
    threshold), with vehicles drawn at the interpolated position between
    the last two physics steps so the display cadence never beats against
    the 60 Hz sim into a hop."""
    source = inspect.getsource(main.main)

    assert "visual_frame_counter" not in source
    assert "push_simulation_frame(screen, draw_vehicles)" in source
    assert "time_accumulator / dt_step" in source
    # One gate over every render-only step, so a skipped tick draws nothing.
    assert "if _simulation_canvas.frame_is_due():" in source


def test_sim_step_pacing_constants_are_unchanged():
    """The fixed-timestep loop that makes master_frame_count advance at a
    stable rate must be untouched by the rendering change; the tick is
    re-armed against the tick's start so work inside it does not stretch
    the period."""
    source = inspect.getsource(main.main)

    assert "dt_step = 1.0 / 60.0" in source
    assert "max_steps_per_callback = 6" in source
    assert main.TICK_MS == 16
    assert "root.after(max(1, int(TICK_MS - work_ms)), simulation_step)" in source
