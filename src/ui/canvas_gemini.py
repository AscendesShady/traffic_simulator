# canvas_gemini.py
import time

import pygame


# ==========================================================
# AUTHORITATIVE NETWORK GEOMETRY
# ==========================================================
WIDTH, HEIGHT = 1000, 600
CANVAS_HEIGHT = HEIGHT

LANE = 22
LANES = 3
ROAD_W = 2 * LANE * LANES  # 132 px
H_Y = 300
INT_X = [300, 700]  # Node A and Node B
STOP = 10


# ==========================================================
# VISUAL DESIGN
# ==========================================================
BG = (30, 80, 30)
ROAD = (50, 50, 50)

WHITE = (230, 230, 230)
LANE_GREY = (130, 130, 130)
YELLOW = (220, 180, 40)
KEEP_CLEAR = (210, 210, 0)
POLE = (160, 160, 160)
LABEL_TEXT = (200, 200, 200)

SIGNAL_COLORS = {
    "RED": (220, 50, 50),
    "YELLOW": (220, 180, 40),
    "GREEN": (50, 220, 50),
}

# Rendering fallback color.
RED = SIGNAL_COLORS["RED"]


# Monotonic flash clock state. Paused time is subtracted so the DBL flash
# resumes from the same phase instead of jumping to wall-clock phase.
paused_freeze_timestamp = 0.0
_dbl_pause_started = None
_dbl_total_paused = 0.0


def _resolve_signal_color(state):
    """Convert a semantic signal state to RGB, failing safely to red.

    RGB tuples/lists remain accepted for compatibility with existing tests and
    reusable callers, but production signal behavior uses semantic strings.
    """
    if isinstance(state, str):
        return SIGNAL_COLORS.get(state.upper(), RED)
    if isinstance(state, (tuple, list)) and len(state) in (3, 4):
        try:
            return tuple(int(component) for component in state)
        except (TypeError, ValueError):
            return RED
    return RED


def _get_dbl_flash_clock(is_paused):
    """Return elapsed monotonic animation time with paused duration removed."""
    global paused_freeze_timestamp, _dbl_pause_started, _dbl_total_paused

    now = time.monotonic()
    if is_paused:
        if _dbl_pause_started is None:
            _dbl_pause_started = now
        effective_time = _dbl_pause_started - _dbl_total_paused
    else:
        if _dbl_pause_started is not None:
            _dbl_total_paused += now - _dbl_pause_started
            _dbl_pause_started = None
        effective_time = now - _dbl_total_paused

    paused_freeze_timestamp = effective_time
    return effective_time


def dashed(surface, start, end, color, width=1):
    """Draw lane markings while keeping intersection conflict boxes clear."""
    dash = 10
    gap = 8
    x1, y1 = start
    x2, y2 = end

    if x1 == x2:
        y = y1
        while y < y2:
            in_intersection = H_Y - ROAD_W // 2 <= y <= H_Y + ROAD_W // 2
            if not in_intersection:
                pygame.draw.line(
                    surface,
                    color,
                    (x1, y),
                    (x1, min(y + dash, y2)),
                    width,
                )
            y += dash + gap
    else:
        x = x1
        while x < x2:
            in_intersection = any(
                cx - ROAD_W // 2 <= x <= cx + ROAD_W // 2 for cx in INT_X
            )
            if not in_intersection:
                pygame.draw.line(
                    surface,
                    color,
                    (x, y1),
                    (min(x + dash, x2), y1),
                    width,
                )
            x += dash + gap


def draw_cleared_medians(surface):
    """Draw center medians only outside the intersection boxes."""
    half = ROAD_W // 2

    pygame.draw.line(surface, YELLOW, (0, H_Y), (INT_X[0] - half, H_Y), 2)
    pygame.draw.line(
        surface,
        YELLOW,
        (INT_X[0] + half, H_Y),
        (INT_X[1] - half, H_Y),
        2,
    )
    pygame.draw.line(
        surface,
        YELLOW,
        (INT_X[1] + half, H_Y),
        (WIDTH, H_Y),
        2,
    )

    for cx in INT_X:
        pygame.draw.line(surface, YELLOW, (cx, 0), (cx, H_Y - half), 2)
        pygame.draw.line(surface, YELLOW, (cx, H_Y + half), (cx, HEIGHT), 2)


