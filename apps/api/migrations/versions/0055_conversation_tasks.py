"""Conversation tasks, their event log, and copilot drafts.

Revision ID: 0055_conversation_tasks
Revises: 0054_workbench_queue_indexes
Create Date: 2026-09-26

R1 of the semantic enhancement (docs/implementation/ai-support-v2). Four tables,
one migration, because they are one unit of work: a task without its event log
cannot be reconstructed, and a draft that outlives the conversation it was
written for is a cross-conversation leak.

**Why tasks are rows and not a field on the run.** The spec's journey has one
customer turn producing three needs - check the order, change the address if it
has not shipped, reissue an invoice - with different states, different missing
fields and different blockers. The run has one terminal status, so a run-level
field would flatten "one done, one blocked, one unsupported" into a single
answer, and the agent would be shown a conversation as resolved when two of the
three things the customer asked for never happened.

**Idempotency is a composite key plus a content hash, not a run id.** The unique
constraint is (tenant_id, conversation_ref_id, source_turn_id, task_local_key).
Re-delivering the same turn - a webhook retry, a worker restart, a replay -
collides on that key and returns the existing row. The hash of the task's
meaningful content is stored alongside so a *different* payload under the same
key is refused rather than silently overwriting: without the hash, a retry that
carries a corrected intent would look like a duplicate and be dropped, and the
correction would be lost with no trace.

**action_revision is what makes a confirmation revocable.** When the arguments
of a pending action change, the revision increments and the old confirmation no
longer matches. A confirmation binds to (task_id, action_revision) rather than
to the task, so "the agent changed the address after I approved it" is
impossible: approving revision 1 does not authorize revision 2.

**`condition` is a constrained JSON column, not free text.** Only fields and
operators the server registers are accepted at write time (see
`agent_runtime.tasks.conditions`); this column stores the already-validated
structure so a reader does not have to trust it.

**Sensitive field values are not in this table.** A confirmed delivery address
or a tax id is business data with a narrower audience than a task row, and
audit/metrics readers reach this table. Slot *names*, origins and confirmation
flags live here; the values live in `copilot_drafts`-grade storage behind the
gateway's own authorization, and the ordinary snapshot records a hash.

**event log is append-only.** `conversation_task_events` has no update path in
the application, and its RLS policy is the same tenant isolation. An operator
reading "why did this task become needs_human" needs the actual sequence of
reasons, not the current value.

**RLS**: every table carries `tenant_id`, gets `ENABLE` + `FORCE ROW LEVEL
SECURITY`, and is granted only to `platform_app`. The composite foreign keys
are (tenant_id, id) pairs, so a row cannot reference a task in another tenant
even if an id is guessed - the FK fails at the database, not at a check that
some future caller might forget.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0055_conversation_tasks"
down_revision: str | None = "0054_workbench_queue_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"

TASKS = "conversation_tasks"
EVENTS = "conversation_task_events"
DRAFTS = "copilot_drafts"
ASSESSMENTS = "semantic_assessments"

# The RLS predicate, identical on all four tables. NULLIF rather than a bare
# cast: `app.tenant_id` is set far more often than it is valid, and a bare
# `::uuid` on an empty value raises instead of matching no rows (see migration
# 0046/0047 for the original reasoning).
_POLICY = """
    CREATE POLICY tenant_isolation ON {table}
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
"""


def _enable_rls(table: str) -> None:
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(_POLICY.format(table=table))


def upgrade() -> None:
    # --- semantic_assessments ----------------------------------------------
    # One row per classification attempt, in every mode including `off`. It is
    # the comparison record SHD-01 needs: the same input produces a row in
    # both modes, and the business tables are untouched either way.
    #
    # `snapshot` holds labels, evidence *positions*, slot names/origins and
    # counts - never a message body and never a slot value. The projection is
    # built by `SemanticAssessment.as_snapshot`.
    op.create_table(
        ASSESSMENTS,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_ref_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(31), nullable=False),
        sa.Column("rule_route", sa.String(31), nullable=False, server_default=""),
        sa.Column("rule_action", sa.String(31), nullable=False, server_default=""),
        sa.Column("model_primary_intent", sa.String(31), nullable=True),
        sa.Column("agreement", sa.String(31), nullable=False, server_default=""),
        sa.Column("effective_decision", sa.String(63), nullable=False, server_default=""),
        sa.Column("reason_codes", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("validation_status", sa.String(31), nullable=False, server_default=""),
        sa.Column("prompt_version", sa.String(63), nullable=False, server_default=""),
        sa.Column("model_name", sa.String(127), nullable=False, server_default=""),
        sa.Column("latency_ms", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "completion_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        # Labels only. This is what the eval report reads back.
        sa.Column("snapshot", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        # A conversation has many assessments (one per turn, per mode), so
        # (tenant_id, conversation_ref_id) is NOT unique and cannot be the
        # target of a composite foreign key. The pair below is the one a
        # cross-tenant reference could abuse, and making it unique lets the
        # task table carry a real composite FK instead of trusting the
        # application to check tenancy.
        sa.UniqueConstraint(
            "tenant_id", "id", "conversation_ref_id", name="uq_semantic_assessments_tenant_ref"
        ),
    )
    op.create_index(
        "ix_semantic_assessments_conversation",
        ASSESSMENTS,
        ["tenant_id", "conversation_ref_id", "created_at"],
    )
    op.create_index("ix_semantic_assessments_mode", ASSESSMENTS, ["tenant_id", "mode"])

    # --- conversation_tasks -------------------------------------------------
    op.create_table(
        TASKS,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_ref_id", sa.Uuid(), nullable=False),
        sa.Column("source_turn_id", sa.String(64), nullable=False),
        # The assessment that produced this task. Nullable: a task may be
        # created by a deterministic rule path with no model involved, and
        # forcing a row here would mean inventing an assessment to satisfy a
        # foreign key.
        sa.Column("assessment_id", sa.Uuid(), nullable=True),
        # Server-generated, stable across retries. The model proposes
        # `source_turn_id`; it never chooses this.
        sa.Column("task_local_key", sa.String(63), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("kind", sa.String(31), nullable=False),
        sa.Column("status", sa.String(31), nullable=False),
        # Optimistic concurrency. A command carrying the wrong version is a
        # 409, not a lost update.
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        # Increments when the action's arguments change. A confirmation binds
        # to (task_id, action_revision); changing the arguments revokes it.
        sa.Column("action_revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        # Hash of the task's meaningful content. A retry under the same
        # (tenant, conversation, turn, local_key) with a DIFFERENT hash is a
        # conflict, not a duplicate - see the module docstring.
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("depends_on", postgresql.JSONB(), nullable=False, server_default="[]"),
        # Already validated against the server's condition registry.
        sa.Column("condition", postgresql.JSONB(), nullable=True),
        # Slot NAMES and origins only. Values live behind the gateway.
        sa.Column("slots", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("missing_slots", postgresql.JSONB(), nullable=False, server_default="[]"),
        # Why a task is blocked, e.g. SEMANTIC_NO_WRITE_CAPABILITY. Shown to the
        # agent verbatim: "unsupported" without a reason is not actionable.
        sa.Column("blocked_reason", sa.String(63), nullable=True),
        # The proposal/execution this task produced, for joins in the workbench.
        sa.Column("proposal_id", sa.Uuid(), nullable=True),
        sa.Column("execution_id", sa.Uuid(), nullable=True),
        # Set only from a verified tool receipt or a recorded human action. An
        # LLM asserting completion never writes here.
        sa.Column("completion_evidence", sa.String(127), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        # The idempotency boundary. See the module docstring.
        sa.UniqueConstraint(
            "tenant_id",
            "conversation_ref_id",
            "source_turn_id",
            "task_local_key",
            name="uq_conversation_task_identity",
        ),
        # Target for the drafts FK: a task's (tenant, id, conversation) triple
        # must be unique for a composite reference to be enforceable.
        sa.UniqueConstraint(
            "tenant_id", "id", "conversation_ref_id", name="uq_conversation_tasks_tenant_ref"
        ),
    )
    op.create_index(
        "ix_conversation_tasks_status",
        TASKS,
        ["tenant_id", "conversation_ref_id", "status", "sequence"],
    )
    # Partial: only tasks that can still act. The scheduler polls for exactly
    # these, and an index over terminal rows would be mostly dead weight.
    op.create_index(
        "ix_conversation_tasks_actionable",
        TASKS,
        ["tenant_id", "status", "updated_at"],
        postgresql_where=sa.text("status IN ('ready', 'awaiting_confirmation', 'executing')"),
    )

    # Composite FKs: (tenant_id, id) pairs, so a cross-tenant reference is
    # rejected by the database rather than by a check some caller may skip.
    op.create_foreign_key(
        "fk_tasks_assessment_tenant",
        TASKS,
        ASSESSMENTS,
        ["tenant_id", "assessment_id", "conversation_ref_id"],
        ["tenant_id", "id", "conversation_ref_id"],
        ondelete="CASCADE",
    )

    # --- conversation_task_events ------------------------------------------
    op.create_table(
        EVENTS,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        # The task's conversation, denormalized so the composite FK can carry
        # tenancy. Without it the reference would be (tenant_id, task_id),
        # which PostgreSQL cannot enforce unless (tenant_id, id) is unique -
        # and it is not declared that way, so the constraint would be dropped
        # rather than created.
        sa.Column("conversation_ref_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("from_status", sa.String(31), nullable=True),
        sa.Column("to_status", sa.String(31), nullable=False),
        # "ai" | "human" | "system". Recorded so "who moved this" is answerable
        # after a handoff without joining the audit log.
        sa.Column("actor_type", sa.String(31), nullable=False, server_default="system"),
        sa.Column("actor_ref", sa.String(63), nullable=True),
        sa.Column("reason_code", sa.String(63), nullable=False, server_default=""),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("from_version", sa.Integer(), nullable=True),
        sa.Column("to_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "task_id", "sequence", name="uq_conversation_task_event_seq"
        ),
    )
    op.create_index(
        "ix_conversation_task_events_task",
        EVENTS,
        ["tenant_id", "task_id", "sequence"],
    )
    op.create_foreign_key(
        "fk_task_events_task_tenant",
        EVENTS,
        TASKS,
        ["tenant_id", "task_id", "conversation_ref_id"],
        ["tenant_id", "id", "conversation_ref_id"],
        ondelete="CASCADE",
    )

    # --- copilot_drafts -----------------------------------------------------
    # A draft is controlled business data, not a log line: it is content an
    # agent may send to a customer. It therefore lives in its own table with its
    # own RLS, and never appears in an audit record, a metric label or a trace
    # attribute.
    op.create_table(
        DRAFTS,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_ref_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        # A draft's task, when it was generated for one. Nullable for the same
        # reason as `tasks.assessment_id`: a summary job needs no task row.
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(31), nullable=False),
        sa.Column("status", sa.String(31), nullable=False),
        # The timeline revision the generation was based on. A new customer
        # message bumps it and the job goes stale rather than overwriting.
        sa.Column("timeline_revision", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # The lease version at generation time. A handoff between queueing and
        # completion makes the result stale; it must not be inserted silently.
        sa.Column("lease_version", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # Authorization references the generation actually used. A summary that
        # cannot name its sources is not sent.
        sa.Column("source_refs", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        # Set when an agent has edited the body. A regeneration must not
        # overwrite it (COP-02).
        sa.Column("edited_by_human", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_code", sa.String(63), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        # One draft per job. A replayed generation request returns the existing
        # draft instead of billing a second model call.
        sa.UniqueConstraint("tenant_id", "job_id", name="uq_copilot_draft_job"),
    )
    op.create_index(
        "ix_copilot_drafts_conversation",
        DRAFTS,
        ["tenant_id", "conversation_ref_id", "status"],
    )
    op.create_foreign_key(
        "fk_copilot_drafts_task_tenant",
        DRAFTS,
        TASKS,
        ["tenant_id", "task_id", "conversation_ref_id"],
        ["tenant_id", "id", "conversation_ref_id"],
        ondelete="CASCADE",
    )

    for table in (ASSESSMENTS, TASKS, EVENTS, DRAFTS):
        _enable_rls(table)


def downgrade() -> None:
    # Expand-only deploy discipline: the application is rolled back first, and
    # the tables are kept. Dropping them here is for a developer reversing an
    # unmerged branch, not a production rollback - per spec 8, a production
    # rollback closes the flags and restores the old application.
    for table in (DRAFTS, EVENTS, TASKS, ASSESSMENTS):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
