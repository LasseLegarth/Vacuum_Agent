"""Per-room battery gate arithmetic (jobs/battery_gate.py).

need(room) = max of the room's last five clean single-room samples once it has three,
first from the stats for the settings it is dispatched with, then from the room's
baseline (all settings), else a fixed fallback. A phase needs the sum of its rooms.
"""

from __future__ import annotations

from custom_components.eufy_vacuum.jobs import battery_gate as bg


def _exact(slug, recent, *, count=None, passes=1, mode="vacuum"):
    return {
        "map_id": 0, "room_slug": slug, "effective_mode": mode, "clean_times": passes,
        "is_carpet": False, "clean_intensity": "", "edge_mopping": False,
        "battery_recent": recent, "battery_recent_max": max(recent) if recent else None,
        "battery_clean_sample_count": len(recent) if count is None else count,
    }


def _base(slug, recent):
    return {
        "map_id": 0, "room_slug": slug, "battery_recent": recent,
        "battery_recent_max": max(recent) if recent else None,
        "battery_clean_sample_count": len(recent),
    }


def _room(slug, *, passes=1, mode="vacuum"):
    return {"room_id": 1, "slug": slug, "clean_mode": mode, "clean_passes": passes}


STATS = {
    "room_stats": [
        _exact("living_room", [13, 14, 13]),
        _exact("corridor", [3, 3]),
        _exact("office", [10, 10, 13], passes=2),
    ],
    "room_baselines": [
        _base("living_room", [13, 14, 13]),
        _base("corridor", [3, 3]),
        _base("office", [9, 10, 10, 11, 12]),
    ],
}


def test_exact_settings_with_three_samples_use_their_max():
    assert bg.room_need(STATS, "Home Map", _room("living_room")) == (14, "exact")


def test_too_few_samples_fall_back_to_the_fixed_need():
    assert bg.room_need(STATS, "Home Map", _room("corridor")) == (bg.FALLBACK_PERCENT, "fallback")


def test_unknown_room_and_missing_stats_fall_back():
    assert bg.room_need(STATS, "Home Map", _room("kids_room"))[1] == "fallback"
    assert bg.room_need(None, "Home Map", _room("kids_room")) == (bg.FALLBACK_PERCENT, "fallback")


def test_other_settings_fall_back_to_the_room_baseline():
    # office has exact stats only for 2 passes; a 1-pass dispatch uses the baseline
    assert bg.room_need(STATS, "Home Map", _room("office")) == (12, "baseline")
    assert bg.room_need(STATS, "Home Map", _room("office", passes=2)) == (13, "exact")


def test_stats_written_before_the_gate_existed_fall_back():
    legacy = {"room_stats": [{"map_id": 0, "room_slug": "kitchen", "effective_mode": "vacuum",
                              "clean_times": 1, "avg_battery_used": 11}]}
    assert bg.room_need(legacy, "Home Map", _room("kitchen"))[1] == "fallback"


def test_phase_need_sums_its_rooms_and_required_adds_threshold_and_margin():
    rooms = [_room("living_room"), _room("corridor")]
    assert bg.rooms_need(STATS, "Home Map", rooms) == 14 + bg.FALLBACK_PERCENT
    assert bg.required_percent(14, 20) == 14 + 20 + bg.MARGIN_PERCENT


def test_hold_state_helpers():
    assert not bg.battery_hold_active({})
    assert not bg.phase_was_held({"battery_gate": {"passed": True}})
    held = {"battery_gate": {"held": True}}
    assert bg.battery_hold_active(held) and bg.phase_was_held(held)
    # released but not yet sent: nothing runs on the robot, still a hold
    released = {"battery_gate": {"held": True, "released_at": "2026-11-24T10:00:00Z"}}
    assert bg.battery_hold_active(released)
    sent = {"battery_gate": {"held": True, "released_at": "x", "dispatched_at": "y"}}
    assert not bg.battery_hold_active(sent) and bg.phase_was_held(sent)
    failed = {"battery_gate": {"held": True, "failed_at": "x"}}
    assert not bg.battery_hold_active(failed)
