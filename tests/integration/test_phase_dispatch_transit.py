"""A fresh cleaning lifecycle acknowledges dispatch, not arrival at its target.

Transit must not resend a command to an already running robot. Target arrival is
still required before releasing the phase completion guard, with a separate bound.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from custom_components.eufy_vacuum.adapters.registry import register_adapter_config
import custom_components.eufy_vacuum.jobs.phase_runner as phase_mod

VAC = "vacuum.alfred"
MAP = "test"


def setup_replay(manager, hass, monkeypatch, freezer, *, arrival=261, starts=True,
                 stale=False, cancel_at=None):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    freezer.move_to(start - timedelta(seconds=60) if stale else start)
    hass.states.async_set(VAC, "cleaning" if stale else "docked")
    freezer.move_to(start)
    register_adapter_config(VAC, {
        "adapter_id": "test", "source": "test",
        "live_transition": {"native_transition_source": True},
        "dispatch": {"phase_timing": {
            "verify_seconds": 90, "arrival_seconds": 600, "confirm_seconds": 15,
            "poll_seconds": 5, "max_attempts": 3,
        }},
    })
    job = {
        "vacuum_entity_id": VAC, "map_id": MAP, "job_id": "transit-test",
        "status": "started", "started_at": start.isoformat(),
        "current_room_started_at": start.isoformat(), "current_room_id": 1,
        "current_phase_index": 0, "queue_room_ids": [1],
        "phases": [{"phase_type": "room_group", "queue_room_ids": [1],
                    "resolved_rooms": [{"room_id": 1, "slug": "target"}]}],
        "resolved_rooms": [{"room_id": 1, "slug": "target"}],
        "_phase_dispatch_pending": True,
    }
    manager.data.setdefault("active_jobs", {}).setdefault(VAC, {})[MAP] = job
    clock = {"seconds": 0}

    async def tick(seconds):
        clock["seconds"] += seconds
        assert clock["seconds"] < 2000, "Watchdog must remain bounded"
        freezer.move_to(start + timedelta(seconds=clock["seconds"]))
        if starts:
            hass.states.async_set(VAC, "cleaning")
        if cancel_at is not None and clock["seconds"] >= cancel_at:
            job["status"] = "cancelled"

    monkeypatch.setattr(phase_mod.asyncio, "sleep", tick)
    monkeypatch.setattr(manager.active_job, "native_current_room_target_id",
                        lambda *args: 1 if arrival is not None and clock["seconds"] >= arrival else 2)
    dispatch = AsyncMock()
    cancel = AsyncMock()
    monkeypatch.setattr(manager.phase_runner, "_dispatch_active_phase", dispatch)
    monkeypatch.setattr(manager.active_job, "async_cancel_active_job", cancel)
    return job, clock, dispatch, cancel


async def run(manager):
    await manager.phase_runner._run_advanced_phase(
        vacuum_entity_id=VAC, map_id=MAP, phase_index=0, initial=True)


async def test_slow_native_arrival_does_not_redispatch(manager, hass, monkeypatch, freezer):
    job, clock, dispatch, cancel = setup_replay(manager, hass, monkeypatch, freezer)
    await run(manager)
    assert dispatch.await_count == 0
    assert job["_phase_dispatch_pending"] is False
    assert 261 <= clock["seconds"] < 300
    assert not job.get("completed_room_ids")  # Arrival is not room completion.
    cancel.assert_not_awaited()


async def test_ignored_dispatch_keeps_original_start_deadline(manager, hass, monkeypatch, freezer):
    job, clock, dispatch, cancel = setup_replay(
        manager, hass, monkeypatch, freezer, arrival=None, starts=False)
    await run(manager)
    assert dispatch.await_count == 2
    assert clock["seconds"] < 300
    assert job.get("_phase_watchdog_dead") is True
    cancel.assert_not_awaited()


async def test_stale_cleaning_is_not_dispatch_acknowledgement(manager, hass, monkeypatch, freezer):
    job, clock, dispatch, cancel = setup_replay(
        manager, hass, monkeypatch, freezer, arrival=None, starts=False, stale=True)
    await run(manager)
    assert dispatch.await_count == 2
    assert clock["seconds"] < 300
    assert job.get("_phase_watchdog_dead") is True
    cancel.assert_not_awaited()


async def test_stale_target_room_is_not_a_new_start(manager, hass, monkeypatch, freezer):
    job, clock, dispatch, cancel = setup_replay(
        manager, hass, monkeypatch, freezer, arrival=0, starts=False, stale=True)
    await run(manager)
    assert dispatch.await_count == 2
    assert job.get("_phase_watchdog_dead") is True
    cancel.assert_not_awaited()


async def test_accepted_run_never_arrives_cancels_without_resending(manager, hass, monkeypatch, freezer):
    job, clock, dispatch, cancel = setup_replay(
        manager, hass, monkeypatch, freezer, arrival=None)
    await run(manager)
    dispatch.assert_not_awaited()
    cancel.assert_awaited_once()
    assert cancel.await_args.kwargs["cancel_reason"] == "phase_arrival_timeout"
    assert 590 <= clock["seconds"] <= 610
    assert job["_phase_dispatch_pending"] is True  # Never falsely confirm target.


async def test_user_cancel_during_transit_stops_watcher(manager, hass, monkeypatch, freezer):
    job, clock, dispatch, cancel = setup_replay(
        manager, hass, monkeypatch, freezer, arrival=None, cancel_at=40)
    await run(manager)
    dispatch.assert_not_awaited()
    cancel.assert_not_awaited()
    assert clock["seconds"] == 40
    assert job["status"] == "cancelled"


async def test_arrival_budget_is_adapter_owned(manager, hass, monkeypatch, freezer):
    setup_replay(manager, hass, monkeypatch, freezer)
    assert manager._phase_timing(VAC)["verify_seconds"] == 90
    assert manager._phase_timing(VAC)["arrival_seconds"] == 600


async def test_arrival_timeout_cancel_failure_is_reapable(manager, hass, monkeypatch, freezer):
    job, clock, dispatch, cancel = setup_replay(
        manager, hass, monkeypatch, freezer, arrival=None)
    cancel.side_effect = RuntimeError("return-to-dock unavailable")
    await run(manager)
    assert job.get("_phase_watchdog_dead") is True
    assert job.get("_phase_dispatch_pending_since")
    dispatch.assert_not_awaited()
