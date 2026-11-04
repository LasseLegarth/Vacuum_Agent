"""End-to-end Roborock harness: dispatch -> live current_room rollover -> completion.

Exercises the Wave 1-3 pieces TOGETHER through the real adapter + manager +
lifecycle listener against stubbed device services — the integration proof the
per-piece unit tests can't give. It pins, among other things, the on-the-wire
app_segment_clean params shape (list-wrapped), which no unit test asserted and
which was wrong until this harness caught it.

Flow:
  1. dispatch (start_selected_rooms) -> app_segment_clean params=[{segments,repeat}]
     with live-resolved ids; the queue-first room's fan is pushed live (per-room
     fan via vacuum.set_fan_speed — passes is global, mop is unsettable).
  2. run starts -> lifecycle marks has_observed_active_lifecycle.
  3. native rollover follows current_room through the device's optimized order
     as a live pointer, pushing each room's fan without treating pointer changes
     as completion proof; transit rooms ignored.
  4. completion (dock: cleaning binary off + charging) -> finalization fires via
     the require_job_active_clear gate.

Coverage targets (ISSUE #19, recheck_rules_per_room)
----------------------------------------------------
[RB-RECHECK-0] option off: a blocker turning true mid-run changes nothing (regression)
[RB-RECHECK-1] a room blocked during an earlier room is not dispatched; room_skipped
               names the rule, the job ends interrupted, run_incomplete lists the room
[RB-RECHECK-2] the rooms after a skipped room are dispatched in order
[RB-RECHECK-3] a room blocked at start but allowed when reached is cleaned
[RB-RECHECK-4] a charge stop with no allowed room after it is not run
[RB-RECHECK-5] a room blocked during a charge: the charge poller's advance ends the run
[RB-RECHECK-6] retry_missed_rooms re-dispatches the skipped rooms, re-checking again
[RB-RECHECK-7] an atomic run with the option on is identical to today
"""

from __future__ import annotations

import pytest

from homeassistant.core import ServiceCall, SupportsResponse

from custom_components.eufy_vacuum.adapters.registry import clear_registry
from custom_components.eufy_vacuum.adapters.roborock import adapter as rb
from custom_components.eufy_vacuum.listeners import job_metrics, lifecycle


_VAC = "vacuum.ivy"
_MAP = "Main floor"
# Two TARGETS (Kitchen, Office); Dining Room is the dock room and NOT a target.
_KITCHEN, _OFFICE, _DINING = 16, 19, 17


class _FakeDevice:
    manufacturer = "Roborock"

    def __init__(self, model="roborock.vacuum.s6"):
        self.model = model


