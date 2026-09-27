import assert from "node:assert/strict";
import {
  nextWorkbenchRightTab,
  WORKBENCH_RIGHT_TABS,
} from "../src/lib/workbenchTabs.ts";

assert.deepEqual(WORKBENCH_RIGHT_TABS, ["reply", "knowledge", "tools", "tasks"]);
assert.equal(nextWorkbenchRightTab("reply", "ArrowLeft"), "tasks");
assert.equal(nextWorkbenchRightTab("tasks", "ArrowRight"), "reply");
assert.equal(nextWorkbenchRightTab("knowledge", "ArrowRight"), "tools");
assert.equal(nextWorkbenchRightTab("tools", "ArrowLeft"), "knowledge");
assert.equal(nextWorkbenchRightTab("tools", "Home"), "reply");
assert.equal(nextWorkbenchRightTab("reply", "End"), "tasks");
assert.equal(nextWorkbenchRightTab("reply", "Tab"), null);

console.log("workbench tabs: 7 keyboard navigation checks passed");
