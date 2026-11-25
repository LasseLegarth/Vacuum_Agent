"""battery_gate_per_room at job start (start_selected_rooms).

[BGS-1] enough battery for the first room -> dispatched as before, phase 0 stamped
        passed (a later re-arm must not hold a running room), initial watchdog
[BGS-2] short battery -> nothing is sent to the robot, the job starts held: phases
        attached, dispatch guard set, the watchdog spawned non-initial (it holds, then
        dispatches), and no room_started event for a room that has not started
[BGS-3] a one-room run gets one phase when the gate is on (an atomic run has no
        driver to hold it), and stays atomic when it is off
[BGS-4] a multi-room batch (strict order off) stays atomic: the gate is strict-order
"""

from __future__ import annotations

import pytest

from custom_components.eufy_vacuum.adapters.registry import (
    register_adapter_config,
    unregister_adapter_config,
)
from custom_components.eufy_vacuum.const import EVENT_ROOM_STARTED

from tests.brand_catalogs import adapter_config
from tests.integration.test_manager_start_selected import (
    _MAP,
    _VAC,
    _register_dispatch,
    _seed_enabled,
)


@pytest.fixture
def roborock_like(manager, hass, monkeypatch):
    register_adapter_config(_VAC, adapter_config(**{
        "adapter_id": "rb", "source": "code",
        "capabilities": {"honors_clean_order": False},
        "dispatch": {"template": "roborock_segment_clean", "passes_max": 3},
        "charging": {"low_battery_threshold_percent": 20},
    }))
    spawned: list[dict] = []

    def _capture(coro, *a, **k):
        frame = getattr(coro, "cr_frame", None)
        if frame is not None and coro.__name__ == "_run_advanced_phase":
            spawned.append(dict(frame.f_locals))
        coro.close()

    monkeypatch.setattr(hass, "async_create_task", _capture)
    monkeypatch.setattr(manager.phase_runner, "_gate_room_stats", lambda vac: None)
    yield spawned
    unregister_adapter_config(_VAC)


async def _start(manager, hass, *, battery, rooms=2, gate=True, strict=True):
    manager.ensure_vacuum_record(vacuum_entity_id=_VAC)
    _seed_enabled(manager, count=rooms)
    hass.states.async_set(_VAC, "docked", {"battery_level": battery})
    calls = _register_dispatch(hass)
    events: list = []
    hass.bus.async_listen(EVENT_ROOM_STARTED, lambda e: events.append(e))
    result = await manager.start_selected_rooms(
        vacuum_entity_id=_VAC, map_id=_MAP, strict_order=strict, battery_gate_per_room=gate)
    await hass.async_block_till_done()
    assert result["started"] is True, (result.get("reason"), result.get("message"))
    return manager.data["active_jobs"][_VAC][_MAP], calls, events


async def test_enough_battery_dispatches_and_stamps_passed(manager, hass, roborock_like):
    """[BGS-1] no stats -> 20 % fallback need -> required 45."""
    job, calls, _ = await _start(manager, hass, battery=45)
    assert len(calls) == 1
    assert job["phases"][0]["battery_gate"]["passed"] is True
    assert job["battery_gate_per_room"] is True
    assert [s["initial"] for s in roborock_like] == [True]


async def test_short_battery_starts_the_job_held(manager, hass, roborock_like, monkeypatch):
    """[BGS-2] the parked robot still gets its saved per-room settings written."""
    writes: list = []

    async def _write(**kw):
        writes.append(kw)

    monkeypatch.setattr(manager, "_run_settings_write", _write)
    job, calls, events = await _start(manager, hass, battery=44)
    assert len(writes) == 1
    assert calls == []
    assert job["status"] == "started" and len(job["phases"]) == 2
    assert job["_phase_dispatch_pending"] is True
    assert "battery_gate" not in job["phases"][0]
    assert [(s["phase_index"], s["initial"]) for s in roborock_like] == [(0, False)]
    assert events == []


async def test_one_room_run_gets_a_phase_only_with_the_gate(manager, hass, roborock_like):
    """[BGS-3]"""
    job, _, _ = await _start(manager, hass, battery=90, rooms=1)
    assert len(job["phases"]) == 1


async def test_one_room_run_without_the_gate_stays_atomic(manager, hass, roborock_like):
    """[BGS-3]"""
    job, _, _ = await _start(manager, hass, battery=90, rooms=1, gate=False)
    assert not job.get("phases")


async def test_a_batch_without_strict_order_stays_atomic(manager, hass, roborock_like):
    """[BGS-4]"""
    job, calls, _ = await _start(manager, hass, battery=30, rooms=3, strict=False)
    assert not job.get("phases") and len(calls) == 1
