"""A child record carries only the mid-job recharge that happened INSIDE its phase.

The run-wide counters only grow, so copying them made every room after one recharge
look like a recharge sample, and the battery gate would lose all of them.
"""

from __future__ import annotations

from tests.unit.test_phased_job_wiring import (  # noqa: F401  (fixtures)
    _VAC,
    _MAP,
    _job,
    _open,
    runner,
    store,
)


def _finish(runner, job, idx, end_t, *, count, seconds):
    job["observed_mid_job_recharge_count"] = count
    job["recharge_seconds_accumulated"] = seconds
    job["current_phase_index"] = idx
    job["phases"][idx]["_timing_end_t"] = end_t
    runner._record_phase_to_parent(_VAC, _MAP, job)


def test_recharge_counters_are_scoped_to_the_phase(store, runner):
    job = _job()
    job["phases"][0]["room_timing"] = [{"room_id": 5, "cleaning_seconds": 570, "area_m2": 8.0}]
    job["phases"][2]["room_timing"] = [{"room_id": 8, "cleaning_seconds": 300, "area_m2": 5.0}]
    _open(store, job)
    runner._manager.get_active_job = lambda **kw: job

    states: list[dict] = []
    finalizer = runner._manager._get_learning_manager().finalizer
    inner = finalizer.finalize_from_inputs

    def _capture(*, inputs, **kw):
        states.append(dict(inputs["active_job_state"]))
        return inner(inputs=inputs, **kw)

    finalizer.finalize_from_inputs = _capture

    # the firmware recharged once, inside phase 0
    _finish(runner, job, 0, "2026-08-02T18:09:35+00:00", count=1, seconds=600)
    _finish(runner, job, 1, "2026-08-02T18:11:40+00:00", count=1, seconds=600)
    _finish(runner, job, 2, "2026-08-02T18:20:00+00:00", count=1, seconds=600)

    room_children = [s for s in states if s.get("phase_index") in (0, 2)]
    assert [(s["phase_index"], s["observed_mid_job_recharge_count"],
             s["recharge_seconds_accumulated"]) for s in room_children] == [
        (0, 1, 600),
        (2, 0, 0),
    ]


def test_a_held_phase_is_measured_from_its_release(store, runner):
    """battery_gate_per_room: the charge during a hold must not be netted out of the
    room's drain (it would teach 0 % and weaken the gate), and the hold is not the
    room's wall time."""
    job = _job()
    job["phases"][0]["room_timing"] = [{"room_id": 5, "cleaning_seconds": 570, "area_m2": 8.0}]
    job["phases"][2]["room_timing"] = [{"room_id": 8, "cleaning_seconds": 300, "area_m2": 5.0}]
    job["phases"][2]["battery_gate"] = {
        "held": True, "from": 31, "released_battery": 70,
        "released_at": "2026-08-02T19:30:00+00:00", "passed": True,
    }
    _open(store, job)
    runner._manager.get_active_job = lambda **kw: job

    calls: list[dict] = []
    finalizer = runner._manager._get_learning_manager().finalizer
    inner = finalizer.finalize_from_inputs

    def _capture(*, inputs, **kw):
        calls.append({"phase_index": inputs["active_job_state"].get("phase_index"), **kw})
        return inner(inputs=inputs, **kw)

    finalizer.finalize_from_inputs = _capture
    for idx, end_t in ((0, "2026-08-02T18:09:35+00:00"), (1, "2026-08-02T18:11:40+00:00"),
                       (2, "2026-08-02T19:45:00+00:00")):
        job["current_phase_index"] = idx
        job["phases"][idx]["_timing_end_t"] = end_t
        runner._record_phase_to_parent(_VAC, _MAP, job)

    held = next(c for c in calls if c["phase_index"] == 2)
    assert held["battery_start"] == 70
    assert held["started_at"] == "2026-08-02T19:30:00+00:00"