def _setup(hass, manager, monkeypatch, model="roborock.vacuum.s6"):
    clear_registry()
    monkeypatch.setattr(rb, "_device_for_vacuum", lambda h, v: _FakeDevice(model))

    # Entity surface (docked at the dock room before the run).
    hass.states.async_set(_VAC, "docked", {"supported_features": 30524, "fan_speed": "max"})
    hass.states.async_set("select.ivy_selected_map", _MAP)
    hass.states.async_set("sensor.ivy_status", "charging")
    hass.states.async_set("binary_sensor.ivy_cleaning", "off")
    hass.states.async_set("binary_sensor.ivy_charging", "on")
    hass.states.async_set("sensor.ivy_battery", "100")
    hass.states.async_set("sensor.ivy_current_room", "Dining Room")
    # The mop-intensity select must EXIST, as it does on real hardware: a global
    # pre-call whose target is missing now refuses rather than silently no-opping
    # (issue #51 — HA warns instead of raising on a missing service target, so the
    # safety abort could never fire and the robot kept the vendor app's water).
    hass.states.async_set(
        "select.ivy_mop_intensity", "off",
        {"options": ["off", "low", "medium", "high"]},
    )
    rb.register_roborock_adapter_for_vacuum(hass, _VAC)
    manager.ensure_vacuum_record(vacuum_entity_id=_VAC)

    # Managed rooms via the real onboarding path (seed discovery -> save), which
    # confirms floor types so the run isn't blocked as onboarding_required. Only
    # Kitchen + Office are enabled targets; Dining Room is the dock room.
    discovered = [
        {"room_id": _KITCHEN, "map_id": _MAP, "name": "KITCHEN", "slug": "kitchen"},
        {"room_id": _OFFICE, "map_id": _MAP, "name": "Office", "slug": "office"},
    ]
    manager.data.setdefault("discovery", {}).setdefault(_VAC, {})[_MAP] = {
        "active_map_id": _MAP, "rooms": discovered,
    }
    manager.save_managed_rooms(
        vacuum_entity_id=_VAC, map_id=_MAP, enabled_room_ids=[_KITCHEN, _OFFICE]
    )
    rooms = manager.data["maps"][_VAC][_MAP]["rooms"]

    # RB-1 REGRESSION GUARD — assert the SHIPPED defaults before overwriting them.
    #
    # This overwrite is what masked RB-1. Room creation used to hardcode Eufy display
    # literals, so every Roborock room was born with fan_speed "Max" / water_level "Off" /
    # clean_intensity "Quick" — none of which is in this brand's vocabulary. The test then
    # immediately replaced fan_speed with valid values, and the e2e went green over a room
    # the real onboarding path could never have produced.
    #
    # The overwrite still earns its place below (two rooms need DIFFERENT fans to prove
    # per-room live pushing), so it stays — but not before the defaults it hides are
    # checked. See tests/adapters/test_adapter_contract.py for the brand-agnostic version.
    for _rid in (_KITCHEN, _OFFICE):
        _fresh = rooms[str(_rid)]
        assert _fresh["fan_speed"] == "balanced", (
            f"a new Roborock room was created with fan_speed {_fresh['fan_speed']!r} — "
            "brand defaults regressed to another vocabulary"
        )
        # UNCONDITIONAL, and briefly was not. This was made model-dependent on the
        # reasoning that the S6 declares no `water_level_options`, so storing a water
        # level looked like the inert field the clean_intensity assertion below forbids.
        # The two are NOT the same case. Roborock HAS a water axis; it withholds the
        # OPTION LIST on models whose mop is unsettable, which is a per-model capability
        # statement. Roborock has no intensity axis at all, which is a per-brand one.
        # `declared_profile_fields` is the single authority on the second, and it reads
        # the brand's PROFILES -- so emptying them to satisfy a contract test makes every
        # room save drop the field from stored rooms. See the note in the adapter's
        # room_profiles block.
        assert _fresh["water_level"] == "off"
        assert not _fresh.get("clean_intensity"), (
            "Roborock exposes no intensity axis; a new room must not store one"
        )
        assert _fresh["profile_name"] == "vacuum_quick"

    rooms[str(_KITCHEN)]["order"] = 1   # queue order; device will path-optimize
    rooms[str(_OFFICE)]["order"] = 2
    # Per-room fan (valid Roborock vocab values) -> pushed live on room entry.
    rooms[str(_KITCHEN)]["fan_speed"] = "turbo"
    rooms[str(_OFFICE)]["fan_speed"] = "quiet"


def _stub_services(hass) -> dict[str, list]:
    calls: dict[str, list] = {"send": [], "fan": [], "mop": []}

    async def _send(call: ServiceCall):
        calls["send"].append(dict(call.data))

    async def _fan(call: ServiceCall):
        calls["fan"].append(dict(call.data))

    async def _mop(call: ServiceCall):
        calls["mop"].append(dict(call.data))

    async def _get_maps(call: ServiceCall):
        return {"maps": [{"flag": 0, "name": _MAP, "rooms": {
            "16": "KITCHEN", "17": "Dining Room", "19": "Office",
        }}]}

    hass.services.async_register("vacuum", "send_command", _send)
    hass.services.async_register("vacuum", "set_fan_speed", _fan)
    hass.services.async_register("select", "select_option", _mop)
    hass.services.async_register(
        "roborock", "get_maps", _get_maps, supports_response=SupportsResponse.ONLY
    )
    return calls


def _job(manager):
    return manager.data["active_jobs"][_VAC][_MAP]


async def _tick_rollover(manager, hass, current_room_name):
    """Simulate the device reporting a room + the 5s progress tick driving rollover.

    SNAP-2: get_job_progress_snapshot is a pure read now (no rollover mutation) —
    apply_job_progress_tick is the explicit call that actually rolls the room, exactly
    what listeners/job_progress.py's real 5s ticker calls in production.
    """
    hass.states.async_set("sensor.ivy_current_room", current_room_name)
    await hass.async_block_till_done()
    manager.apply_job_progress_tick(vacuum_entity_id=_VAC, map_id=_MAP)
    await hass.async_block_till_done()


