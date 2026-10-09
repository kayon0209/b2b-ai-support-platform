export interface PromotionScore {
  category: string;
  passed: number;
  total: number;
}

export interface PromotionRegression {
  category: string;
  baseline_rate: number;
  candidate_rate: number;
}

export interface PromotionEvidence {
  eval_run_id: string;
  scores: PromotionScore[];
  regressions: PromotionRegression[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function parsePromotionEvidence(values: Record<string, string>): PromotionEvidence {
  const evalRunId = values.eval_run_id.trim();
  if (!evalRunId || evalRunId.length > 127) {
    throw new Error("evaluation run ID is required and must be at most 127 characters");
  }

  let rawScores: unknown;
  let rawRegressions: unknown;
  try {
    rawScores = JSON.parse(values.scores_json);
    rawRegressions = values.regressions_json.trim()
      ? JSON.parse(values.regressions_json)
      : [];
  } catch {
    throw new Error("scores and regressions must be valid JSON arrays");
  }
  if (!Array.isArray(rawScores) || rawScores.length === 0 || !Array.isArray(rawRegressions)) {
    throw new Error("at least one score is required; regressions must be an array");
  }

  const scores: PromotionScore[] = rawScores.map((item: unknown) => {
    if (
      !isRecord(item) ||
      typeof item.category !== "string" ||
      !item.category.trim() ||
      typeof item.passed !== "number" ||
      typeof item.total !== "number" ||
      !Number.isInteger(item.passed) ||
      !Number.isInteger(item.total) ||
      item.passed < 0 ||
      item.total < item.passed
    ) {
      throw new Error("each score needs a category and valid passed/total counts");
    }
    return { category: item.category.trim(), passed: item.passed, total: item.total };
  });

  const regressions: PromotionRegression[] = rawRegressions.map((item: unknown) => {
    if (
      !isRecord(item) ||
      typeof item.category !== "string" ||
      !item.category.trim() ||
      typeof item.baseline_rate !== "number" ||
      typeof item.candidate_rate !== "number" ||
      !Number.isFinite(item.baseline_rate) ||
      !Number.isFinite(item.candidate_rate) ||
      item.baseline_rate < 0 ||
      item.baseline_rate > 1 ||
      item.candidate_rate < 0 ||
      item.candidate_rate > 1
    ) {
      throw new Error("each regression needs a category and rates between 0 and 1");
    }
    return {
      category: item.category.trim(),
      baseline_rate: item.baseline_rate,
      candidate_rate: item.candidate_rate,
    };
  });
  return { eval_run_id: evalRunId, scores, regressions };
}
