// Run: node --test src/renderers/room-rules-room-options.test.mjs
//
// The room-level options in the Rules view:
//   RPT: the "pass through when blocked" toggle (allow_pass_through_when_blocked)
//   RBW: the "when blocked mid-room" selector (on_block_while_cleaning)
//
// Driven from the room switch's attributes (what the backend publishes, room_entities.py)
// through the real `_normalizeRoom`, so the test also proves the card can READ the value.
//
// Coverage:
//   [RPT-1] a room without the flag renders the toggle OFF
//   [RPT-2] a room with the flag renders the toggle ON
//   [RBW-1] a room without the value renders "finish" selected
//   [RBW-2] a room set to stop_now renders "stop_now" selected

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

function renderRuleList(room) {
  return makeHost()._renderRuleList({ roomRulesForRoom: () => [] }, room);
}

function renderToggle(room) {
  const match = renderRuleList(room).match(
    /<button[^>]*data-action="toggle-room-pass-through"[^>]*>([^<]*)<\/button>/,
  );
  assert.ok(match, "the Rules view must render the pass-through toggle");
  return { button: match[0], label: match[1].trim() };
}

/** The chips of the selector, as [{ value, label, active }]. */
function renderChips(room) {
  const chips = [...renderRuleList(room).matchAll(
    /<button[^>]*data-action="set-room-on-block-while-cleaning"[^>]*>([^<]*)<\/button>/g,
  )].map(([button, label]) => ({
    value: button.match(/data-value="([^"]*)"/)?.[1],
    label: label.trim(),
    active: /\bactive\b/.test(button),
  }));
  assert.equal(chips.length, 2, "the Rules view must render both choices");
  return chips;
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

test("[RBW-1] a room without the value renders finish selected", () => {
  assert.deepEqual(renderChips(roomFromSwitchAttrs()), [
    { value: "finish", label: "Finish Room", active: true },
    { value: "stop_now", label: "Stop Now", active: false },
  ]);
});

test("[RBW-2] a room set to stop_now renders stop_now selected", () => {
  const chips = renderChips(roomFromSwitchAttrs({ on_block_while_cleaning: "stop_now" }));
  assert.deepEqual(chips.map((c) => [c.value, c.active]), [["finish", false], ["stop_now", true]]);
});