async def test_roborock_dispatch_rollover_completion(hass, manager, monkeypatch):
    _setup(hass, manager, monkeypatch)
    calls = _stub_services(hass)

    finalize_calls: list[dict] = []

    async def _finalize(**kwargs):
        finalize_calls.append(kwargs)
        return {"job_id": "j1", "completed_job": {}}

    async def _no_external(*, vacuum_entity_id):
        return False

    monkeypatch.setattr(manager, "finalize_learning_for_active_job", _finalize)
    monkeypatch.setattr(manager, "maybe_handle_external_run", _no_external)

    lifecycle.register(hass)

    # --- 1. DISPATCH -------------------------------------------------------
    result = await manager.start_selected_rooms(vacuum_entity_id=_VAC, map_id=_MAP)
    await hass.async_block_till_done()
    assert result["started"] is True, (result.get("reason"), result.get("message"))

    assert len(calls["send"]) == 1
    send = calls["send"][0]
    assert send["entity_id"] == _VAC
    assert send["command"] == "app_segment_clean"
    # THE wire-shape assertion: params LIST-wrapped, live-resolved segments, queue order.
    assert send["params"] == [{"segments": [_KITCHEN, _OFFICE], "repeat": 1}]
    # Per-room fan: dispatch seeds the queue-first room's fan (Kitchen -> turbo).
    # No mop pre-call (the S6 mop is unsettable).
    assert [c["fan_speed"] for c in calls["fan"]] == ["turbo"]
    assert calls["mop"] == []

    assert _job(manager)["status"] == "started"

    # --- 2. RUN STARTS -> lifecycle marks active ---------------------------
    hass.states.async_set("binary_sensor.ivy_cleaning", "on")
    hass.states.async_set("sensor.ivy_status", "segment_cleaning")
    await hass.async_block_till_done()
    assert _job(manager).get("has_observed_active_lifecycle") is True
    assert finalize_calls == []  # mid-run, no completion

    # --- 3. NATIVE ROLLOVER (device order: Office first, then Kitchen) -----
    await _tick_rollover(manager, hass, "Office")
    assert _job(manager)["current_room_id"] == _OFFICE          # adopted, no phantom-complete
    assert _job(manager)["completed_room_ids"] == []
    assert calls["fan"][-1]["fan_speed"] == "quiet"             # Office fan pushed live

    await _tick_rollover(manager, hass, "KITCHEN")
    assert _job(manager)["completed_room_ids"] == []             # pointer move != completion
    assert _job(manager)["current_room_id"] == _KITCHEN
    assert calls["fan"][-1]["fan_speed"] == "turbo"             # Kitchen fan pushed live

    # transit room (a non-target the robot crosses) is ignored
    await _tick_rollover(manager, hass, "LIVINGROOM")
    assert _job(manager)["current_room_id"] == _KITCHEN          # unchanged
    assert calls["fan"][-1]["fan_speed"] == "turbo"             # no new fan call
    assert finalize_calls == []                                  # still running

    # --- 4. COMPLETION (dock: current_room reverts, cleaning off + charging) ---
    hass.states.async_set("sensor.ivy_current_room", "Dining Room")  # dock room, not a target
    hass.states.async_set("sensor.ivy_status", "charging")
    hass.states.async_set("binary_sensor.ivy_cleaning", "off")
    await hass.async_block_till_done()

    # The require_job_active_clear gate finalized once (charging + cleaning off
    # + has_observed). current_room reverting to the non-target dock room did NOT
    # trigger a spurious rollover.
    assert len(finalize_calls) == 1


# ===========================================================================
# Stepped run (native in-queue charge): Kitchen -> charge_wait -> Office.
#
# The phase-interleave + charge-poller logic is covered generically in
# test_charge_wait_phase; THESE prove the pieces ride the REAL Roborock adapter
# end to end — a stepped run dispatches app_segment_clean ONCE PER GROUP with the
# charge break between (vac -> charge -> vac), which the flat harness above can't
# show. Roborock is the whole point of the charge step: an S6 re-docks + charges
# after each group anyway, so "charge to X% before the next group" is near-free.
# ===========================================================================

# [{room_group Kitchen}, {charge to 90%}, {room_group Office}] — the stash shape
# start_run_profile builds from a saved profile's steps (profiles.run_profile_steps).
_STEPPED = [
    {"type": "room_group", "rooms": [{"room_id": _KITCHEN}]},
    {"type": "charge_wait", "target_battery_percent": 90},
    {"type": "room_group", "rooms": [{"room_id": _OFFICE}]},
]


async def _start_stepped(hass, manager, monkeypatch, *, steps=None, model="roborock.vacuum.s6"):
    """Setup + kick a stepped run by seeding ``_pending_run_steps`` — exactly what
    ``start_run_profile`` stashes after applying a profile — then dispatching through
    the real adapter. Returns (calls, result).

    The charge poller + phase watchdog run as background tasks; we close them so the
    test drives dispatch synchronously (both are covered in test_charge_wait_phase /
    test_strict_order_phase_timing). Without this the initial watchdog would hit real
    ``asyncio.sleep`` verify windows and hang.
    """
    _setup(hass, manager, monkeypatch, model=model)
    calls = _stub_services(hass)

    async def _finalize(**kwargs):
        return {"job_id": "j1", "completed_job": {}}

    async def _no_external(*, vacuum_entity_id):
        return False

    monkeypatch.setattr(manager, "finalize_learning_for_active_job", _finalize)
    monkeypatch.setattr(manager, "maybe_handle_external_run", _no_external)
    if not hass.services.has_service("vacuum", "return_to_base"):
        hass.services.async_register("vacuum", "return_to_base", lambda call: None)
    monkeypatch.setattr(hass, "async_create_task", lambda coro, *a, **k: coro.close())

    manager.data.setdefault("_pending_run_steps", {}).setdefault(_VAC, {})[_MAP] = list(steps or _STEPPED)
    result = await manager.start_selected_rooms(vacuum_entity_id=_VAC, map_id=_MAP)
    await hass.async_block_till_done()
    return calls, result


