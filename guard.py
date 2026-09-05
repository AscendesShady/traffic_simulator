"""Strict validation boundary for LLM-produced traffic-priority decisions."""

import json
from pathlib import Path
import re
import time

import control_panel


BASE_DIR = Path(__file__).resolve().parent
REJECT_LOG_PATH = BASE_DIR / "agent_rejects.log"
ROUTE_ORDER = sorted(control_panel.bus_routes_config.keys())
VALID_ROUTES = set(ROUTE_ORDER)
FLAG_KEYS = {"tsp", "dbl"}
MAX_REASON_LEN = 500


def extract_json(raw_text: str) -> dict | None:
    """Extract the first balanced JSON object from potentially noisy output."""
    try:
        raw = raw_text if isinstance(raw_text, str) else str(raw_text)
        raw = re.sub(
            r"<think>.*?</think>",
            "",
            raw,
            flags=re.DOTALL | re.IGNORECASE,
        )
        raw = re.sub(r"```(?:json)?", "", raw, flags=re.IGNORECASE)

        start = None
        depth = 0
        in_string = False
        escaped = False
        for index, character in enumerate(raw):
            if start is None:
                if character == "{":
                    start = index
                    depth = 1
                continue

            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue

            if character == '"':
                in_string = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    parsed = json.loads(raw[start : index + 1])
                    return parsed if isinstance(parsed, dict) else None
        return None
    except Exception:
        return None


def validate_flags(obj: dict) -> dict | None:
    """Return a complete strict-bool flag map, or ``None`` on any mismatch."""
    if not isinstance(obj, dict):
        return None
    flags = obj.get("flags")
    if not isinstance(flags, dict) or set(flags) != VALID_ROUTES:
        return None
    for route_flags in flags.values():
        if not isinstance(route_flags, dict) or set(route_flags) != FLAG_KEYS:
            return None
        if not isinstance(route_flags["tsp"], bool):
            return None
        if not isinstance(route_flags["dbl"], bool):
            return None
    return flags


def validate_flags_positional(obj: dict) -> dict | None:
    """Strictly validate model arrays and map positions to canonical routes."""
    if not isinstance(obj, dict):
        return None
    tsp = obj.get("tsp")
    dbl = obj.get("dbl")
    route_count = len(ROUTE_ORDER)
    if not isinstance(tsp, list) or len(tsp) != route_count:
        return None
    if not isinstance(dbl, list) or len(dbl) != route_count:
        return None
    if any(not isinstance(value, bool) for value in tsp):
        return None
    if any(not isinstance(value, bool) for value in dbl):
        return None
    return {
        route_id: {"tsp": tsp[index], "dbl": dbl[index]}
        for index, route_id in enumerate(ROUTE_ORDER)
    }


def extract_reason(obj) -> str:
    """Forgivingly extract an optional annotation without affecting flags."""
    if not isinstance(obj, dict):
        return ""
    reason = obj.get("reason", "")
    if not isinstance(reason, str):
        return ""
    reason = reason.strip()
    return reason[:MAX_REASON_LEN]


def all_off_flags() -> dict:
    """Return a fresh, complete fail-safe flag map."""
    return {
        route_id: {"tsp": False, "dbl": False}
        for route_id in ROUTE_ORDER
    }


def _safe_turn(turn) -> int:
    try:
        return int(turn)
    except (TypeError, ValueError, OverflowError):
        return 0


def _record_rejection(turn: int, model: str, raw_text: str) -> None:
    try:
        record = {
            "turn": turn,
            "model": model,
            "raw": raw_text[:2000],
        }
        with REJECT_LOG_PATH.open("a", encoding="utf-8") as reject_log:
            reject_log.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # Logging must never weaken the all-off airlock.
        pass


def safe_decision(raw_text: str, turn: int, model: str) -> dict:
    """Always return a complete decision; malformed output is held all-off."""
    safe_turn = _safe_turn(turn)
    safe_model = model if isinstance(model, str) else str(model)
    safe_raw = raw_text if isinstance(raw_text, str) else str(raw_text)
    reason = ""
    try:
        parsed = extract_json(safe_raw)
        flags = validate_flags_positional(parsed)
        reason = extract_reason(parsed)
    except Exception:
        flags = None

    if flags is None:
        status = "HELD_ALL_OFF"
        flags = all_off_flags()
        reason = ""
        _record_rejection(safe_turn, safe_model, safe_raw)
    else:
        status = "OK"

    return {
        "schema_version": 1,
        "turn": safe_turn,
        "timestamp": round(time.time(), 3),
        "model": safe_model,
        "status": status,
        "flags": flags,
        "reason": reason if status == "OK" else "",
    }
