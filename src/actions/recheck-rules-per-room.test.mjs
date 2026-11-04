// recheck_rules_per_room in the card: the per-run toggle next to the
// strict-order control, and the persisted per-profile flag in the run-profile editor.
// The backend re-checks each room's rules (blockers and modifiers) right before it is
// dispatched; the card only has to carry the opt-in to start_selected_rooms and the
// profile services.
//
// Run: node --test src/actions/recheck-rules-per-room.test.mjs
//
// Coverage (RCR = ReCheck Rules):
//   [RCR-1] the per-run toggle defaults off and flips
//   [RCR-2] startCleaning sends recheck_rules_per_room only when the toggle is on and
//           strict order is on (the chip is only shown then)
//   [RCR-3] saveRunProfile always sends the flag; overwriteRunProfile only when given
//   [RCR-4] the run-profile draft starts off, a legacy profile normalizes to off, and the
//           editor opens with the saved value

import { test } from "node:test";
import assert from "node:assert/strict";

import { makeActions, envelope } from "./_test-host.mjs";
import { applyRoomsState } from "../state/rooms.js";
import { applyRunProfilesState } from "../state/run-profiles.js";

function makeCard({ recheck, strict = true }) {
  const calls = [];
  const card = makeActions({
    hass: {
      callService: async (domain, service, data) => {
        calls.push({ service, data });
        if (service === "get_start_status") return envelope({ blocked: false, reason: "ready" });
        if (service === "start_selected_rooms") return envelope({ started: true });
        return envelope({});
      },
    },
  });
  card.state = {
    vacuumEntityId: () => "vacuum.ivy",
    activeMapId: () => "1",
    batteryLevel: () => 80,
    strictOrder: () => strict,
    recheckRulesPerRoom: () => recheck,
    resetLiveTrail: () => {},
    setStartStatus: () => {},
    clearCancelRunConfirmation: () => {},
    setStartConfirmation: () => {},
    clearStartConfirmation: () => {},
    setLearningEstimate: () => {},
    setLearningReanchored: () => {},
    setLearningCompletedRooms: () => {},
    setLearningNextRoom: () => {},
    setLearningJobActive: () => {},
    beginLearningJob: () => {},
    learningReanchored: () => null,
  };
  card.runLearningEstimate = async () => null;
  return { card, calls };
}

test("[RCR-1] the per-run toggle defaults off and flips", () => {
  const proto = {};
  applyRoomsState(proto);
  const state = Object.create(proto);

  assert.equal(state.recheckRulesPerRoom(), false);
  assert.equal(state.toggleRecheckRulesPerRoom(), true);
  assert.equal(state.recheckRulesPerRoom(), true);
  assert.equal(state.toggleRecheckRulesPerRoom(), false);
});

test("[RCR-2] startCleaning sends recheck_rules_per_room only with the toggle and strict order on", async () => {
  for (const [recheck, strict, sent] of [[true, true, true], [false, true, undefined], [true, false, undefined]]) {
    const { card, calls } = makeCard({ recheck, strict });
    await card.startCleaning();
    const start = calls.find((c) => c.service === "start_selected_rooms");
    assert.equal(start.data.recheck_rules_per_room, sent, `recheck=${recheck} strict=${strict}`);
  }
});

test("[RCR-3] saveRunProfile always sends the flag; overwriteRunProfile only when given", async () => {
  const { card, calls } = makeCard({ recheck: false });
  const ids = { vacuum_entity_id: "vacuum.ivy", map_id: "1" };

  await card.saveRunProfile({ ...ids, name: "Daily", recheck_rules_per_room: true });
  await card.saveRunProfile({ ...ids, name: "Plain" });
  await card.overwriteRunProfile({ ...ids, profile_id: "p1", recheck_rules_per_room: false });
  await card.overwriteRunProfile({ ...ids, profile_id: "p1" });

  assert.deepEqual(
    calls.map((c) => c.data.recheck_rules_per_room),
    [true, false, false, undefined],
  );
});

test("[RCR-4] the run-profile draft starts off, legacy normalizes off, the editor opens saved", () => {
  const proto = {};
  applyRunProfilesState(proto);
  const state = Object.create(proto);

  assert.equal(state._emptyRunProfileDraft().recheck_rules_per_room, false);
  assert.equal(state._normalizeRunProfile({ id: "old" }).recheck_rules_per_room, false);
  assert.equal(
    state._normalizeRunProfile({ id: "p1", recheck_rules_per_room: true }).recheck_rules_per_room,
    true,
  );

  state.selectedRunProfile = () => state._normalizeRunProfile({ id: "p1", recheck_rules_per_room: true });
  state.openSelectedRunProfileEditor();
  assert.equal(state._ensureRunProfilesState().draft.recheck_rules_per_room, true);
});
