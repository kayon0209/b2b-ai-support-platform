"""Strip markup out of stored tenant display names.

Revision ID: 0053_brand_display_name_cleanup
Revises: 0052_ab_experiments
Create Date: 2026-09-23

`identity/branding.py` refuses `<` and `>` in `display_name` on write, and that
rule only ever governs the *next* write. A tenant stored
`<img src=x onerror=alert(2)>Acme & Co` before the rule existed, and the value
has been served verbatim ever since: it was the `<h1>` of the customer support
window, on desktop and on mobile, for every customer of that tenant (measured
2026-09-23).

Two repairs, and they are not substitutes for each other:

* this migration fixes what is already stored, once;
* `sanitize_display_name` on the read path fixes anything that arrives between
  this migration and the next write, and keeps the value honest for readers
  that never went through validation at all.

The strip is deliberately narrow - angle brackets and control characters only.
`&`, quotes and any script are legitimate in a label ("Acme & Co", "华秋电子"),
and a repair that mangles a tenant's real name to make a point is not a repair.

Note the `REGEXP_REPLACE` is greedy per tag (`<[^>]*>`), not a parser. It does
not need to be one: the goal is to remove markup characters from a label, and
anything that survives the strip is still rendered as text by every consumer.

`downgrade()` is a no-op. The removed markup was the defect and the original
value is not recorded anywhere to restore; writing something back would only
invent data.
"""

import sqlalchemy as sa
from alembic import op

revision = "0053_brand_display_name_cleanup"
down_revision = "0052_ab_experiments"
branch_labels = None
depends_on = None

TABLE = "tenants"
COLUMN = "brand_display_name"


def upgrade() -> None:
    # `tenants` is global reference data - no RLS, so no tenant binding is
    # needed here, unlike every tenant-owned table.
    #
    # Three passes, in this order, and the order is the point: whole tags first,
    # then any angle bracket left over from something that was not a complete
    # tag, then control characters, then trim. This is the same sequence as
    # `identity.branding.sanitize_display_name`, and the two must keep agreeing.
    #
    # The interpolation below is not string-built SQL from caller input:
    # `TABLE` and `COLUMN` are the two module-level literals above, and
    # Postgres cannot bind an identifier as a parameter - so there is no
    # parameterised form of this statement to use instead. `S608` is ignored
    # for this file in `pyproject.toml`, with the same reasoning.
    statement = f"""
            UPDATE {TABLE}
               SET {COLUMN} = NULLIF(
                       BTRIM(
                           REGEXP_REPLACE(
                               REGEXP_REPLACE(
                                   REGEXP_REPLACE({COLUMN}, '<[^>]*>', '', 'g'),
                                   '[<>]', '', 'g'
                               ),
                               '[[:cntrl:]]', '', 'g'
                           )
                       ),
                       ''
                   )
             WHERE {COLUMN} IS NOT NULL
               AND ({COLUMN} ~ '[<>]' OR {COLUMN} ~ '[[:cntrl:]]')
            """
    op.execute(sa.text(statement))


def downgrade() -> None:
    """Nothing to restore: the removed markup was the defect, and the value it
    came from is not recorded anywhere."""
