"""Import every mapped module, so `Base.metadata` is complete.

Why this module exists
----------------------
`cases.enterprise_account_id` references `enterprise_accounts`, which is mapped
in `identity.models`. SQLAlchemy resolves a foreign key's target table when
mappers are configured, so a process that maps `cases` **without** mapping
`identity` fails with

    NoReferencedTableError: Foreign key associated with column
    'cases.enterprise_account_id' could not find table 'enterprise_accounts'

That failure depends on import order, appears in some entry points and not
others, and reads like a broken schema rather than a missing import. It was
found exactly that way: a worker-side integration test failed, while the API
tests passed, because the API imports every router and the worker test did not.

The alternative considered was to stop declaring the foreign key in the model
and let the migration own it. That trades a real guarantee - "the ORM knows
what the database enforces" - for a smaller import graph, and it would leave
`create_all` (used by the SQLite unit tests) building a schema that is wrong
rather than merely incomplete. So the metadata is made complete instead.

Completeness is checked rather than asserted: `test_schema_privileges.py`
verifies that every table in the live database is present in `Base.metadata`,
so a model module that is missing from this list fails loudly instead of
producing an order-dependent error somewhere else.

`platform_core.db` imports this module, which means anything that can open a
session has complete metadata. None of the mapped modules import `db` (checked),
so there is no cycle.
"""

from platform_core.agent_runtime import models as _agent_runtime_models  # noqa: F401
from platform_core.audit import models as _audit_models  # noqa: F401
from platform_core.billing import models as _billing_models  # noqa: F401
from platform_core.cases import agent_models as _agent_models  # noqa: F401
from platform_core.cases import canned_models as _canned_models  # noqa: F401
from platform_core.cases import models as _cases_models  # noqa: F401
from platform_core.cases import sla_models as _sla_models  # noqa: F401
from platform_core.evaluation import ab_models as _ab_models  # noqa: F401
from platform_core.evaluation import category_models as _category_models  # noqa: F401
from platform_core.identity import control_lease as _control_lease  # noqa: F401
from platform_core.identity import models as _identity_models  # noqa: F401
from platform_core.integrations import models as _integrations_models  # noqa: F401
from platform_core.knowledge import acl as _knowledge_acl  # noqa: F401
from platform_core.knowledge import flag_models as _flag_models  # noqa: F401
from platform_core.knowledge import gap_models as _gap_models  # noqa: F401
from platform_core.knowledge import models as _knowledge_models  # noqa: F401
from platform_core.outbox import OutboxEvent as _OutboxEvent  # noqa: F401
from platform_core.support_bridge import models as _support_bridge_models  # noqa: F401
from platform_core.tool_gateway import models as _tool_gateway_models  # noqa: F401

__all__: list[str] = []
