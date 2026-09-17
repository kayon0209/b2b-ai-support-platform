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

export type GapStats = Record<string, number>;

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

export interface ListEnvelope<T> {
  items: T[];
  total: number;
}

export interface ApiError {
  status: number;
  code: string;
  message: string;
  retryable: boolean;
}
