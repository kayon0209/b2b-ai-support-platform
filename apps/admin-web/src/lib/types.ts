// Wire types for the B2B AI support control plane. Field names mirror the
// FastAPI response models in apps/api/src/platform_core (see AGENTS.md).

export interface QualityMetrics {
  window_seconds: number;
  total_runs: number;
  completed: number;
  abstained: number;
  handed_off: number;
  failed: number;
  untimed_runs: number;
  abstention_rate: number;
  handoff_rate: number;
  citation_coverage: number;
  route_counts: Record<string, number>;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  /**
   * Resolution outcomes, derived from Cases rather than runs. The denominator
   * is resolved-or-reopened Cases only, so an open Case does not drag the
   * rate around with backlog.
   */
  cases_measured: number;
  supported_resolution: number;
  wrong_resolution: number;
  open_cases: number;
  supported_resolution_rate: number;
  wrong_resolution_rate: number;
}

export interface RouteDistribution {
  window_seconds: number;
  route_counts: Record<string, number>;
  total_runs: number;
}

export interface Gap {
  id: string;
  sample_question: string;
  reason_code: string;
  status: string;
  frequency: number;
  first_seen_at: number | null;
  last_seen_at: number | null;
  acknowledged_at: number | null;
  target_space_id: string | null;
}

export interface Draft {
  id: string;
  gap_id: string;
  title: string;
  body: string;
  status: string;
  author_kind: string;
  reviewed_by: string | null;
  reviewed_at: number | null;
  review_notes: string | null;
  published_document_id: string | null;
}

/** GET /v1/knowledge/gaps/stats — by_status maps status → gap count. */
export interface GapStats {
  by_status: Record<string, number>;
  total_gaps: number;
  total_occurrences: number;
}

export interface PromptVersion {
  id: string;
  template_name: string;
  version: number;
  published: boolean;
  body: string;
}

export interface ActivePrompt {
  active: PromptVersion | null;
  template_name: string;
}

export interface FeatureFlag {
  key: string;
  description: string;
  enabled: boolean;
  rollout_percent: number;
  created_at: number;
}

export interface FlagDecision {
  key: string;
  enabled: boolean;
  reason: string;
  rollout_percent: number;
}

export interface Case {
  case_id: string;
  subject: string;
  status: string;
  priority: string;
  category: string;
  assignee_ref: string | null;
  team_ref: string | null;
  version: number;
  opened_at: number;
  first_response_due_at: number | null;
  resolution_due_at: number | null;
  first_responded_at: number | null;
  resolved_at: number | null;
  closed_at: number | null;
}

export type MemberRole =
  | "tenant_owner"
  | "security_admin"
  | "support_admin"
  | "knowledge_manager"
  | "support_agent"
  | "support_viewer"
  | "integration_service"
  | "auditor";

export interface Member {
  user_id: string;
  /** PK of the membership row — what member commands address. */
  membership_id: string;
  email: string;
  display_name: string;
  role: string;
  status: string;
}

/** Response of POST /v1/identity/members/invite (token is shown once). */
export interface InviteResult {
  invitation_token?: string;
  status?: string;
  message?: string;
  user_id?: string;
  role?: string;
}

export interface TenantBranding {
  tenant_id: string;
  slug: string;
  display_name: string | null;
  logo_url: string | null;
  primary_color: string | null;
  support_email: string | null;
}

export interface ListEnvelope<T> {
  items: T[];
  total: number;
}

/** Response of GET /v1/tenant/usage and PUT /v1/tenant/quota. */
export interface UsageSnapshot {
  period_start: number;
  period_end: number;
  runs_used: number;
  prompt_tokens: number;
  completion_tokens: number;
  /** null means unlimited. */
  quota: number | null;
  remaining: number | null;
  over_quota: boolean;
}

/**
 * Response of GET /v1/tenant/billing — the append-only ledger rollup an
 * invoice is computed from. Distinct from `UsageSnapshot`, which is a
 * live count of runs: the ledger additionally survives a run row being
 * rewritten, and adjustments appear here as their own entries.
 */
export interface BillingRollup {
  period_start: number;
  period_end: number;
  entries: number;
  usage_entries: number;
  adjustment_entries: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

/**
 * Response of POST /v1/tenant/billing/adjustments.
 *
 * `duplicate` reports that the Idempotency-Key had already been used, so this
 * call changed nothing. Surfaced rather than swallowed because a retry that
 * silently did nothing and one that silently did something twice look
 * identical from the client, and only one of them is correct.
 */
export interface BillingAdjustmentResult {
  billing: BillingRollup;
  duplicate: boolean;
}

/**
 * A tool proposal as `GET /v1/tool-proposals` returns it — a write the agent
 * prepared.
 *
 * Two status fields, and the difference is the whole point. `status` is the
 * stored row. `effective_status` is what a human should act on: a proposal
 * past its expiry is reported as `expired`, because `confirm` and `execute`
 * both refuse it. Binding the UI to `status` would offer an approval the API
 * will not accept, and the operator would only find that out by clicking.
 */
/**
 * One tool the tenant can propose against.
 *
 * Read from `GET /v1/tools` rather than carried in the UI, so a tool added to
 * the server catalog appears without a front-end change and one a tenant has
 * disabled disappears. `input_schema` is what the propose form prefills from,
 * so the operator starts from the shape the API validates against.
 */
export interface ToolCatalogEntry {
  name: string;
  version: number;
  risk: string;
  requires_confirmation: boolean;
  input_schema: {
    type?: string;
    properties?: Record<string, unknown>;
    required?: string[];
  };
  /** True when this tenant overrides the shared catalog entry. */
  tenant_scoped: boolean;
}

export interface ToolProposal {
  proposal_id: string;
  tool_name: string | null;
  tool_version: number | null;
  risk: string | null;
  status: string;
  effective_status: string;
  /** The frozen arguments. This is exactly what an approval binds to. */
  arguments: Record<string, unknown>;
  action_hash: string;
  permission_decision: string;
  permission_reason: string;
  required_confirmation: boolean;
  expires_at: number;
}

/**
 * One attempt at running a proposal.
 *
 * `verification_status` is the honest field: `executed` means the call
 * returned, not that the write happened. `unknown` means the postcondition
 * could not be determined and must never be rendered as a completed action.
 */
export interface ToolProposalExecution {
  execution_id: string;
  status: string;
  verification_status: string | null;
  output: Record<string, unknown> | null;
  error_code: string | null;
  started_at: number;
  completed_at: number | null;
}

/**
 * A failed API call, as a real `Error`.
 *
 * It must extend `Error`, not merely be shaped like one. Every call site
 * reads the failure with `err instanceof Error ? err.message : String(err)`,
 * and for a plain object that falls through to `String(err)` — which renders
 * the literal text `[object Object]`. That is what every error banner and
 * every `alert()` in this app used to show, so the server's message (the one
 * thing the operator needs) was thrown away at the last step.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;

  constructor(status: number, code: string, message: string, retryable: boolean) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryable = retryable;
  }
}