async def test_roborock_stepped_dispatch_builds_charge_phase(hass, manager, monkeypatch):
    """[RB-STEP-1] A stepped profile (Kitchen -> charge_wait -> Office) builds a 3-phase
    [room, charge_wait, room] job and dispatches app_segment_clean for GROUP 0 (Kitchen)
    ONLY — Office waits behind the charge, and the charge break is never a phantom clean."""
    calls, result = await _start_stepped(hass, manager, monkeypatch)
    assert result["started"] is True, (result.get("reason"), result.get("message"))

    job = _job(manager)
    assert [p.get("phase_type") for p in job["phases"]] == [
        "room_group", "charge_wait", "room_group",
    ]
    assert job["phases"][1]["target_battery_percent"] == 90
    assert job["current_phase_index"] == 0

    # Phase 0 dispatched Kitchen ONLY, params list-wrapped, live-resolved id.
    assert len(calls["send"]) == 1
    assert calls["send"][0]["command"] == "app_segment_clean"
    assert calls["send"][0]["params"] == [{"segments": [_KITCHEN], "repeat": 1}]
    # No mop pre-call on the S6 (mop unsettable) even in a stepped run.
    assert calls["mop"] == []


async def test_roborock_stepped_resumes_next_group_after_charge(hass, manager, monkeypatch):
    """[RB-STEP-2] After the charge_wait phase, advancing re-dispatches app_segment_clean
    for the NEXT group (Office) through the real adapter — the vac->charge->vac RESUME.
    Proves per-group re-dispatch, not just the first group."""
    from custom_components.eufy_vacuum.queue.queue_engine import advance_active_job_phase

    calls, _ = await _start_stepped(hass, manager, monkeypatch)

    # Walk the job to the Office phase (index 2) as the completion hook + charge poller
    # would (phase 0 clean done -> charge_wait -> target reached -> Office).
    job = _job(manager)
    for _ in range(2):
        job = advance_active_job_phase(job)
        assert job is not None
        manager.data["active_jobs"][_VAC][_MAP] = job
    assert job["current_phase_index"] == 2

    calls["send"].clear()
    calls["fan"].clear()
    await manager.phase_runner._dispatch_active_phase(
        vacuum_entity_id=_VAC, map_id=_MAP, job=job, attempt=1,
    )

    assert len(calls["send"]) == 1
    assert calls["send"][0]["command"] == "app_segment_clean"
    assert calls["send"][0]["params"] == [{"segments": [_OFFICE], "repeat": 1}]
    # Office's per-room fan (quiet) is pushed before its segment on the resume.
    assert calls["fan"][-1]["fan_speed"] == "quiet"


async def test_roborock_settable_mop_per_group_water(hass, manager, monkeypatch):
    """[RB-STEP-3] On a SETTABLE-mop model (S7), a per-group vac->mop run pushes the mop
    intensity select PER PHASE from each group's own water_level: Kitchen dry (off) then
    Kitchen mopped (high). Proves the global_pre_calls dispatch (Wave 2) + per-phase
    re-derivation (Wave 1) together. The S6 gets none of this (mop unsettable)."""
    from custom_components.eufy_vacuum.queue.queue_engine import advance_active_job_phase

    # Same room twice — a vacuum pass, a dry-hold wait, then a mop pass at high water.
    # clean_mode is the logical mop switch (vacuum forces water off; vacuum_mop keeps it).
    mop_steps = [
        {"type": "room_group", "rooms": [{"room_id": _KITCHEN, "clean_mode": "vacuum"}]},
        {"type": "wait", "wait_minutes": 1},
        {"type": "room_group",
         "rooms": [{"room_id": _KITCHEN, "clean_mode": "vacuum_mop", "water_level": "high"}]},
    ]
    calls, result = await _start_stepped(
        hass, manager, monkeypatch, steps=mop_steps, model="roborock.vacuum.a15"
    )
    assert result["started"] is True, (result.get("reason"), result.get("message"))

    # Phase 0 (vacuum): mop intensity set OFF before the Kitchen segment.
    assert calls["mop"], "settable model should push a mop pre-call"
    assert calls["mop"][-1]["entity_id"] == "select.ivy_mop_intensity"
    assert calls["mop"][-1]["option"] == "off"

    # Walk to the mop phase (index 2) and re-dispatch through the real adapter.
    job = _job(manager)
    for _ in range(2):
        job = advance_active_job_phase(job)
        assert job is not None
        manager.data["active_jobs"][_VAC][_MAP] = job
    assert job["current_phase_index"] == 2

    calls["mop"].clear()
    await manager.phase_runner._dispatch_active_phase(
        vacuum_entity_id=_VAC, map_id=_MAP, job=job, attempt=1,
    )
    # Phase 2 (mop): intensity re-derived from THIS group -> HIGH (per-phase, not the
    # whole-run max that would have mopped the vacuum pass too).
    assert calls["mop"][-1]["option"] == "high"


