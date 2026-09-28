import assert from "node:assert/strict";
import {
  nextWorkbenchQueueTab,
  nextWorkbenchRightTab,
  WORKBENCH_QUEUE_TABS,
  WORKBENCH_RIGHT_TABS,
} from "../src/lib/workbenchTabs.ts";

assert.deepEqual(WORKBENCH_QUEUE_TABS, ["queue", "mine", "waiting"]);
assert.equal(nextWorkbenchQueueTab("queue", "ArrowLeft"), "waiting");
assert.equal(nextWorkbenchQueueTab("waiting", "ArrowRight"), "queue");
assert.equal(nextWorkbenchQueueTab("mine", "ArrowRight"), "waiting");
assert.equal(nextWorkbenchQueueTab("mine", "Home"), "queue");
assert.equal(nextWorkbenchQueueTab("queue", "End"), "waiting");
assert.equal(nextWorkbenchQueueTab("mine", "Tab"), null);

assert.deepEqual(WORKBENCH_RIGHT_TABS, ["reply", "knowledge", "tools", "tasks"]);
assert.equal(nextWorkbenchRightTab("reply", "ArrowLeft"), "tasks");
assert.equal(nextWorkbenchRightTab("tasks", "ArrowRight"), "reply");
assert.equal(nextWorkbenchRightTab("knowledge", "ArrowRight"), "tools");
assert.equal(nextWorkbenchRightTab("tools", "ArrowLeft"), "knowledge");
assert.equal(nextWorkbenchRightTab("tools", "Home"), "reply");
assert.equal(nextWorkbenchRightTab("reply", "End"), "tasks");
assert.equal(nextWorkbenchRightTab("reply", "Tab"), null);

console.log("workbench tabs: 13 keyboard navigation checks passed");
