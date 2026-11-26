"""continue_without_docking: a room that ends on the way home sends the next room at once.

[CWD-1]  returning + job-active off -> the advance is scheduled once, runs after the
         settle for the rest of the poll, from_returning, and stamps end_boundary
[CWD-2]  returning + job-active on (a recharge-resume) -> nothing
[CWD-3]  option off, or an adapter that cannot show the end before the dock (no
         require_job_active_clear, no arrival bound, a dock service) -> nothing
[CWD-4]  the next phase is a charge stop, or this is the last room -> nothing
[CWD-5]  the current room not confirmed yet (dispatch pending) or never armed -> nothing
[CWD-6]  a cancel in flight, a stop_now room or a battery hold -> nothing
[CWD-7]  job-active unavailable or missing -> nothing (only an explicit off is a finish)
[CWD-8]  the robot is docked or paused -> nothing (the dock completion does it)
[CWD-9]  recheck: a blocked next room is looked past without side effects; a charge
         stop after it -> nothing; an allowed room after it -> ready
[CWD-10] the next room is the dock room -> nothing (its long post-dock settle)
[CWD-11] the run moved on during the settle -> dropped, no advance
[CWD-12] _run_advanced_phase from_returning: sent at once, no settle; confirmed
[CWD-13] the robot ignored it while returning -> waited for on the dock, then the normal
         settle and attempts
[CWD-14] from_returning but already docked -> the normal settle path
[CWD-15] the lifecycle listener asks the phase runner on a not-done tick
[CWD-16] the option rides the profile and the retry like battery_gate_per_room
[CWD-17] a pause or a newer watchdog during the dock wait stops the old one: no second send
[CWD-18] a scheduled continuation is cancelled on unload and does nothing on a closed manager
[CWD-19] the robot docks while the send is being prepared -> nothing is sent on the way;
         the normal settle and attempts follow
[CWD-20] a send error on the way home also waits for the dock before the normal attempts
[CWD-21] the option on an adapter that cannot do it is logged once per run
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.eufy_vacuum.adapters.registry import register_adapter_config
import custom_components.eufy_vacuum.jobs.phase_runner as phase_mod

_REAL_SLEEP = asyncio.sleep
VAC = "vacuum.alfred"
MAP = "Home Map"
JOB_ACTIVE = "binary_sensor.alfred_cleaning"


def _adapter(**over):
    cfg = {
        "adapter_id": "test", "source": "test",
        "entities": {"job_active": JOB_ACTIVE},
        "completion": {"require_job_active_clear": True},
        "charging": {"low_battery_threshold_percent": 20},
        "dispatch": {"phase_timing": {"settle_seconds": 10, "dock_settle_seconds": 45,
                                      "verify_seconds": 90, "arrival_seconds": 600,
                                      "confirm_seconds": 15, "poll_seconds": 5,
                                      "max_attempts": 3}},
    }
    cfg.update(over)
    return cfg


def _phase(room_id, slug):
    room = {"room_id": room_id, "slug": slug, "clean_mode": "vacuum", "clean_passes": 1}
    return {"phase_type": "room_group", "queue_room_ids": [room_id],
            "resolved_rooms": [room], "room_count": 1}


def _setup(manager, hass, *, phases=None, adapter=None, vac="returning", active="off", **job_over):
    register_adapter_config(VAC, adapter or _adapter())
    phases = phases or [_phase(21, "living_room"), _phase(22, "corridor"), _phase(16, "kids_room")]
    job = {
        "vacuum_entity_id": VAC, "map_id": MAP, "job_id": "cwd-test", "status": "started",
        "started_at": "2026-11-26T09:00:00+00:00", "current_phase_index": 0,
        "current_room_id": 21, "queue_room_ids": [21], "phases": phases,
        "has_observed_active_lifecycle": True, "_phase_dispatch_pending": False,
        "continue_without_docking": True,
    }
    job.update(job_over)
    manager.data.setdefault("active_jobs", {}).setdefault(VAC, {})[MAP] = job
    hass.states.async_set(VAC, vac, {"battery_level": 80})
    if active is not None:
        hass.states.async_set(JOB_ACTIVE, active)
    return job


def _ready(manager):
    return manager.phase_runner.continue_without_docking_ready(vacuum_entity_id=VAC, map_id=MAP)


def _stored(manager):
    return manager.get_active_job(vacuum_entity_id=VAC, map_id=MAP)


@pytest.fixture
def fast_sleep(monkeypatch):
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)
        await _REAL_SLEEP(0)

    monkeypatch.setattr(phase_mod.asyncio, "sleep", _sleep)
    return slept


async def test_room_end_on_the_way_home_advances_once(manager, hass, monkeypatch, fast_sleep):
    """[CWD-1]"""
    _setup(manager, hass)
    assert _ready(manager)
    advanced: list[bool] = []

    async def _advance(*, vacuum_entity_id, map_id, from_returning=False):
        advanced.append(from_returning)
        return True

    monkeypatch.setattr(manager.phase_runner, "maybe_advance_phase", _advance)
    runner = manager.phase_runner
    assert runner.maybe_continue_without_docking(vacuum_entity_id=VAC, map_id=MAP) is True
    # the other entities of the same poll fire too: scheduled once
    assert runner.maybe_continue_without_docking(vacuum_entity_id=VAC, map_id=MAP) is False
    await hass.async_block_till_done()
    assert advanced == [True]
    assert fast_sleep[0] == phase_mod._CWD_SETTLE_SECONDS
    assert _stored(manager)["phases"][0]["end_boundary"] == "returning"
    assert runner._cwd_scheduled == set()


async def test_a_recharge_return_is_not_a_finish(manager, hass):
    """[CWD-2]"""
    _setup(manager, hass, active="on")
    assert not _ready(manager)


@pytest.mark.parametrize("adapter_over", [
    {"completion": {}},
    {"dispatch": {"phase_timing": {"settle_seconds": 10}}},
    {"vocabulary": {"hard_service_states": ["washing"]}},
])
async def test_an_adapter_that_cannot_show_the_end_is_left_alone(manager, hass, adapter_over):
    """[CWD-3]"""
    _setup(manager, hass, adapter=_adapter(**adapter_over))
    assert not _ready(manager)


async def test_option_off_is_left_alone(manager, hass):
    """[CWD-3]"""
    _setup(manager, hass, continue_without_docking=False)
    assert not _ready(manager)


async def test_a_charge_stop_or_the_last_room_docks_as_today(manager, hass):
    """[CWD-4]"""
    charge = {"phase_type": "charge_wait", "target_battery": 50}
    _setup(manager, hass, phases=[_phase(21, "living_room"), charge, _phase(16, "kids_room")])
    assert not _ready(manager)
    _setup(manager, hass, phases=[_phase(21, "living_room")])
    assert not _ready(manager)


async def test_an_unconfirmed_or_unarmed_room_is_left_alone(manager, hass):
    """[CWD-5]"""
    _setup(manager, hass, _phase_dispatch_pending=True)
    assert not _ready(manager)
    _setup(manager, hass, has_observed_active_lifecycle=False)
    assert not _ready(manager)


async def test_cancel_stop_now_and_hold_are_left_alone(manager, hass):
    """[CWD-6]"""
    _setup(manager, hass, _cancel_in_flight=True)
    assert not _ready(manager)
    _setup(manager, hass, rule_skipped_rooms=[
        {"room_id": 21, "phase_index": 0, "skip_reason": phase_mod.SKIP_REASON_MID_ROOM}])
    assert not _ready(manager)
    job = _setup(manager, hass)
    job["phases"][0]["battery_gate"] = {"held": True}
    assert not _ready(manager)


@pytest.mark.parametrize("active", ["unavailable", "unknown", None])
async def test_only_an_explicit_off_is_a_finish(manager, hass, active):
    """[CWD-7]"""
    _setup(manager, hass, active=active)
    assert not _ready(manager)


@pytest.mark.parametrize("vac", ["docked", "paused", "cleaning", "error"])
async def test_not_returning_is_left_to_the_dock(manager, hass, vac):
    """[CWD-8]"""
    _setup(manager, hass, vac=vac)
    assert not _ready(manager)


async def test_recheck_looks_past_a_blocked_room_without_side_effects(manager, hass, monkeypatch):
    """[CWD-9]"""
    def _rules(*, vacuum_entity_id, map_id, room_ids):
        blocked = [r for r in room_ids if r == 22]
        return {"included_room_ids": [r for r in room_ids if r != 22],
                "blocked_room_ids": blocked,
                "blocked_rooms": [{"room_id": r, "name": "Corridor", "reason": "x"} for r in blocked],
                "modified_rooms": []}

    monkeypatch.setattr(manager.run_plan, "evaluate_room_rules", _rules)
    monkeypatch.setattr(manager.run_plan, "phase_after_rules",
                        lambda *, vacuum_entity_id, map_id, phase, decision:
                        None if decision["blocked_room_ids"] else phase)
    events: list = []
    hass.bus.async_listen("eufy_vacuum_room_skipped", lambda e: events.append(e))

    job = _setup(manager, hass, recheck_rules_per_room=True)
    assert _ready(manager)  # corridor blocked, kids_room allowed
    charge = {"phase_type": "charge_wait", "target_battery": 50}
    _setup(manager, hass, recheck_rules_per_room=True,
           phases=[_phase(21, "living_room"), _phase(22, "corridor"), charge,
                   _phase(16, "kids_room")])
    assert not _ready(manager)
    await hass.async_block_till_done()
    assert events == []
    assert not job.get("rule_skipped_rooms")


async def test_a_dock_room_target_docks_first(manager, hass):
    """[CWD-10]"""
    _setup(manager, hass)
    manager.data.setdefault("maps", {}).setdefault(VAC, {}).setdefault(MAP, {}).setdefault(
        "rooms", {})["22"] = {"is_dock_room": True}
    assert not _ready(manager)


async def test_a_run_that_moved_on_during_the_settle_is_dropped(
        manager, hass, monkeypatch, fast_sleep):
    """[CWD-11]"""
    job = _setup(manager, hass)
    advanced: list = []

    async def _advance(**kw):
        advanced.append(kw)
        return True

    async def _sleep(seconds):
        job["current_phase_index"] = 1  # the dock completion advanced meanwhile
        await _REAL_SLEEP(0)

    monkeypatch.setattr(manager.phase_runner, "maybe_advance_phase", _advance)
    monkeypatch.setattr(phase_mod.asyncio, "sleep", _sleep)
    assert manager.phase_runner.maybe_continue_without_docking(vacuum_entity_id=VAC, map_id=MAP)
    await hass.async_block_till_done()
    assert advanced == []
    assert "end_boundary" not in job["phases"][0]


def _watchdog(manager, monkeypatch, confirms, on_dispatch=None):
    sent: list[int] = []

    async def _dispatch(*, vacuum_entity_id, map_id, job, attempt, **kw):
        sent.append(attempt)
        if on_dispatch:
            on_dispatch(len(sent))

    async def _started(**kw):
        return confirms.pop(0)

    monkeypatch.setattr(manager.phase_runner, "_dispatch_active_phase", _dispatch)
    monkeypatch.setattr(manager.phase_runner, "_await_phase_started", _started)
    return sent


async def test_sent_at_once_while_returning(manager, hass, monkeypatch, fast_sleep):
    """[CWD-12]"""
    _setup(manager, hass, current_phase_index=1, _phase_dispatch_pending=True)
    sent = _watchdog(manager, monkeypatch, [True])
    await manager.phase_runner._run_advanced_phase(
        vacuum_entity_id=VAC, map_id=MAP, phase_index=1, from_returning=True)
    assert sent == [1] and fast_sleep == []
    assert not _stored(manager).get("_phase_dispatch_pending")


async def test_ignored_while_returning_falls_back_to_the_dock(
        manager, hass, monkeypatch, fast_sleep):
    """[CWD-13]"""
    _setup(manager, hass, current_phase_index=1, _phase_dispatch_pending=True)
    sent = _watchdog(manager, monkeypatch, [False, True])
    polls = {"n": 0}

    async def _sleep(seconds):
        fast_sleep.append(seconds)
        polls["n"] += 1
        if polls["n"] == 3:
            hass.states.async_set(VAC, "docked", {"battery_level": 80})
        await _REAL_SLEEP(0)

    monkeypatch.setattr(phase_mod.asyncio, "sleep", _sleep)
    await manager.phase_runner._run_advanced_phase(
        vacuum_entity_id=VAC, map_id=MAP, phase_index=1, from_returning=True)
    # one send on the way, three dock polls, the normal settle, then attempt 1 again
    assert sent == [1, 1]
    assert fast_sleep == [5, 5, 5, 10]
    assert not _stored(manager).get("_phase_dispatch_pending")


async def test_already_docked_takes_the_normal_path(manager, hass, monkeypatch, fast_sleep):
    """[CWD-14]"""
    _setup(manager, hass, vac="docked", current_phase_index=1, _phase_dispatch_pending=True)
    sent = _watchdog(manager, monkeypatch, [True])
    await manager.phase_runner._run_advanced_phase(
        vacuum_entity_id=VAC, map_id=MAP, phase_index=1, from_returning=True)
    assert sent == [1] and fast_sleep == [10]


async def test_the_lifecycle_listener_asks_the_phase_runner(manager, hass, monkeypatch):
    """[CWD-15]"""
    from custom_components.eufy_vacuum.listeners import lifecycle

    manager.ensure_vacuum_record(vacuum_entity_id=VAC)
    _setup(manager, hass, vac="returning", active="on")
    asked: list = []
    monkeypatch.setattr(manager.phase_runner, "maybe_continue_without_docking",
                        lambda **kw: asked.append(kw) or False)

    async def _no_external(*, vacuum_entity_id):
        return False

    monkeypatch.setattr(manager, "maybe_handle_external_run", _no_external)
    monkeypatch.setattr(manager, "get_known_map_ids", lambda vac: [MAP])
    await hass.async_block_till_done()
    lifecycle.register(hass)
    hass.states.async_set(JOB_ACTIVE, "off")
    await hass.async_block_till_done()
    lifecycle.remove(hass)
    assert {"vacuum_entity_id": VAC, "map_id": MAP} in asked


async def test_the_option_rides_the_profile_and_the_retry(hass, manager, monkeypatch):
    """[CWD-16]"""
    from custom_components.eufy_vacuum.services import job_control, run_profiles
    from tests.integration.test_run_profile_strict_order import (
        _MAP, _VAC, _capture_dispatch, _stub_profile,
    )

    _stub_profile(manager, monkeypatch, profile={"continue_without_docking": True})
    seen = _capture_dispatch(manager, monkeypatch)
    await manager.start_run_profile(vacuum_entity_id=_VAC, map_id=_MAP, profile_id="p1")
    assert seen["continue_without_docking"] is True
    _stub_profile(manager, monkeypatch, profile={"name": "saved long ago"})
    seen = _capture_dispatch(manager, monkeypatch)
    await manager.start_run_profile(vacuum_entity_id=_VAC, map_id=_MAP, profile_id="p1")
    assert seen["continue_without_docking"] is False
    seen = _capture_dispatch(manager, monkeypatch)
    await manager.start_run_profile(vacuum_entity_id=_VAC, map_id=_MAP, profile_id="p1",
                                    continue_without_docking=True)
    assert seen["continue_without_docking"] is True

    enrich = manager.profiles._enrich_saved_run_profile
    assert enrich("p1", {"name": "old"})["continue_without_docking"] is False

    base = {"vacuum_entity_id": _VAC, "continue_without_docking": True}
    job_control._START_SELECTED_ROOMS_SCHEMA(dict(base))
    job_control._START_RUN_PROFILE_SCHEMA({**base, "profile_id": "p1"})
    run_profiles._RUN_PROFILE_OVERWRITE_SCHEMA({**base, "profile_id": "p1"})
    run_profiles._RUN_PROFILE_NAME_SCHEMA({**base, "name": "Daily", "map_id": _MAP})


@pytest.mark.parametrize("change", ["pause", "newer_watchdog"])
async def test_a_pause_or_newer_watchdog_stops_the_dock_wait(
        manager, hass, monkeypatch, fast_sleep, change):
    """[CWD-17]"""
    job = _setup(manager, hass, current_phase_index=1, _phase_dispatch_pending=True)
    sent = _watchdog(manager, monkeypatch, [False, True, True])
    runner = manager.phase_runner

    async def _sleep(seconds):
        fast_sleep.append(seconds)
        if change == "pause":
            # paused on one poll, resumed on the next: the resume's own watchdog sends
            job["status"] = "paused" if len(fast_sleep) == 1 else "started"
        else:
            runner._watchdog_gen[(VAC, MAP, 1)] = runner._watchdog_gen.get((VAC, MAP, 1), 0) + 1
        await _REAL_SLEEP(0)

    monkeypatch.setattr(phase_mod.asyncio, "sleep", _sleep)
    await runner._run_advanced_phase(
        vacuum_entity_id=VAC, map_id=MAP, phase_index=1, from_returning=True)
    assert sent == [1]


async def test_a_continuation_dies_with_its_manager(manager, hass, monkeypatch, fast_sleep):
    """[CWD-18]"""
    _setup(manager, hass)
    advanced: list = []

    async def _advance(**kw):
        advanced.append(kw)
        return True

    monkeypatch.setattr(manager.phase_runner, "maybe_advance_phase", _advance)
    runner = manager.phase_runner
    assert runner.maybe_continue_without_docking(vacuum_entity_id=VAC, map_id=MAP)
    for task in runner.cancel_all():
        assert task.cancelled() or task.cancelling()
    await hass.async_block_till_done()
    assert advanced == [] and runner._cwd_scheduled == set()

    async def _sleep(seconds):
        manager._closed = True  # unloaded during the settle
        await _REAL_SLEEP(0)

    monkeypatch.setattr(phase_mod.asyncio, "sleep", _sleep)
    assert runner.maybe_continue_without_docking(vacuum_entity_id=VAC, map_id=MAP)
    await hass.async_block_till_done()
    manager._closed = False
    assert advanced == []


async def test_docked_during_the_send_preparation_sends_nothing_on_the_way(
        manager, hass, monkeypatch, fast_sleep):
    """[CWD-19]"""
    _setup(manager, hass, current_phase_index=1, _phase_dispatch_pending=True)
    wire: list = []

    async def _pre_calls(**kw):
        hass.states.async_set(VAC, "docked", {"battery_level": 80})  # docks meanwhile

    async def _noop(*a, **kw):
        return None

    async def _payload(**kw):
        return {"segments": [22]}

    async def _send(*, vacuum_entity_id, payload):
        wire.append(payload)

    monkeypatch.setattr(manager, "_run_global_pre_calls", _pre_calls)
    monkeypatch.setattr(manager.active_job, "apply_per_room_live_settings_awaited", _noop)
    monkeypatch.setattr(manager, "_resolve_live_dispatch_payload", _payload)
    monkeypatch.setattr(manager, "_dispatch_clean_payload", _send)
    confirms = [True]

    async def _started(**kw):
        return confirms.pop(0)

    monkeypatch.setattr(manager.phase_runner, "_await_phase_started", _started)
    await manager.phase_runner._run_advanced_phase(
        vacuum_entity_id=VAC, map_id=MAP, phase_index=1, from_returning=True)
    # nothing on the way; the normal settle, then one send from the dock
    assert wire == [{"segments": [22]}] and fast_sleep == [10]


async def test_a_send_error_on_the_way_waits_for_the_dock(manager, hass, monkeypatch, fast_sleep):
    """[CWD-20]"""
    from homeassistant.exceptions import HomeAssistantError

    _setup(manager, hass, current_phase_index=1, _phase_dispatch_pending=True)
    sent: list[int] = []

    async def _dispatch(*, vacuum_entity_id, map_id, job, attempt, **kw):
        sent.append(attempt)
        if len(sent) == 1:
            raise HomeAssistantError("transient")

    async def _started(**kw):
        return True

    async def _sleep(seconds):
        fast_sleep.append(seconds)
        if len(fast_sleep) == 2:
            hass.states.async_set(VAC, "docked", {"battery_level": 80})
        await _REAL_SLEEP(0)

    monkeypatch.setattr(manager.phase_runner, "_dispatch_active_phase", _dispatch)
    monkeypatch.setattr(manager.phase_runner, "_await_phase_started", _started)
    monkeypatch.setattr(phase_mod.asyncio, "sleep", _sleep)
    await manager.phase_runner._run_advanced_phase(
        vacuum_entity_id=VAC, map_id=MAP, phase_index=1, from_returning=True)
    assert sent == [1, 1] and fast_sleep == [5, 5, 10]


async def test_an_unsupported_adapter_is_logged_once(manager, hass, caplog):
    """[CWD-21]"""
    _setup(manager, hass, adapter=_adapter(completion={}))
    runner = manager.phase_runner
    for _ in range(3):
        assert not runner.maybe_continue_without_docking(vacuum_entity_id=VAC, map_id=MAP)
    hits = [r for r in caplog.records if "continue_without_docking" in r.getMessage()]
    assert len(hits) == 1 and hits[0].levelname == "WARNING"