async def test_roborock_stepped_forces_strict_order(hass, manager, monkeypatch):
    """[RB-STEP-4] A stepped run WITH STOPS forces strict order. Roborock ignores clean
    order inside one app_segment_clean (honors_clean_order False), so a 2-room group would
    otherwise be path-optimized; with stops present it must run in the EXACT order shown.
    The 2-room first group therefore splits into two per-room phases and phase 0 dispatches
    Kitchen ALONE — not app_segment_clean [Kitchen, Office]."""
    steps = [
        {"type": "room_group", "rooms": [{"room_id": _KITCHEN}, {"room_id": _OFFICE}]},
        {"type": "wait", "wait_minutes": 1},
        {"type": "room_group", "rooms": [{"room_id": _KITCHEN}]},
    ]
    calls, result = await _start_stepped(hass, manager, monkeypatch, steps=steps)
    assert result["started"] is True, (result.get("reason"), result.get("message"))

    job = _job(manager)
    # The 2-room first group SPLIT into two per-room phases (strict) before the wait —
    # non-strict would have been a single [room, wait, room] with both rooms in phase 0.
    assert [p.get("phase_type") for p in job["phases"]] == [
        "room_group", "room_group", "wait", "room_group",
    ]
    # Phase 0 dispatched Kitchen ALONE (queue order 1), not [Kitchen, Office].
    assert len(calls["send"]) == 1
    assert calls["send"][0]["params"] == [{"segments": [_KITCHEN], "repeat": 1}]


# ===========================================================================
# ISSUE #19: recheck_rules_per_room. Each room phase of a sequenced run re-checks
# its blocker rules right before it is dispatched.
#
# These drive the REAL watchdog, lifecycle listener and learning finalizer against a
# simulated device: a segment clean puts the robot in that room cleaning, and
# _finish_room docks it again (the phase-complete signal). The watchdog timing is
# zeroed so each spawned dispatch runs to completion inside async_block_till_done.
# ===========================================================================

_BEDROOM = 21
_NAMES = {_KITCHEN: "KITCHEN", _OFFICE: "Office", _BEDROOM: "Bedroom"}
_KID_HOME = {
    "id": "b_kid", "kind": "blocker", "entity_id": "person.kid",
    "operator": "equals", "value": "home", "enabled": True,
    "effect": {"reason": "kid_home"},
}