def draw_road_labels(screen, font):
    """Draw node and approach labels used to orient the live simulation."""
    half = ROAD_W // 2

    node_a = font.render("NODE A", True, (255, 255, 255))
    node_b = font.render("NODE B", True, (255, 255, 255))
    screen.blit(node_a, (INT_X[0] - 25, H_Y - 8))
    screen.blit(node_b, (INT_X[1] - 25, H_Y - 8))

    eb_label = font.render("EB Corridor -->", True, LABEL_TEXT)
    wb_label = font.render("<-- WB Corridor", True, LABEL_TEXT)
    screen.blit(eb_label, (20, H_Y - half - 20))
    screen.blit(wb_label, (WIDTH - 150, H_Y + half + 8))

    a_sb_label = font.render("A_SB |", True, LABEL_TEXT)
    a_nb_label = font.render("A_NB ^", True, LABEL_TEXT)
    screen.blit(a_sb_label, (INT_X[0] + half + 8, 20))
    screen.blit(a_nb_label, (INT_X[0] - half - 55, HEIGHT - 35))

    b_sb_label = font.render("B_SB |", True, LABEL_TEXT)
    b_nb_label = font.render("B_NB ^", True, LABEL_TEXT)
    screen.blit(b_sb_label, (INT_X[1] + half + 8, 20))
    screen.blit(b_nb_label, (INT_X[1] - half - 55, HEIGHT - 35))


def get_signal_light_center(x, y, facing):
    if facing == "EASTBOUND":
        return x - 6, y
    if facing == "WESTBOUND":
        return x + 6, y
    if facing == "NORTHBOUND":
        return x, y + 6
    if facing == "SOUTHBOUND":
        return x, y - 6
    return x, y


def draw_signal_head(screen, x, y, facing, state="RED"):
    """Draw one primary signal head from a semantic state or RGB value."""
    pygame.draw.rect(screen, POLE, (x - 2, y - 2, 4, 4))
    center_x, center_y = get_signal_light_center(x, y, facing)
    pygame.draw.circle(
        screen,
        _resolve_signal_color(state),
        (center_x, center_y),
        6,
    )


def draw_dbl_signal(screen, x, y, facing, state="INACTIVE", is_paused=False):
    """Draw DBL request state without presenting a pending grant as active."""
    flash_time = _get_dbl_flash_clock(is_paused)

    if isinstance(state, bool):
        state = "ACTIVE" if state else "INACTIVE"
    state = str(state).upper()

    if state == "ACTIVE":
        is_flash_on = int(flash_time * 10) % 2 == 0
        light_color = (0, 255, 120) if is_flash_on else (0, 60, 20)
    elif state == "CLEARING":
        is_flash_on = int(flash_time * 5) % 2 == 0
        light_color = (0, 200, 220) if is_flash_on else (0, 45, 50)
    elif state in ("REQUESTED", "TRANSITIONING"):
        light_color = (245, 158, 11)
    else:
        light_color = (0, 0, 0)

    center_x, center_y = get_signal_light_center(x, y, facing)

    if facing == "EASTBOUND":
        box_x, box_y = center_x, center_y - 16
    elif facing == "WESTBOUND":
        box_x, box_y = center_x, center_y + 16
    elif facing == "NORTHBOUND":
        box_x, box_y = center_x - 16, center_y
    elif facing == "SOUTHBOUND":
        box_x, box_y = center_x + 16, center_y
    else:
        box_x, box_y = center_x, center_y

    pygame.draw.circle(screen, (30, 30, 35), (box_x, box_y), 6)
    pygame.draw.circle(screen, light_color, (box_x, box_y), 4)
    pygame.draw.circle(screen, (200, 200, 200), (box_x, box_y), 6, 1)


