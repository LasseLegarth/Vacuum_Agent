// Run: node --test src/renderers/room-rules-block-while-cleaning.test.mjs
//
// RBW: ISSUE #22, the room-level "when blocked mid-room" selector in the Rules view.
//
// Driven from the room switch's attributes (what the backend publishes, room_entities.py)
// through the real `_normalizeRoom`, so the test also proves the card can READ the value.
//
// Coverage:
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

/** The chips of the selector, as [{ value, label, active }]. */
function renderChips(room) {
  const html = makeHost()._renderRuleList({ roomRulesForRoom: () => [] }, room);
  const chips = [...html.matchAll(
    /<button[^>]*data-action="set-room-on-block-while-cleaning"[^>]*>([^<]*)<\/button>/g,
  )].map(([button, label]) => ({
    value: button.match(/data-value="([^"]*)"/)?.[1],
    label: label.trim(),
    active: /\bactive\b/.test(button),
  }));
  assert.equal(chips.length, 2, "the Rules view must render both choices");
  return chips;
}

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
