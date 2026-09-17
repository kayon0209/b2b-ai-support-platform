"""Seed a demo tenant/admin user so the admin UI can be exercised live.

Not part of the product: this is throwaway local tooling for manually
verifying the admin-web against a real API. It uses the `platform` (superuser)
role because it must insert across RLS boundaries before any tenant exists.

Usage:
    python scripts/seed_admin_demo.py
Prints the bootstrap token to paste into VITE_API_TOKEN.
"""

import os
import uuid

from sqlalchemy import create_engine, text

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

SLUG = "admin-demo"
ROLE = "tenant_owner"
EMAIL = "admin-demo@example.com"


def main() -> None:
    tenant_id = uuid.uuid5(uuid.NAMESPACE_URL, f"tenant:{SLUG}")
    user_id = uuid.uuid5(uuid.NAMESPACE_URL, f"user:{EMAIL}")
    engine = create_engine(ADMIN_URL)

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, 'Admin Demo Tenant', 'active') "
                "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name"
            ),
            {"id": str(tenant_id), "slug": SLUG},
        )
        conn.execute(
            text(
                "INSERT INTO users (id, primary_email, display_name, is_service_account) "
                "VALUES (:id, :email, 'Admin Demo', false) "
                "ON CONFLICT (primary_email) DO UPDATE SET display_name = EXCLUDED.display_name"
            ),
            {"id": str(user_id), "email": EMAIL},
        )
        # Membership is the row the middleware resolves; without it the
        # request fails closed with no role.
        conn.execute(
            text(
                "INSERT INTO memberships (id, tenant_id, user_id, role, status) "
                "VALUES (gen_random_uuid(), :tid, :uid, :role, 'active') "
                "ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = EXCLUDED.role"
            ),
            {"tid": str(tenant_id), "uid": str(user_id), "role": ROLE},
        )

    engine.dispose()
    print(f"tenant_id = {tenant_id}")
    print(f"user_id   = {user_id}")
    print(f"role      = {ROLE}")
    print(f"VITE_API_TOKEN=pt_{SLUG}_{user_id}")


if __name__ == "__main__":
    main()