def draw_intersection(
    screen,
    center_x,
    signal_states=None,
    dbl_states=None,
    is_paused=False,
):
    """Draw one intersection using safe semantic signal and DBL maps."""
    half = ROAD_W // 2
    states = signal_states if isinstance(signal_states, dict) else {}
    dbls = dbl_states if isinstance(dbl_states, dict) else {}

    pygame.draw.rect(
        screen,
        KEEP_CLEAR,
        (center_x - half, H_Y - half, ROAD_W, ROAD_W),
        2,
    )

    # Stop bars cover the correct approach carriageway half.
    pygame.draw.line(
        screen,
        WHITE,
        (center_x - half - STOP, H_Y - half),
        (center_x - half - STOP, H_Y),
        3,
    )
    pygame.draw.line(
        screen,
        WHITE,
        (center_x + half + STOP, H_Y),
        (center_x + half + STOP, H_Y + half),
        3,
    )
    pygame.draw.line(
        screen,
        WHITE,
        (center_x - half, H_Y + half + STOP),
        (center_x, H_Y + half + STOP),
        3,
    )
    pygame.draw.line(
        screen,
        WHITE,
        (center_x, H_Y - half - STOP),
        (center_x + half, H_Y - half - STOP),
        3,
    )

    eastbound_position = (center_x - half - STOP, H_Y - half - 12)
    westbound_position = (center_x + half + STOP, H_Y + half + 12)
    northbound_position = (center_x - half - 12, H_Y + half + STOP)
    southbound_position = (center_x + half + 12, H_Y - half - STOP)

    draw_signal_head(
        screen,
        eastbound_position[0],
        eastbound_position[1],
        "EASTBOUND",
        states.get("EB", "RED"),
    )
    draw_signal_head(
        screen,
        westbound_position[0],
        westbound_position[1],
        "WESTBOUND",
        states.get("WB", "RED"),
    )
    draw_signal_head(
        screen,
        northbound_position[0],
        northbound_position[1],
        "NORTHBOUND",
        states.get("NB", "RED"),
    )
    draw_signal_head(
        screen,
        southbound_position[0],
        southbound_position[1],
        "SOUTHBOUND",
        states.get("SB", "RED"),
    )

    draw_dbl_signal(
        screen,
        eastbound_position[0],
        eastbound_position[1],
        "EASTBOUND",
        dbls.get("EB", "INACTIVE"),
        is_paused,
    )
    draw_dbl_signal(
        screen,
        westbound_position[0],
        westbound_position[1],
        "WESTBOUND",
        dbls.get("WB", "INACTIVE"),
        is_paused,
    )
    draw_dbl_signal(
        screen,
        northbound_position[0],
        northbound_position[1],
        "NORTHBOUND",
        dbls.get("NB", "INACTIVE"),
        is_paused,
    )
    draw_dbl_signal(
        screen,
        southbound_position[0],
        southbound_position[1],
        "SOUTHBOUND",
        dbls.get("SB", "INACTIVE"),
        is_paused,
    )


def draw_network(
    screen,
    signal_data=None,
    dbl_states=None,
    font=None,
    is_paused=False,
):
    """Draw the complete two-node road network."""
    signal_data = signal_data if isinstance(signal_data, dict) else {}
    dbl_states = dbl_states if isinstance(dbl_states, dict) else {}

    screen.fill(BG)

    pygame.draw.rect(
        screen,
        ROAD,
        (0, H_Y - ROAD_W // 2, WIDTH, ROAD_W),
    )
    for center_x in INT_X:
        pygame.draw.rect(
            screen,
            ROAD,
            (center_x - ROAD_W // 2, 0, ROAD_W, HEIGHT),
        )

    # These offsets are lane boundaries, not vehicle centerlines.
    lane_boundary_offsets = [-2 * LANE, -LANE, LANE, 2 * LANE]

    for offset in lane_boundary_offsets:
        dashed(
            screen,
            (0, H_Y + offset),
            (WIDTH, H_Y + offset),
            LANE_GREY,
            1,
        )

    for center_x in INT_X:
        for offset in lane_boundary_offsets:
            dashed(
                screen,
                (center_x + offset, 0),
                (center_x + offset, HEIGHT),
                LANE_GREY,
                1,
            )

    draw_cleared_medians(screen)

    for center_x in INT_X:
        draw_intersection(
            screen,
            center_x,
            signal_data.get(center_x),
            dbl_states.get(center_x),
            is_paused,
        )

    if font is not None:
        draw_road_labels(screen, font)
