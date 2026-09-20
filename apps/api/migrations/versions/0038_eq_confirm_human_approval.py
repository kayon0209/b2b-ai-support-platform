"""Raise `case.eq_confirm` to `human_approval` on rows that already exist.

Revision ID: 0038_eq_confirm_human_approval
Revises: 0037_turn_text_resolver
Create Date: 2026-09-19

A data migration, which is unusual for this repository and is the point of the
revision. `TOOL_CATALOG` in `tool_gateway/registry.py` defines the risk class a
tool is *seeded* with, and `ensure_tool_definitions` only ever **adds** missing
rows - by design, so a tenant that edited or disabled a definition is not
silently repaired over. The consequence is that changing a risk class in code
reaches new tenants and **not existing ones**, and the gateway reads the risk
from the row. So without this, the code would say `human_approval` while every
tenant already seeded kept `confirmed_write`, and the stricter class would be a
comment rather than a control.

**Why the class matters here.** `case.eq_confirm` records a customer's answer to
an engineering question, and the case status is what the factory reads - so
recording a confirmation is one step from releasing production against a spec.
`human_approval` is the class the policy engine reserves for `tenant_owner` and
keeps deliberately unreachable by the agent at every stage, propose included;
`confirmed_write` lets the agent propose it and a support agent approve it. The
research report asks for the former in five places ("EQ 放行…必须人工").

`risk` and `required_permissions` move together: the second is what the
definition advertises, and leaving it on `tool.write.confirmed` would make the
row describe a control it no longer has.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0038_eq_confirm_human_approval"
down_revision: str | None = "0037_turn_text_resolver"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE tool_definitions
        SET risk = 'human_approval',
            required_permissions = '["tool.human_approval"]'::jsonb
        WHERE name = 'case.eq_confirm'
          AND risk = 'confirmed_write'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE tool_definitions
        SET risk = 'confirmed_write',
            required_permissions = '["tool.write.confirmed"]'::jsonb
        WHERE name = 'case.eq_confirm'
          AND risk = 'human_approval'
        """
    )
