"""`battery_gate_per_room` rides the same route as `recheck_rules_per_room`.

[BGO-1] a profile saved with the flag dispatches with it True
[BGO-2] a legacy profile record without the key dispatches False
[BGO-3] an explicit argument overrides the saved value, both ways
[BGO-4] the read path normalises a legacy record to False
[BGO-5] save stores the flag; overwrite keeps it when absent and replaces it when given
[BGO-6] the service schemas accept the flag
"""

from __future__ import annotations

from tests.integration.test_run_profile_strict_order import (
    _MAP,
    _VAC,
    _capture_dispatch,
    _stub_profile,
)


async def test_saved_gate_reaches_dispatch(hass, manager, monkeypatch):
    """[BGO-1]"""
    _stub_profile(manager, monkeypatch, profile={"battery_gate_per_room": True})
    seen = _capture_dispatch(manager, monkeypatch)
    await manager.start_run_profile(vacuum_entity_id=_VAC, map_id=_MAP, profile_id="p1")
    assert seen["battery_gate_per_room"] is True


async def test_legacy_profile_dispatches_gate_false(hass, manager, monkeypatch):
    """[BGO-2]"""
    _stub_profile(manager, monkeypatch, profile={"name": "saved long ago"})
    seen = _capture_dispatch(manager, monkeypatch)
    await manager.start_run_profile(vacuum_entity_id=_VAC, map_id=_MAP, profile_id="p1")
    assert seen["battery_gate_per_room"] is False


async def test_explicit_gate_argument_overrides_saved_value(hass, manager, monkeypatch):
    """[BGO-3]"""
    for saved, explicit in ((True, False), (False, True)):
        _stub_profile(manager, monkeypatch, profile={"battery_gate_per_room": saved})
        seen = _capture_dispatch(manager, monkeypatch)
        await manager.start_run_profile(
            vacuum_entity_id=_VAC, map_id=_MAP, profile_id="p1",
            battery_gate_per_room=explicit,
        )
        assert seen["battery_gate_per_room"] is explicit


def test_read_path_normalises_legacy_gate_record(hass, manager):
    """[BGO-4]"""
    enrich = manager.profiles._enrich_saved_run_profile
    assert enrich("p1", {"name": "old"})["battery_gate_per_room"] is False
    assert enrich("p2", {"battery_gate_per_room": True})["battery_gate_per_room"] is True


def test_save_and_overwrite_store_gate(hass, manager):
    """[BGO-5]"""
    from tests.integration.conftest import setup_map

    manager.ensure_vacuum_record(vacuum_entity_id=_VAC)
    setup_map(manager, _VAC, _MAP, count=2, enabled_room_ids=[1, 2])

    def _saved(profile_id):
        return manager.get_saved_run_profiles(
            vacuum_entity_id=_VAC, map_id=_MAP
        )["library"][profile_id]["battery_gate_per_room"]

    saved = manager.save_run_profile(
        vacuum_entity_id=_VAC, map_id=_MAP, name="Daily", battery_gate_per_room=True,
    )
    profile_id = saved["profile_id"]
    assert _saved(profile_id) is True
    manager.overwrite_run_profile(vacuum_entity_id=_VAC, map_id=_MAP, profile_id=profile_id)
    assert _saved(profile_id) is True
    manager.overwrite_run_profile(
        vacuum_entity_id=_VAC, map_id=_MAP, profile_id=profile_id,
        battery_gate_per_room=False,
    )
    assert _saved(profile_id) is False


def test_service_schemas_accept_the_gate():
    """[BGO-6]"""
    from custom_components.eufy_vacuum.services import job_control, run_profiles

    base = {"vacuum_entity_id": _VAC, "battery_gate_per_room": True}
    job_control._START_SELECTED_ROOMS_SCHEMA(dict(base))
    job_control._START_RUN_PROFILE_SCHEMA({**base, "profile_id": "p1"})
    run_profiles._RUN_PROFILE_OVERWRITE_SCHEMA({**base, "profile_id": "p1"})
