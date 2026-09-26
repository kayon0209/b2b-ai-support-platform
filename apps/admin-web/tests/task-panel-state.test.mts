/**
 * Tests for the task panel's two concurrency rules.
 *
 * Both defects were found by the acceptance review reading the component, and
 * neither is visible to the type checker: one shows a task list for a
 * conversation the operator has left, the other submits one task's field value
 * against another task. They are executed here rather than matched in source,
 * following the approach `url-state.test.mts` documents.
 *
 * Run by Node's type stripping: no test runner, no DOM, no new dependency.
 */

import assert from "node:assert/strict";

import {
  allCollected,
  collectKey,
  collectedFor,
  mayApply,
  nextSeq,
  type RequestSeq,
} from "../src/lib/taskPanelState.ts";

let failures = 0;
let passes = 0;

function test(name: string, body: () => void): void {
  try {
    body();
    passes += 1;
  } catch (error) {
    failures += 1;
    console.error(`FAIL ${name}`);
    console.error(`  ${(error as Error).message}`);
  }
}

// --- a stale response must not land -----------------------------------------

test("a response for the current request applies", () => {
  const seq: RequestSeq = { current: 0 };
  const started = nextSeq(seq);
  assert.equal(mayApply(seq, started, "conv-a", "conv-a"), true);
});

test("a response overtaken by a newer request is dropped", () => {
  const seq: RequestSeq = { current: 0 };
  const first = nextSeq(seq);
  nextSeq(seq); // a second request starts
  assert.equal(mayApply(seq, first, "conv-a", "conv-a"), false);
});

test("a response for a conversation the operator left is dropped", () => {
  // The case the review found: one request in flight, the operator navigates,
  // and the old response resolves afterwards. The sequence number alone does
  // not catch this, because no second request has started yet.
  const seq: RequestSeq = { current: 0 };
  const started = nextSeq(seq);
  assert.equal(mayApply(seq, started, "conv-a", "conv-b"), false);
});

test("switching away and back does not resurrect the first response", () => {
  const seq: RequestSeq = { current: 0 };
  const forA = nextSeq(seq);
  nextSeq(seq); // navigating to B starts a request
  // Back on A: the conversation matches again, but the seq does not, so the
  // first response is still dropped rather than overwriting B's data.
  assert.equal(mayApply(seq, forA, "conv-a", "conv-a"), false);
});

// --- field state is per task ------------------------------------------------

test("the collect key is scoped to the task", () => {
  assert.equal(collectKey("task-1", "street"), "task-1:street");
  assert.notEqual(collectKey("task-1", "street"), collectKey("task-2", "street"));
});

test("two tasks waiting for the same field keep separate values", () => {
  // The second case the review found: both tasks are waiting for `street`.
  const collecting = {
    [collectKey("task-1", "street")]: "南京西路 100 号",
    [collectKey("task-2", "street")]: "张江路 1 号",
  };
  assert.equal(collectedFor(collecting, "task-1", ["street"]).street, "南京西路 100 号");
  assert.equal(collectedFor(collecting, "task-2", ["street"]).street, "张江路 1 号");
});

test("only the fields the task is waiting for are sent", () => {
  // The server refuses a batch naming a field the task is not waiting for, so
  // the client must not send one - including values the operator typed for a
  // different task.
  const collecting = {
    [collectKey("task-1", "street")]: "a",
    [collectKey("task-1", "city")]: "上海",
    [collectKey("task-2", "city")]: "北京",
  };
  const sent = collectedFor(collecting, "task-1", ["street"]);
  assert.deepEqual(Object.keys(sent), ["street"]);
  assert.equal("city" in sent, false);
});

test("a missing field reads as empty rather than undefined", () => {
  assert.equal(collectedFor({}, "task-1", ["street"]).street, "");
});

test("allCollected needs every field, and only non-blank ones", () => {
  assert.equal(allCollected({}, "task-1", ["street"]), false);
  assert.equal(allCollected({ [collectKey("task-1", "street")]: "   " }, "task-1", ["street"]), false);
  assert.equal(
    allCollected(
      { [collectKey("task-1", "street")]: "a", [collectKey("task-1", "city")]: "b" },
      "task-1",
      ["street", "city"],
    ),
    true,
  );
});

test("allCollected is per task, not per field", () => {
  const collecting = { [collectKey("task-2", "street")]: "a" };
  assert.equal(allCollected(collecting, "task-1", ["street"]), false);
  assert.equal(allCollected(collecting, "task-2", ["street"]), true);
});

console.log(`${passes} passed, ${failures} failed`);
if (failures > 0) process.exit(1);