def _recheck_setup(
    hass, manager, monkeypatch, freezer, *, blocked_rooms=(_OFFICE,), three=False,
):
    """Kitchen (dock, order 1) -> Office (2) [-> Bedroom (3)], with the person.kid
    blocker on ``blocked_rooms``. Returns (calls, events)."""
    from custom_components.eufy_vacuum.const import DATA_LEARNING, DOMAIN
    from custom_components.eufy_vacuum.learning.manager import LearningManager

    clear_registry()
    monkeypatch.setattr(rb, "_device_for_vacuum", lambda h, v: _FakeDevice())
    if three:
        _orig_save = manager.save_managed_rooms

        def _save(**kwargs):
            manager.data["discovery"][_VAC][_MAP]["rooms"].append(
                {"room_id": _BEDROOM, "map_id": _MAP, "name": "Bedroom", "slug": "bedroom"}
            )
            kwargs["enabled_room_ids"] = [_KITCHEN, _OFFICE, _BEDROOM]
            return _orig_save(**kwargs)

        monkeypatch.setattr(manager, "save_managed_rooms", _save)
    _setup(hass, manager, monkeypatch)
    rooms = manager.data["maps"][_VAC][_MAP]["rooms"]
    rooms[str(_KITCHEN)]["is_dock_room"] = True
    rooms[str(_KITCHEN)]["grants_access_to"] = [_OFFICE] + ([_BEDROOM] if three else [])
    if three:
        rooms[str(_BEDROOM)]["order"] = 3
        rooms[str(_BEDROOM)]["fan_speed"] = "balanced"
    for rid in blocked_rooms:
        rooms[str(rid)]["rules"] = [dict(_KID_HOME)]
    hass.states.async_set("person.kid", "not_home")

    calls = _stub_services(hass)
    hass.services.async_remove("vacuum", "send_command")
    hass.services.async_remove("roborock", "get_maps")

    async def _send(call: ServiceCall):
        # The simulated robot: a segment clean starts cleaning that room.
        calls["send"].append(dict(call.data))
        freezer.tick(60)
        rid = call.data["params"][0]["segments"][0]
        hass.states.async_set(_VAC, "cleaning", {"supported_features": 30524})
        hass.states.async_set("binary_sensor.ivy_cleaning", "on")
        hass.states.async_set("sensor.ivy_status", "segment_cleaning")
        hass.states.async_set("sensor.ivy_current_room", _NAMES[rid])
        hass.states.async_set("sensor.ivy_cleaning_time", "0")   # per-run counters
        hass.states.async_set("sensor.ivy_cleaning_area", "0")

    async def _get_maps(call: ServiceCall):
        return {"maps": [{"flag": 0, "name": _MAP, "rooms": {
            str(rid): name for rid, name in {**_NAMES, _DINING: "Dining Room"}.items()
        }}]}

    hass.services.async_register("vacuum", "send_command", _send)
    hass.services.async_register(
        "roborock", "get_maps", _get_maps, supports_response=SupportsResponse.ONLY
    )
    calls["dock"] = []
    hass.services.async_register(
        "vacuum", "return_to_base", lambda call: calls["dock"].append(dict(call.data))
    )

    hass.data[DOMAIN][DATA_LEARNING] = LearningManager(hass)

    async def _no_external(*, vacuum_entity_id):
        return False

    monkeypatch.setattr(manager, "maybe_handle_external_run", _no_external)
    monkeypatch.setattr(manager, "_phase_timing", lambda _vac: {
        "settle_seconds": 0, "dock_settle_seconds": 0, "verify_seconds": 0,
        "confirm_seconds": 0, "poll_seconds": 0, "max_attempts": 1,
    })

    events: dict[str, list] = {"skipped": [], "finished": [], "incomplete": []}
    for name, key in (
        ("eufy_vacuum_room_skipped", "skipped"),
        ("eufy_vacuum_job_finished", "finished"),
        ("eufy_vacuum_run_incomplete", "incomplete"),
    ):
        hass.bus.async_listen(name, lambda e, k=key: events[k].append(dict(e.data)))
    lifecycle.register(hass)
    job_metrics.register(hass)
    return calls, events


async def _finish_room(hass, freezer):
    """The robot cleans for a while, finishes its room and docks: the phase-complete
    signal. The counters are what credits the room as cleaned. The clock moves so each
    phase's counter slice (second-resolution timestamps) is its own."""
    freezer.tick(240)
    hass.states.async_set("sensor.ivy_cleaning_time", "4")
    hass.states.async_set("sensor.ivy_cleaning_area", "6")
    await hass.async_block_till_done()
    hass.states.async_set(_VAC, "docked", {"supported_features": 30524})
    hass.states.async_set("sensor.ivy_current_room", "Dining Room")
    hass.states.async_set("sensor.ivy_status", "charging")
    hass.states.async_set("binary_sensor.ivy_cleaning", "off")
    await hass.async_block_till_done()


def _segments(calls):
    """Every segment sent to the robot, in dispatch order."""
    return [rid for c in calls["send"] for rid in c["params"][0]["segments"]]


async def test_recheck_skips_room_blocked_during_earlier_room(hass, manager, monkeypatch, freezer):
    """[RB-RECHECK-1] Kitchen -> Office, strict + recheck. person.kid comes home while
    Kitchen is cleaning; Office's blocker now matches, so Office is never dispatched.
    room_skipped names the rule, the job finishes interrupted and run_incomplete lists
    Office. A rule skip says nothing about whether the robot can reach the room, so
    the trouble-rooms log does not count it as a miss."""
    calls, events = _recheck_setup(hass, manager, monkeypatch, freezer)

    result = await manager.start_selected_rooms(
        vacuum_entity_id=_VAC, map_id=_MAP, strict_order=True, recheck_rules_per_room=True,
    )
    await hass.async_block_till_done()
    assert result["started"] is True, (result.get("reason"), result.get("message"))
    assert _segments(calls) == [_KITCHEN]

    hass.states.async_set("person.kid", "home")
    await _finish_room(hass, freezer)

    assert _segments(calls) == [_KITCHEN]
    assert [(e["room_id"], e["reason"], e["triggered_rule_id"]) for e in events["skipped"]] == [
        (_OFFICE, "rule_blocked", "b_kid"),
    ]
    assert [e["status"] for e in events["finished"]] == ["interrupted"]
    assert [e["missed_room_ids"] for e in events["incomplete"]] == [[_OFFICE]]
    trouble = hass.data["eufy_vacuum"]["learning"].get_trouble_rooms_log(vacuum_entity_id=_VAC)
    assert trouble["rooms"].get(str(_OFFICE), {}).get("miss_count", 0) == 0


