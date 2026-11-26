"""battery_gate_per_room: a room phase is held on the dock until the battery covers it.

[BGH-1]  enough battery -> no hold, no return_to_base, the decision is stamped
[BGH-2]  short battery -> return_to_base, hold, release at the hold TARGET (rest of the
         run with the rooms allowed now), release battery + time recorded
[BGH-3]  the target leaves out rooms the rules block now and is capped at 100
[BGH-4]  a cancel during the hold -> the loop exits without releasing
[BGH-5]  recheck_rules_per_room: the room becomes blocked during the hold -> skipped
         (battery-hold reason), the run advances, no release
[BGH-6]  deadline: at/above `required` releases with what it has; below cancels
         with battery_gate_timeout
[BGH-7]  a re-arm resumes a persisted hold with its ORIGINAL deadline
[BGH-8]  a newer hold loop (re-arm, resume) supersedes the old one: one release
[BGH-9]  an unreadable battery at the first check passes (the firmware still guards)
[BGH-10] a phase that already passed is never gated again (re-arm mid-room)
[BGH-11] _run_advanced_phase dispatches only after the hold released
[BGH-12] flag off -> no gate at all
[BGH-13] a modifier that changes the room's settings at release raises its need: the
         hold goes on to the new requirement
[BGH-14] a failed cancel at the deadline leaves the run reapable (hold failed,
         watchdog dead), never wedged
[BGH-15] a loop whose job was cancelled while it saved does not send the robot home
[BGH-16] dispatch ends the hold (dispatched_at)
[BGH-17] a phase that needs more than a full battery requires 100
[BGH-18] a modifier that stops matching during the hold lowers the need again
[BGH-19] a room still blocked at a later release attempt is not restored
[BGH-20] the hold target is stored while the hold runs, not only at the release
[BGH-21] a stop_now blocker during a hold leaves it to the gate loop: nothing was
         started, so there is no room to stop and no return_to_base
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from custom_components.eufy_vacuum.adapters.registry import register_adapter_config
from custom_components.eufy_vacuum.jobs import battery_gate as bg
import custom_components.eufy_vacuum.jobs.phase_runner as phase_mod

_REAL_SLEEP = asyncio.sleep
VAC = "vacuum.alfred"
MAP = "Home Map"
START = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)


def _stats(**needs):
    return {"room_baselines": [
        {"map_id": 0, "room_slug": slug, "battery_recent_max": need,
         "battery_clean_sample_count": 5}
        for slug, need in needs.items()
    ]}


STATS = _stats(living_room=14, corridor=3, kids_room=16, guest_room=12)


def _phase(room_id, slug):
    room = {"room_id": room_id, "slug": slug, "clean_mode": "vacuum", "clean_passes": 1}
    return {"phase_type": "room_group", "queue_room_ids": [room_id],
            "resolved_rooms": [room], "room_count": 1}


def _setup(manager, hass, monkeypatch, freezer, *, battery, recheck=False,
           gate=True, phases=None, charge_per_poll=1, battery_steps=None):
    freezer.move_to(START)
    register_adapter_config(VAC, {
        "adapter_id": "test", "source": "test",
        "charging": {"low_battery_threshold_percent": 20},
        "dispatch": {"phase_timing": {"settle_seconds": 0, "verify_seconds": 90,
                                      "confirm_seconds": 15, "poll_seconds": 5,
                                      "max_attempts": 3}},
    })
    hass.states.async_set(VAC, "docked", {"battery_level": battery})
    phases = phases or [_phase(21, "living_room"), _phase(22, "corridor"),
                        _phase(16, "kids_room"), _phase(18, "guest_room")]
    job = {
        "vacuum_entity_id": VAC, "map_id": MAP, "job_id": "gate-test",
        "status": "started", "started_at": START.isoformat(),
        "current_phase_index": 0, "current_room_id": phases[0]["queue_room_ids"][0],
        "queue_room_ids": list(phases[0]["queue_room_ids"]),
        "resolved_rooms": list(phases[0]["resolved_rooms"]),
        "phases": phases, "_phase_dispatch_pending": True,
    }
    if gate:
        job["battery_gate_per_room"] = True
    if recheck:
        job["recheck_rules_per_room"] = True
    manager.data.setdefault("active_jobs", {}).setdefault(VAC, {})[MAP] = job
    monkeypatch.setattr(manager.phase_runner, "_gate_room_stats", lambda vac: STATS)

    calls: list[str] = []

    async def _return_to_base(call):
        calls.append("return_to_base")

    hass.services.async_register("vacuum", "return_to_base", _return_to_base)
    clock = {"seconds": 0, "battery": battery}
    steps = list(battery_steps or [])

    async def tick(seconds):
        clock["seconds"] += seconds
        assert clock["seconds"] < 30 * 3600, "the hold must stay bounded"
        freezer.move_to(START + timedelta(seconds=clock["seconds"]))
        if steps:
            clock["battery"] = steps.pop(0)
        elif clock["battery"] is not None:
            clock["battery"] = min(100, clock["battery"] + charge_per_poll)
        hass.states.async_set(VAC, "docked", {"battery_level": clock["battery"]})

    monkeypatch.setattr(phase_mod.asyncio, "sleep", tick)
    return job, clock, calls


def _gate_state(manager):
    job = manager.get_active_job(vacuum_entity_id=VAC, map_id=MAP)
    return job["phases"][job["current_phase_index"]].get("battery_gate") or {}


async def _gate(manager, idx=0):
    return await manager.phase_runner._battery_gate(
        vacuum_entity_id=VAC, map_id=MAP, phase_index=idx)


async def test_enough_battery_passes_without_a_hold(manager, hass, monkeypatch, freezer):
    """[BGH-1] living room needs 14 -> required 39."""
    _, clock, calls = _setup(manager, hass, monkeypatch, freezer, battery=39)
    assert await _gate(manager) is True
    assert calls == [] and clock["seconds"] == 0
    gate = _gate_state(manager)
    assert gate["passed"] is True and gate["required"] == 39 and not gate.get("held")


async def test_short_battery_holds_until_the_target(manager, hass, monkeypatch, freezer):
    """[BGH-2] remaining run 14+3+16+12 = 45 -> target 70; required 39."""
    _, clock, calls = _setup(manager, hass, monkeypatch, freezer, battery=31)
    assert await _gate(manager) is True
    assert calls == ["return_to_base"]
    gate = _gate_state(manager)
    assert gate["held"] is True and gate["from"] == 31 and gate["required"] == 39
    assert gate["target"] == 70 and gate["released_battery"] == 70
    assert gate["passed"] is True and gate["released_at"]
    assert clock["seconds"] == (70 - 31) * bg.HOLD_POLL_SECONDS


async def test_target_skips_blocked_rooms_and_caps_at_100(manager, hass, monkeypatch, freezer):
    """[BGH-3]"""
    _setup(manager, hass, monkeypatch, freezer, battery=10, recheck=True)

    def _rules(*, vacuum_entity_id, map_id, room_ids):
        included = [r for r in room_ids if r != 16]  # someone is in the kids room
        return {"included_room_ids": included, "blocked_room_ids": [16] if 16 in room_ids else [],
                "blocked_rooms": [], "modified_rooms": []}

    monkeypatch.setattr(manager.run_plan, "evaluate_room_rules", _rules)
    monkeypatch.setattr(manager.run_plan, "phase_after_rules",
                        lambda *, vacuum_entity_id, map_id, phase, decision: phase)
    job = manager.get_active_job(vacuum_entity_id=VAC, map_id=MAP)
    pr = manager.phase_runner
    assert pr._gate_hold_target(VAC, MAP, job, 0) == 14 + 3 + 12 + 25
    monkeypatch.setattr(pr, "_gate_room_stats", lambda vac: _stats(
        living_room=40, corridor=30, kids_room=16, guest_room=30))
    assert pr._gate_hold_target(VAC, MAP, job, 0) == 100


async def test_cancel_during_hold_exits_without_release(manager, hass, monkeypatch, freezer):
    """[BGH-4]"""
    job, clock, _ = _setup(manager, hass, monkeypatch, freezer, battery=31)
    inner = phase_mod.asyncio.sleep

    async def tick(seconds):
        await inner(seconds)
        if clock["seconds"] >= 300:
            job["status"] = "cancelled"

    monkeypatch.setattr(phase_mod.asyncio, "sleep", tick)
    assert await _gate(manager) is False
    assert not _gate_state(manager).get("released_at")


async def test_room_blocked_during_hold_is_skipped(manager, hass, monkeypatch, freezer):
    """[BGH-5]"""
    job, clock, _ = _setup(manager, hass, monkeypatch, freezer, battery=31, recheck=True)
    blocked = {"now": False}

    def _rules(*, vacuum_entity_id, map_id, room_ids):
        if blocked["now"]:
            return {"included_room_ids": [], "blocked_room_ids": list(room_ids),
                    "blocked_rooms": [{"room_id": r, "name": "Living room",
                                       "reason": "blocker"} for r in room_ids],
                    "modified_rooms": []}
        return {"included_room_ids": list(room_ids), "blocked_room_ids": [],
                "blocked_rooms": [], "modified_rooms": []}

    monkeypatch.setattr(manager.run_plan, "evaluate_room_rules", _rules)
    monkeypatch.setattr(manager.run_plan, "phase_after_rules",
                        lambda *, vacuum_entity_id, map_id, phase, decision:
                        None if decision["blocked_room_ids"] else phase)
    advanced: list[int] = []

    async def _advance(*, vacuum_entity_id, map_id):
        advanced.append(1)

    monkeypatch.setattr(manager.phase_runner, "_advance_or_end_run", _advance)
    inner = phase_mod.asyncio.sleep

    async def tick(seconds):
        await inner(seconds)
        if clock["seconds"] >= 120:
            blocked["now"] = True

    monkeypatch.setattr(phase_mod.asyncio, "sleep", tick)
    assert await _gate(manager) is False
    assert advanced == [1]
    stored = manager.get_active_job(vacuum_entity_id=VAC, map_id=MAP)
    skip = stored["rule_skipped_rooms"][-1]
    assert skip["room_id"] == 21 and skip["phase_index"] == 0
    assert skip["skip_reason"] == phase_mod.SKIP_REASON_BATTERY_HOLD
    assert phase_mod.PhaseRunner._phase_stopped_mid_room(stored, 0)  # no learning child


async def test_deadline_releases_with_enough_for_this_room(manager, hass, monkeypatch, freezer):
    """[BGH-6a] charging stalls at 45 (>= required 39, < target 70)."""
    _, clock, _ = _setup(manager, hass, monkeypatch, freezer, battery=31,
                         battery_steps=[45] * 2000)
    assert await _gate(manager) is True
    assert _gate_state(manager)["released_battery"] == 45
    assert clock["seconds"] >= bg.HOLD_TIMEOUT_MINUTES * 60


async def test_deadline_below_required_cancels(manager, hass, monkeypatch, freezer):
    """[BGH-6b]"""
    _setup(manager, hass, monkeypatch, freezer, battery=31, battery_steps=[31] * 2000)
    cancels: list[dict] = []

    async def _cancel(**kw):
        cancels.append(kw)
        manager.get_active_job(vacuum_entity_id=VAC, map_id=MAP)["status"] = "cancelled"

    monkeypatch.setattr(manager.active_job, "async_cancel_active_job", _cancel)
    assert await _gate(manager) is False
    assert cancels[0]["cancel_reason"] == "battery_gate_timeout"


async def test_rearm_keeps_the_original_deadline(manager, hass, monkeypatch, freezer):
    """[BGH-7] a hold persisted 5 h 50 min ago has 10 min left, not 6 h."""
    job, clock, _ = _setup(manager, hass, monkeypatch, freezer, battery=31,
                           battery_steps=[31] * 2000)
    began = START - timedelta(minutes=bg.HOLD_TIMEOUT_MINUTES - 10)
    job["phases"][0]["battery_gate"] = {
        "held": True, "from": 25, "required": 39, "started_at": began.isoformat(),
        "deadline": (began + timedelta(minutes=bg.HOLD_TIMEOUT_MINUTES)).isoformat(),
    }

    async def _cancel(**kw):
        job["status"] = "cancelled"

    monkeypatch.setattr(manager.active_job, "async_cancel_active_job", _cancel)
    assert await _gate(manager) is False
    assert 600 <= clock["seconds"] <= 600 + bg.HOLD_POLL_SECONDS
    assert _gate_state(manager)["from"] == 25


async def test_a_newer_hold_loop_supersedes_the_old_one(manager, hass, monkeypatch, freezer):
    """[BGH-8]"""
    _setup(manager, hass, monkeypatch, freezer, battery=31)
    inner = phase_mod.asyncio.sleep

    async def yielding(seconds):  # let the two loops interleave like real tasks
        await inner(seconds)
        await _REAL_SLEEP(0)

    monkeypatch.setattr(phase_mod.asyncio, "sleep", yielding)
    results = await asyncio.gather(_gate(manager), _gate(manager))
    assert sorted(results) == [False, True]


async def test_unreadable_battery_passes(manager, hass, monkeypatch, freezer):
    """[BGH-9]"""
    _, _, calls = _setup(manager, hass, monkeypatch, freezer, battery=31)
    hass.states.async_set(VAC, "docked", {})
    assert await _gate(manager) is True
    assert calls == [] and _gate_state(manager)["passed"] is True


async def test_a_passed_phase_is_not_gated_again(manager, hass, monkeypatch, freezer):
    """[BGH-10]"""
    job, _, calls = _setup(manager, hass, monkeypatch, freezer, battery=10)
    job["phases"][0]["battery_gate"] = {"passed": True, "required": 39}
    assert await _gate(manager) is True
    assert calls == []


async def test_dispatch_happens_only_after_release(manager, hass, monkeypatch, freezer):
    """[BGH-11]"""
    job, clock, _ = _setup(manager, hass, monkeypatch, freezer, battery=31)
    seen: list[int] = []

    async def _dispatch(*, vacuum_entity_id, map_id, job, attempt):
        seen.append(clock["battery"])
        job_now = manager.get_active_job(vacuum_entity_id=VAC, map_id=MAP)
        job_now["status"] = "cancelled"  # end the watchdog after the first send

    monkeypatch.setattr(manager.phase_runner, "_dispatch_active_phase", _dispatch)
    await manager.phase_runner._run_advanced_phase(
        vacuum_entity_id=VAC, map_id=MAP, phase_index=0, initial=False)
    assert seen and seen[0] >= 70


async def test_flag_off_never_gates(manager, hass, monkeypatch, freezer):
    """[BGH-12]"""
    _, _, calls = _setup(manager, hass, monkeypatch, freezer, battery=5, gate=False)
    assert await _gate(manager) is True
    assert calls == [] and _gate_state(manager) == {}


async def test_a_modifier_at_release_raises_the_need(manager, hass, monkeypatch, freezer):
    """[BGH-13] one pass needs 10 (required 35); three passes need 40 (required 65)."""
    _setup(manager, hass, monkeypatch, freezer, battery=30, recheck=True,
           phases=[_phase(21, "living_room")])
    stats = {"room_stats": [
        {"map_id": 0, "room_slug": "living_room", "effective_mode": "vacuum",
         "clean_times": passes, "is_carpet": False, "clean_intensity": "",
         "edge_mopping": False, "battery_recent_max": need, "battery_clean_sample_count": 5}
        for passes, need in ((1, 10), (3, 40))]}
    monkeypatch.setattr(manager.phase_runner, "_gate_room_stats", lambda vac: stats)
    monkeypatch.setattr(manager.run_plan, "evaluate_room_rules",
                        lambda *, vacuum_entity_id, map_id, room_ids: {
                            "included_room_ids": list(room_ids), "blocked_room_ids": [],
                            "blocked_rooms": [], "modified_rooms": []})

    def _after(*, vacuum_entity_id, map_id, phase, decision):
        rooms = [{**r, "clean_passes": 3} for r in phase["resolved_rooms"]]
        return {**phase, "resolved_rooms": rooms}

    monkeypatch.setattr(manager.run_plan, "phase_after_rules", _after)
    monkeypatch.setattr(phase_mod, "phase_dispatch_fields",
                        lambda decided, **kw: {"queue_room_ids": list(decided["queue_room_ids"]),
                                               "resolved_rooms": decided["resolved_rooms"]})
    assert await _gate(manager) is True
    gate = _gate_state(manager)
    assert gate["required"] == 65 and gate["released_battery"] == 65


async def test_failed_timeout_cancel_leaves_the_run_reapable(manager, hass, monkeypatch, freezer):
    """[BGH-14]"""
    _setup(manager, hass, monkeypatch, freezer, battery=31, battery_steps=[31] * 2000)

    async def _boom(**kw):
        raise RuntimeError("return_to_base failed")

    monkeypatch.setattr(manager.active_job, "async_cancel_active_job", _boom)
    assert await _gate(manager) is False
    stored = manager.get_active_job(vacuum_entity_id=VAC, map_id=MAP)
    assert not bg.battery_hold_active(stored["phases"][0])
    assert stored.get("_phase_watchdog_dead") is True


async def test_a_stale_loop_does_not_send_the_robot_home(manager, hass, monkeypatch, freezer):
    """[BGH-15]"""
    job, _, calls = _setup(manager, hass, monkeypatch, freezer, battery=31)

    async def _save():
        manager.data["active_jobs"][VAC][MAP]["status"] = "cancelled"

    monkeypatch.setattr(manager, "async_save", _save)
    assert await _gate(manager) is False
    assert calls == []


def _stub_wire(manager, monkeypatch, *, pause_before_send=False):
    """The real _dispatch_active_phase, with its device-facing awaits stubbed."""
    sent: list[dict] = []

    async def _noop(*a, **kw):
        return None

    async def _resolve(*a, **kw):
        if pause_before_send:
            manager.data["active_jobs"][VAC][MAP]["status"] = "paused"
        return {"segments": [21]}

    async def _send(*a, **kw):
        sent.append(kw.get("payload"))
        manager.data["active_jobs"][VAC][MAP]["status"] = "cancelled"  # end the watchdog

    monkeypatch.setattr(manager, "_run_global_pre_calls", _noop)
    monkeypatch.setattr(manager.active_job, "apply_per_room_live_settings_awaited", _noop)
    monkeypatch.setattr(manager, "_resolve_live_dispatch_payload", _resolve)
    monkeypatch.setattr(manager, "_dispatch_clean_payload", _send)
    return sent


async def test_dispatch_ends_the_hold(manager, hass, monkeypatch, freezer):
    """[BGH-16] only a command actually sent ends the hold."""
    _setup(manager, hass, monkeypatch, freezer, battery=31)
    sent = _stub_wire(manager, monkeypatch)
    await manager.phase_runner._run_advanced_phase(
        vacuum_entity_id=VAC, map_id=MAP, phase_index=0, initial=False)
    gate = manager.data["active_jobs"][VAC][MAP]["phases"][0]["battery_gate"]
    assert sent and gate["dispatched_at"] and not bg.battery_hold_active({"battery_gate": gate})


async def test_a_pause_before_the_send_keeps_the_hold(manager, hass, monkeypatch, freezer):
    """[BGH-16b]"""
    _setup(manager, hass, monkeypatch, freezer, battery=31)
    sent = _stub_wire(manager, monkeypatch, pause_before_send=True)
    await manager.phase_runner._run_advanced_phase(
        vacuum_entity_id=VAC, map_id=MAP, phase_index=0, initial=False)
    gate = manager.data["active_jobs"][VAC][MAP]["phases"][0]["battery_gate"]
    assert sent == [] and bg.battery_hold_active({"battery_gate": gate})


def _modifier_rules(manager, monkeypatch, *, blocked=(), passes_now):
    """Rules stub: `blocked` rooms are blocked; the phase is rebuilt for the rest with
    `passes_now()` passes (a modifier)."""
    def _rules(*, vacuum_entity_id, map_id, room_ids):
        b = [r for r in room_ids if r in blocked]
        return {"included_room_ids": [r for r in room_ids if r not in blocked],
                "blocked_room_ids": b,
                "blocked_rooms": [{"room_id": r, "name": f"R{r}", "reason": "x"} for r in b],
                "modified_rooms": []}

    def _after(*, vacuum_entity_id, map_id, phase, decision):
        kept = [r for r in phase["resolved_rooms"] if r["room_id"] not in decision["blocked_room_ids"]]
        if not kept:
            return None
        return {**phase, "queue_room_ids": [r["room_id"] for r in kept],
                "resolved_rooms": [{**r, "clean_passes": passes_now()} for r in kept]}

    monkeypatch.setattr(manager.run_plan, "evaluate_room_rules", _rules)
    monkeypatch.setattr(manager.run_plan, "phase_after_rules", _after)
    monkeypatch.setattr(phase_mod, "phase_dispatch_fields",
                        lambda decided, **kw: {"queue_room_ids": list(decided["queue_room_ids"]),
                                               "resolved_rooms": decided["resolved_rooms"]})


_PASS_STATS = {"room_stats": [
    {"map_id": 0, "room_slug": slug, "effective_mode": "vacuum", "clean_times": passes,
     "is_carpet": False, "clean_intensity": "", "edge_mopping": False,
     "battery_recent_max": need, "battery_clean_sample_count": 5}
    for slug in ("living_room", "corridor") for passes, need in ((1, 10), (3, 40))]}


async def test_a_modifier_that_stops_matching_releases_on_the_lower_need(
        manager, hass, monkeypatch, freezer):
    """[BGH-18] 3 passes (65) while the modifier matches; it stops after an hour, and
    the battery (stuck at 60) covers the 1-pass need (35): release, no timeout."""
    _, clock, _ = _setup(manager, hass, monkeypatch, freezer, battery=30, recheck=True,
                         phases=[_phase(21, "living_room")], battery_steps=[60] * 2000)
    monkeypatch.setattr(manager.phase_runner, "_gate_room_stats", lambda vac: _PASS_STATS)
    _modifier_rules(manager, monkeypatch,
                    passes_now=lambda: 3 if clock["seconds"] < 3600 else 1)
    assert await _gate(manager) is True
    gate = _gate_state(manager)
    assert gate["required"] == 35 and gate["released_battery"] == 60
    assert 3600 <= clock["seconds"] < 3700


async def test_a_still_blocked_room_is_not_restored_at_a_later_release(
        manager, hass, monkeypatch, freezer):
    """[BGH-19] group [21, 22]; 22 stays blocked; a modifier keeps 21 above the battery
    for a while. The released phase dispatches 21 only, with one skip record for 22."""
    group = {"phase_type": "room_group", "queue_room_ids": [21, 22], "room_count": 2,
             "resolved_rooms": [
                 {"room_id": 21, "slug": "living_room", "clean_mode": "vacuum", "clean_passes": 1},
                 {"room_id": 22, "slug": "corridor", "clean_mode": "vacuum", "clean_passes": 1}]}
    _, clock, _ = _setup(manager, hass, monkeypatch, freezer, battery=30, recheck=True,
                         phases=[group])
    monkeypatch.setattr(manager.phase_runner, "_gate_room_stats", lambda vac: _PASS_STATS)
    _modifier_rules(manager, monkeypatch, blocked=(22,),
                    passes_now=lambda: 3 if clock["seconds"] < 1800 else 1)
    assert await _gate(manager) is True
    stored = manager.get_active_job(vacuum_entity_id=VAC, map_id=MAP)
    assert stored["queue_room_ids"] == [21]
    assert [s["room_id"] for s in stored["rule_skipped_rooms"]] == [22]
    assert "skip_reason" not in stored["rule_skipped_rooms"][0]  # a boundary skip


async def test_requirement_is_capped_at_a_full_battery(manager, hass, monkeypatch, freezer):
    """[BGH-17]"""
    _setup(manager, hass, monkeypatch, freezer, battery=99)
    monkeypatch.setattr(manager.phase_runner, "_gate_room_stats",
                        lambda vac: _stats(living_room=90))
    rooms = [{"room_id": 21, "slug": "living_room", "clean_mode": "vacuum"}]
    assert manager.phase_runner.battery_gate_required(VAC, MAP, rooms) == 100


async def test_the_target_is_visible_during_the_hold(manager, hass, monkeypatch, freezer):
    """[BGH-20] remaining run 45 -> target 70 from the first poll on."""
    _, clock, _ = _setup(manager, hass, monkeypatch, freezer, battery=31)
    seen: list = []
    inner = phase_mod.asyncio.sleep

    async def tick(seconds):
        await inner(seconds)
        gate = _gate_state(manager)
        if not gate.get("released_at"):
            seen.append(gate.get("target"))

    monkeypatch.setattr(phase_mod.asyncio, "sleep", tick)
    assert await _gate(manager) is True
    assert seen and set(seen) == {70}


async def test_stop_now_leaves_a_held_room_to_the_gate(manager, hass, monkeypatch, freezer):
    """[BGH-21]"""
    job, _, calls = _setup(manager, hass, monkeypatch, freezer, battery=31, recheck=True)
    monkeypatch.setattr(
        manager, "_normalized_managed_rooms_with_automation",
        lambda *, vacuum_entity_id, map_id: {"21": {"on_block_while_cleaning": "stop_now"}})
    monkeypatch.setattr(manager.run_plan, "evaluate_room_rules",
                        lambda *, vacuum_entity_id, map_id, room_ids: {
                            "included_room_ids": [], "blocked_room_ids": list(room_ids),
                            "blocked_rooms": [{"room_id": r, "name": "Living room",
                                               "reason": "blocker"} for r in room_ids],
                            "modified_rooms": []})
    advanced: list[int] = []

    async def _advance(*, vacuum_entity_id, map_id):
        advanced.append(1)

    monkeypatch.setattr(manager.phase_runner, "_advance_or_end_run", _advance)
    job["phases"][0]["battery_gate"] = {"held": True, "from": 31, "required": 39}
    assert await manager.phase_runner.stop_room_blocked_mid_clean(
        vacuum_entity_id=VAC, map_id=MAP) is False
    stored = manager.get_active_job(vacuum_entity_id=VAC, map_id=MAP)
    assert calls == [] and advanced == [] and not stored.get("rule_skipped_rooms")

    # control: the same blocker on a room that is running stops it
    job["phases"][0]["battery_gate"] = {"passed": True}
    assert await manager.phase_runner.stop_room_blocked_mid_clean(
        vacuum_entity_id=VAC, map_id=MAP) is True
    assert calls == ["return_to_base"] and advanced == [1]
