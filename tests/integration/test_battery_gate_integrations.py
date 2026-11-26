"""battery_gate_per_room: the rest of the integration treats an active hold as a
commanded dock, like a charge_wait phase.

[BGI-1] the unplanned mid-job recharge observer does not claim a hold
[BGI-2] the stranded-job reaper does not stamp or reap a held run
[BGI-3] resuming a paused run whose phase is held does not start the robot
[BGI-4] a run with a hold is a break/charge run for the finalizer (drain aggregates)
[BGI-5] the active-job sensor exposes an active hold, with the target it charges to
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.eufy_vacuum.adapters.registry import register_adapter_config
from custom_components.eufy_vacuum.learning import job_finalizer

_VAC = "vacuum.alfred"
_MAP = "Home Map"
_HELD = {"held": True, "from": 19, "required": 39, "started_at": "2026-11-24T10:00:00+00:00"}


def _job(*, held: bool, status: str = "started", started_at: str | None = None) -> dict:
    phase = {"phase_type": "room_group",
             "resolved_rooms": [{"room_id": 21, "slug": "living_room"}], "room_count": 1}
    if held:
        phase["battery_gate"] = dict(_HELD)
    return {
        "vacuum_entity_id": _VAC, "map_id": _MAP, "job_id": "j", "status": status,
        "started_at": started_at or datetime.now(timezone.utc).isoformat(),
        "battery_gate_per_room": True, "current_phase_index": 0, "phases": [phase],
        "queue_room_ids": [21], "paused_at": None,
    }


def _seed(manager, job):
    manager.data.setdefault("active_jobs", {}).setdefault(_VAC, {})[_MAP] = job


def _adapter():
    register_adapter_config(_VAC, {
        "adapter_id": "test", "source": "test",
        "charging": {"low_battery_threshold_percent": 20},
    })


async def test_recharge_observer_ignores_a_hold(hass, manager):
    """[BGI-1] returning at 15 % reads as a low-battery return -- unless held."""
    _adapter()
    hass.states.async_set(_VAC, "returning", {"battery_level": 15})
    _seed(manager, _job(held=False))
    control = manager.active_job.update_active_job_recharge_observation(
        vacuum_entity_id=_VAC, map_id=_MAP)
    assert control.get("pending_mid_job_recharge_return") is True

    _seed(manager, _job(held=True))
    out = manager.active_job.update_active_job_recharge_observation(
        vacuum_entity_id=_VAC, map_id=_MAP)
    assert not out.get("pending_mid_job_recharge_return")
    assert not out.get("observed_mid_job_recharge")


async def test_reaper_leaves_a_held_run_alone(hass, manager):
    """[BGI-2] docked, never armed, dispatched an hour ago: a strand -- unless held."""
    _adapter()
    hass.states.async_set(_VAC, "docked", {"battery_level": 30})
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    _seed(manager, _job(held=False, started_at=hour_ago))
    manager.active_job.poll_stranded_started_job(vacuum_entity_id=_VAC, map_id=_MAP)
    assert manager.data["active_jobs"][_VAC][_MAP].get("stranded_since")

    _seed(manager, _job(held=True, started_at=hour_ago))
    assert manager.active_job.poll_stranded_started_job(
        vacuum_entity_id=_VAC, map_id=_MAP) is None
    assert not manager.data["active_jobs"][_VAC][_MAP].get("stranded_since")


async def test_resume_during_a_hold_does_not_start_the_robot(hass, manager, monkeypatch):
    """[BGI-3]"""
    _adapter()
    started: list[str] = []

    async def _start(call):
        started.append(call.service)

    hass.services.async_register("vacuum", "start", _start)
    monkeypatch.setattr(manager.phase_runner, "rearm_dock_phase_if_needed", lambda **kw: True)
    job = _job(held=True, status="paused")
    job["paused_at"] = datetime.now(timezone.utc).isoformat()
    _seed(manager, job)
    out = await manager.active_job.async_resume_active_job(vacuum_entity_id=_VAC, map_id=_MAP)
    assert out["resumed"] is True
    assert started == []

    # released but not yet sent (settle window): still nothing to resume on the robot
    job = _job(held=True, status="paused")
    job["phases"][0]["battery_gate"]["released_at"] = "2026-11-24T11:00:00+00:00"
    _seed(manager, job)
    await manager.active_job.async_resume_active_job(vacuum_entity_id=_VAC, map_id=_MAP)
    assert started == []


def test_a_held_run_counts_as_a_charge_run_for_the_finalizer():
    """[BGI-4]"""
    held = _job(held=True)
    assert job_finalizer._run_had_charge_wait_phase(held)
    assert job_finalizer._run_had_break_phase(held)
    assert not job_finalizer._run_had_charge_wait_phase(_job(held=False))
    # the held phase's own child is measured from the release: a normal sample
    child = {**held, "phase_index": 0}
    assert not job_finalizer._run_had_charge_wait_phase(child)
    assert not job_finalizer._run_had_break_phase(child)


async def test_the_active_job_sensor_shows_an_active_hold(hass, manager):
    """[BGI-5] HA can show "charging to 39 % before Living room"; nothing once released."""
    from custom_components.eufy_vacuum.sensor.lifecycle import EufyVacuumActiveJobSensor

    sensor = EufyVacuumActiveJobSensor(manager=manager, vacuum_entity_id=_VAC, map_id=_MAP)
    _seed(manager, _job(held=True))
    held = _job(held=True)
    held["phases"][0]["battery_gate"]["target"] = 65
    _seed(manager, held)
    hold = sensor.extra_state_attributes["battery_hold"]
    # room ids come from the resolved rooms: a stored phase need not carry queue_room_ids
    assert hold == {"phase_index": 0, "room_ids": [21], "from": 19, "required": 39,
                    "target": 65, "started_at": "2026-11-24T10:00:00+00:00",
                    "deadline": None}
    sent = _job(held=True)
    sent["phases"][0]["battery_gate"]["dispatched_at"] = "2026-11-24T11:00:00+00:00"
    _seed(manager, sent)
    assert sensor.extra_state_attributes["battery_hold"] is None
