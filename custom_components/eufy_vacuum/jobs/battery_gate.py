"""Per-room battery gate (run option ``battery_gate_per_room``).

A strict-order room phase is only dispatched when the battery covers it: the rooms'
learned drain plus the low-battery threshold plus a margin. A robot that runs flat
mid-room docks at the threshold and, on several brands, arms its own native
"recharge and resume", which later starts cleaning by itself even when the run was
cancelled in the meantime. When the battery is short the phase runner holds the
phase on the dock instead (``PhaseRunner._battery_gate``); this module holds the
arithmetic and the phase-state predicates other modules share.

need(room): the max of the room's last five clean single-room samples
(``stats_rebuilder._battery_gate_fields``) once it has ``MIN_SAMPLES``; first from the
stats for the settings it is dispatched with, then from the room's baseline (all
settings), else ``FALLBACK_PERCENT``.
"""

from __future__ import annotations

from typing import Any

from ..learning.utils import _canonical_clean_intensity, _canonical_clean_mode

MARGIN_PERCENT = 5
FALLBACK_PERCENT = 20
MIN_SAMPLES = 3
HOLD_POLL_SECONDS = 30
HOLD_TIMEOUT_MINUTES = 360


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _usable(entry: dict[str, Any] | None) -> float | None:
    if not isinstance(entry, dict):
        return None
    if _safe_int(entry.get("battery_clean_sample_count")) < MIN_SAMPLES:
        return None
    value = entry.get("battery_recent_max")
    return float(value) if isinstance(value, (int, float)) else None


def room_need(stats: dict[str, Any] | None, map_id: Any, room: dict[str, Any]) -> tuple[float, str]:
    """Return (need %, source) for one resolved room; source is exact/baseline/fallback."""
    stats = stats if isinstance(stats, dict) else {}
    map_int = _safe_int(map_id)
    slug = str(room.get("slug", "")).strip().lower()

    def _same_room(item: dict[str, Any]) -> bool:
        return _safe_int(item.get("map_id")) == map_int and item.get("room_slug") == slug

    for item in stats.get("room_stats") or []:
        if (
            isinstance(item, dict)
            and _same_room(item)
            and _canonical_clean_mode(item.get("effective_mode"))
            == _canonical_clean_mode(room.get("clean_mode"))
            and _safe_int(item.get("clean_times"), 1) == _safe_int(room.get("clean_passes", 1), 1)
            and bool(item.get("is_carpet")) == bool(room.get("carpet", False))
            and _canonical_clean_intensity(item.get("clean_intensity"))
            == _canonical_clean_intensity(room.get("clean_intensity"))
            and bool(item.get("edge_mopping", False)) == bool(room.get("edge_mopping", False))
        ):
            need = _usable(item)
            if need is not None:
                return need, "exact"
            break
    for item in stats.get("room_baselines") or []:
        if isinstance(item, dict) and _same_room(item):
            need = _usable(item)
            if need is not None:
                return need, "baseline"
            break
    return float(FALLBACK_PERCENT), "fallback"


def rooms_need(stats: dict[str, Any] | None, map_id: Any, rooms: list[dict[str, Any]]) -> float:
    """Sum of the rooms' needs. Each need is dock to dock, so a group phase counts the
    drive out and back once per room: cautious, never short."""
    return sum(room_need(stats, map_id, r)[0] for r in rooms if isinstance(r, dict))


def required_percent(need: float, threshold: int) -> float:
    """Battery needed before a phase with ``need`` may start."""
    return need + threshold + MARGIN_PERCENT


def battery_hold_active(phase: Any) -> bool:
    """True while the gate holds the phase on the dock: from the hold until the phase
    is actually sent (released but not yet dispatched still counts: nothing runs on
    the robot), unless the hold failed (then the run must stay reapable)."""
    gate = phase.get("battery_gate") if isinstance(phase, dict) else None
    return (
        isinstance(gate, dict)
        and bool(gate.get("held"))
        and not gate.get("dispatched_at")
        and not gate.get("failed_at")
    )


def hold_attributes(job: Any) -> dict[str, Any] | None:
    """The current phase's active hold, for the active-job sensor and the card."""
    if not isinstance(job, dict):
        return None
    phases = job.get("phases")
    idx = _safe_int(job.get("current_phase_index"), -1)
    if not isinstance(phases, list) or not 0 <= idx < len(phases):
        return None
    if not battery_hold_active(phases[idx]):
        return None
    gate = phases[idx]["battery_gate"]
    return {
        "phase_index": idx,
        "room_ids": list(phases[idx].get("queue_room_ids") or []),
        "from": gate.get("from"),
        "required": gate.get("required"),
        "started_at": gate.get("started_at"),
        "deadline": gate.get("deadline"),
    }


def phase_was_held(phase: Any) -> bool:
    """True when the gate held (and so charged) the phase at some point."""
    gate = phase.get("battery_gate") if isinstance(phase, dict) else None
    return isinstance(gate, dict) and bool(gate.get("held"))
