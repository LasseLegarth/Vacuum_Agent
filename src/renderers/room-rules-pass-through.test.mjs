// Run: node --test src/renderers/room-rules-pass-through.test.mjs
//
// RPT: ISSUE #21, the room-level "pass through when blocked" toggle in the Rules view.
//
// Driven from the room switch's attributes (what the backend publishes, room_entities.py)
// through the real `_normalizeRoom`, so the test also proves the card can READ the flag.
//
// Coverage:
//   [RPT-1] a room without the flag renders the toggle OFF
//   [RPT-2] a room with the flag renders the toggle ON

import { test } from "node:test";
import assert from "node:assert/strict";

import { applySharedRenderers } from "./shared.js";
import { applyRoomRulesRenderers } from "./room-rules.js";
import { applyRoomsState } from "../state/rooms.js";

function makeHost() {
  class H {}
  applySharedRenderers(H.prototype);
  applyRoomRulesRenderers(H.prototype);
  const h = new H();
  h._i18nLanguage = () => "en";
  return h;
}

function roomFromSwitchAttrs(extra = {}) {
  const proto = {};
  applyRoomsState(proto);
  const state = Object.create(proto);
  return state._normalizeRoom({ room_id: 4, map_id: "1", room_name: "Hall", ...extra }, true);
}

function renderToggle(room) {
  const html = makeHost()._renderRuleList({ roomRulesForRoom: () => [] }, room);
  const match = html.match(/<button[^>]*data-action="toggle-room-pass-through"[^>]*>([^<]*)<\/button>/);
  assert.ok(match, "the Rules view must render the pass-through toggle");
  return { button: match[0], label: match[1].trim() };
}

test("[RPT-1] a room without the flag renders the toggle off", () => {
  const { button, label } = renderToggle(roomFromSwitchAttrs());
  assert.doesNotMatch(button, /\bactive\b/);
  assert.equal(label, "Allow Pass Through");
});

test("[RPT-2] a room with the flag renders the toggle on", () => {
  const { button, label } = renderToggle(roomFromSwitchAttrs({ allow_pass_through_when_blocked: true }));
  assert.match(button, /\bactive\b/);
  assert.equal(label, "Passes Through");
});