async def test_recheck_off_dispatches_as_today(hass, manager, monkeypatch, freezer):
    """[RB-RECHECK-0] Regression guard: without recheck_rules_per_room the rules are
    decided once at start. Office's blocker turning true mid-run changes nothing:
    both rooms are dispatched, no room_skipped, the job completes, no run_incomplete."""
    calls, events = _recheck_setup(hass, manager, monkeypatch, freezer)

    result = await manager.start_selected_rooms(
        vacuum_entity_id=_VAC, map_id=_MAP, strict_order=True,
    )
    await hass.async_block_till_done()
    assert result["started"] is True, (result.get("reason"), result.get("message"))

    hass.states.async_set("person.kid", "home")
    await _finish_room(hass, freezer)
    assert _segments(calls) == [_KITCHEN, _OFFICE]
    await _finish_room(hass, freezer)

    assert events["skipped"] == []
    assert [e["status"] for e in events["finished"]] == ["completed"]
    assert events["incomplete"] == []


async def test_recheck_continues_with_the_rooms_after_a_skip(hass, manager, monkeypatch, freezer):
    """[RB-RECHECK-2] Kitchen -> Office -> Bedroom, strict + recheck. Office becomes
    blocked during Kitchen: it is skipped and the run goes on to Bedroom, in order."""
    calls, events = _recheck_setup(hass, manager, monkeypatch, freezer, three=True)

    result = await manager.start_selected_rooms(
        vacuum_entity_id=_VAC, map_id=_MAP, strict_order=True, recheck_rules_per_room=True,
    )
    await hass.async_block_till_done()
    assert result["started"] is True, (result.get("reason"), result.get("message"))

    hass.states.async_set("person.kid", "home")
    await _finish_room(hass, freezer)
    assert _segments(calls) == [_KITCHEN, _BEDROOM]
    assert [e["room_id"] for e in events["skipped"]] == [_OFFICE]
    assert events["finished"] == []

    await _finish_room(hass, freezer)
    assert _segments(calls) == [_KITCHEN, _BEDROOM]
    assert [e["status"] for e in events["finished"]] == ["interrupted"]
    assert [e["missed_room_ids"] for e in events["incomplete"]] == [[_OFFICE]]


async def test_recheck_cleans_a_room_blocked_at_start_once_allowed(hass, manager, monkeypatch, freezer):
    """[RB-RECHECK-3] Office is blocked when the run starts but allowed by the time the
    robot reaches it: with recheck it stays in the run and is cleaned. Nothing was
    skipped, so the job completes. (The start preflight is unchanged: a reduced run
    still needs its confirmation.)"""
    calls, events = _recheck_setup(hass, manager, monkeypatch, freezer)
    hass.states.async_set("person.kid", "home")

    result = await manager.start_selected_rooms(
        vacuum_entity_id=_VAC, map_id=_MAP, strict_order=True, recheck_rules_per_room=True,
        confirm_reduced_run=True,
    )
    await hass.async_block_till_done()
    assert result["started"] is True, (result.get("reason"), result.get("message"))
    assert _segments(calls) == [_KITCHEN]

    hass.states.async_set("person.kid", "not_home")
    await _finish_room(hass, freezer)
    assert _segments(calls) == [_KITCHEN, _OFFICE]
    await _finish_room(hass, freezer)

    assert events["skipped"] == []
    assert [e["status"] for e in events["finished"]] == ["completed"]
    assert events["incomplete"] == []


async def test_recheck_does_not_run_a_stop_with_no_room_after_it(hass, manager, monkeypatch, freezer):
    """[RB-RECHECK-4] Stepped run Kitchen -> charge to 90% -> Office, recheck on, battery
    at 50%. Office becomes blocked during Kitchen, so nothing is left after the charge:
    the charge is not run and the job ends as soon as Kitchen docks, interrupted, with
    Office missed."""
    calls, events = _recheck_setup(hass, manager, monkeypatch, freezer)
    hass.states.async_set("sensor.ivy_battery", "50")
    manager.data.setdefault("_pending_run_steps", {}).setdefault(_VAC, {})[_MAP] = list(_STEPPED)

    result = await manager.start_selected_rooms(
        vacuum_entity_id=_VAC, map_id=_MAP, recheck_rules_per_room=True,
    )
    await hass.async_block_till_done()
    assert result["started"] is True, (result.get("reason"), result.get("message"))

    hass.states.async_set("person.kid", "home")
    await _finish_room(hass, freezer)

    assert _segments(calls) == [_KITCHEN]
    assert calls["dock"] == []
    assert [e["room_id"] for e in events["skipped"]] == [_OFFICE]
    assert [e["status"] for e in events["finished"]] == ["interrupted"]
    assert [e["missed_room_ids"] for e in events["incomplete"]] == [[_OFFICE]]


