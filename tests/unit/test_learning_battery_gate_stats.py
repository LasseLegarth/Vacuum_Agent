"""Room stats: clean per-room battery samples for the per-room battery gate.

A battery sample is clean when the run's ``battery.used`` is known and above zero
and no mid-job recharge happened inside it. A recharge sample measures the charge
gain, not the room (a 13/14/13 % room learned 10 % because one recharge sample
read 0). Timing, area and water keep every sample, as before.
"""

from __future__ import annotations

from tests.unit.test_learning_stats_rebuilder import _job
from tests.unit.test_learning_stats_rebuilder import _make_rebuilder as _rebuilder


def _room_job(job_id: str, day: int, used: float, *, recharge: bool = False, rooms=None) -> dict:
    job = _job(
        job_id=job_id,
        started_at=f"2026-11-{day:02d}T09:00:00+00:00",
        ended_at=f"2026-11-{day:02d}T09:20:00+00:00",
        duration_minutes=20.0,
        battery_used=used,
        room_slugs=rooms or ["living_room"],
    )
    if recharge:
        job["battery"]["mid_job_recharge_observed"] = True
    return job


def _stats(tmp_path, jobs):
    payload = _rebuilder(tmp_path).build_room_stats_payload(
        vacuum_entity_id="vacuum.alfred", jobs=jobs
    )
    exact = {r["room_slug"]: r for r in payload["room_stats"]}
    base = {r["room_slug"]: r for r in payload["room_baselines"]}
    return exact, base


def test_recharge_sample_is_excluded_from_battery_but_not_timing(tmp_path):
    jobs = [
        _room_job("a", 10, 13),
        _room_job("b", 11, 14),
        _room_job("c", 12, 0, recharge=True),
        _room_job("d", 13, 13),
    ]
    exact, base = _stats(tmp_path, jobs)
    for entry in (exact["living_room"], base["living_room"]):
        assert entry["avg_battery_used"] == 13.33
        assert entry["battery_recent"] == [13.0, 14.0, 13.0]
        assert entry["battery_recent_max"] == 14.0
        assert entry["battery_clean_sample_count"] == 3
        # timing still sees all four runs
        assert entry["sample_count"] == 4


def test_zero_used_sample_is_excluded_without_recharge_flag(tmp_path):
    exact, _ = _stats(tmp_path, [_room_job("a", 10, 10), _room_job("b", 11, 0)])
    entry = exact["living_room"]
    assert entry["avg_battery_used"] == 10.0
    assert entry["battery_recent"] == [10.0]
    assert entry["battery_clean_sample_count"] == 1


def test_recent_window_is_the_last_five_by_start_time(tmp_path):
    used = [9, 10, 11, 12, 13, 14, 15]
    jobs = [_room_job(f"j{i}", 10 + i, u) for i, u in enumerate(used)]
    jobs.reverse()  # input order must not matter
    exact, base = _stats(tmp_path, jobs)
    for entry in (exact["living_room"], base["living_room"]):
        assert entry["battery_recent"] == [11.0, 12.0, 13.0, 14.0, 15.0]
        assert entry["battery_recent_max"] == 15.0
        assert entry["battery_clean_sample_count"] == 7


def test_multi_room_run_keeps_its_average_but_gives_no_gate_sample(tmp_path):
    exact, base = _stats(
        tmp_path, [_room_job("a", 10, 20, rooms=["kitchen", "corridor"])]
    )
    for entry in (exact["kitchen"], base["kitchen"], exact["corridor"]):
        assert entry["avg_battery_used"] == 10.0
        assert entry["battery_recent"] == []
        assert entry["battery_recent_max"] is None
        assert entry["battery_clean_sample_count"] == 0


def test_setting_buckets_drop_recharge_battery_only(tmp_path):
    jobs = [_room_job("a", 10, 12), _room_job("b", 11, 0, recharge=True)]
    _, base = _stats(tmp_path, jobs)
    bucket = base["living_room"]["by_clean_times"]["1"]
    assert bucket["avg_battery_used"] == 12.0
    assert bucket["sample_count"] == 2  # minutes keep both runs
