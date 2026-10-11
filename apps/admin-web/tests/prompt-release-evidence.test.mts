import assert from "node:assert/strict";
import { parsePromotionEvidence } from "../src/lib/promptReleaseEvidence.ts";

const parsed = parsePromotionEvidence({
  eval_run_id: "ci-run-2026-10-08",
  scores_json: '[{"category":"citation","passed":19,"total":20}]',
  regressions_json:
    '[{"category":"latency","baseline_rate":0.95,"candidate_rate":0.9}]',
});

assert.deepEqual(parsed, {
  eval_run_id: "ci-run-2026-10-08",
  scores: [{ category: "citation", passed: 19, total: 20 }],
  regressions: [{ category: "latency", baseline_rate: 0.95, candidate_rate: 0.9 }],
});

assert.throws(
  () =>
    parsePromotionEvidence({
      eval_run_id: "ci-run",
      scores_json: '[{"category":"citation","passed":21,"total":20}]',
      regressions_json: "[]",
    }),
  /passed\/total/,
);

assert.throws(
  () =>
    parsePromotionEvidence({
      eval_run_id: "ci-run",
      scores_json: '[{"category":"citation","passed":1,"total":1}]',
      regressions_json: '[{"category":"latency","baseline_rate":1.2,"candidate_rate":0.9}]',
    }),
  /rates between 0 and 1/,
);

console.log("prompt release evidence: schema normalization and invalid summaries rejected");