async def test_recheck_ends_the_run_when_the_room_after_a_charge_is_blocked(
    hass, manager, monkeypatch, freezer,
):
    """[RB-RECHECK-5] Stepped run Kitchen -> charge to 90% -> Office, recheck on. Office
    is allowed when Kitchen docks, so the charge runs; person.kid comes home while the
    robot charges. When the charge is done Office is skipped and the run ends right
    there (the charge poller's advance finalizes it), interrupted, Office missed."""
    import asyncio

    calls, events = _recheck_setup(hass, manager, monkeypatch, freezer)
    hass.states.async_set("sensor.ivy_battery", "50")
    manager.data.setdefault("_pending_run_steps", {}).setdefault(_VAC, {})[_MAP] = list(_STEPPED)
    real_sleep = asyncio.sleep

    async def _charging(seconds, *args, **kwargs):
        if seconds == 30.0:  # the charge poller's battery poll
            hass.states.async_set("person.kid", "home")
            hass.states.async_set("sensor.ivy_battery", "90")
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _charging)

    result = await manager.start_selected_rooms(
        vacuum_entity_id=_VAC, map_id=_MAP, recheck_rules_per_room=True,
    )
    await hass.async_block_till_done()
    assert result["started"] is True, (result.get("reason"), result.get("message"))

    await _finish_room(hass, freezer)

    assert len(calls["dock"]) == 1          # the charge ran
    assert _segments(calls) == [_KITCHEN]
    assert [e["room_id"] for e in events["skipped"]] == [_OFFICE]
    assert [e["status"] for e in events["finished"]] == ["interrupted"]
    assert [e["missed_room_ids"] for e in events["incomplete"]] == [[_OFFICE]]


async def test_retry_missed_rooms_rechecks_per_room_again(hass, manager, monkeypatch, freezer):
    """[RB-RECHECK-6] Office and Bedroom are skipped by their rules; run_incomplete lists
    both. retry_missed_rooms re-dispatches them in order, re-checking per room again:
    person.kid comes back home during Office, so the retry skips Bedroom too."""
    from custom_components.eufy_vacuum.learning.services import (
        SERVICE_RETRY_MISSED_ROOMS,
        async_register_learning_services,
        async_unregister_learning_services,
    )

    calls, events = _recheck_setup(
        hass, manager, monkeypatch, freezer, blocked_rooms=(_OFFICE, _BEDROOM), three=True,
    )
    await async_register_learning_services(hass)
    try:
        await manager.start_selected_rooms(
            vacuum_entity_id=_VAC, map_id=_MAP, strict_order=True, recheck_rules_per_room=True,
        )
        await hass.async_block_till_done()
        hass.states.async_set("person.kid", "home")
        await _finish_room(hass, freezer)
        assert [e["missed_room_ids"] for e in events["incomplete"]] == [[_OFFICE, _BEDROOM]]

        hass.states.async_set("person.kid", "not_home")
        result = await hass.services.async_call(
            "eufy_vacuum", SERVICE_RETRY_MISSED_ROOMS, {"vacuum_entity_id": _VAC},
            blocking=True, return_response=True,
        )
        await hass.async_block_till_done()
        assert result["started"] is True, (result.get("reason"), result.get("message"))
        assert _segments(calls) == [_KITCHEN, _OFFICE]

        hass.states.async_set("person.kid", "home")
        await _finish_room(hass, freezer)
    finally:
        await async_unregister_learning_services(hass)

    assert _segments(calls) == [_KITCHEN, _OFFICE]
    assert [e["room_id"] for e in events["skipped"]] == [_OFFICE, _BEDROOM, _BEDROOM]
    assert [e["status"] for e in events["finished"]] == ["interrupted", "interrupted"]
    assert events["incomplete"][-1]["missed_room_ids"] == [_BEDROOM]


async def test_recheck_is_a_no_op_on_an_atomic_run(hass, manager, monkeypatch, freezer):
    """[RB-RECHECK-7] Without strict order a Roborock run is one atomic batch, with no
    point between rooms to re-check at: recheck_rules_per_room changes nothing. The
    batch goes out once, Office's blocker turning true mid-run skips nothing, and the
    job completes."""
    calls, events = _recheck_setup(hass, manager, monkeypatch, freezer)

    result = await manager.start_selected_rooms(
        vacuum_entity_id=_VAC, map_id=_MAP, recheck_rules_per_room=True,
    )
    await hass.async_block_till_done()
    assert result["started"] is True, (result.get("reason"), result.get("message"))
    assert [c["params"] for c in calls["send"]] == [[{"segments": [_KITCHEN, _OFFICE], "repeat": 1}]]

    hass.states.async_set("person.kid", "home")
    await _finish_room(hass, freezer)

    assert len(calls["send"]) == 1
    assert events["skipped"] == []
    assert [e["status"] for e in events["finished"]] == ["completed"]
    assert events["incomplete"] == []
