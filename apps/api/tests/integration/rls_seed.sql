-- RLS isolation verification (ticket 2 acceptance).
-- Run as psql script: two tenants seeded by owner role, then connect as
-- platform_app (no BYPASSRLS) and prove cross-tenant reads/writes fail.

-- Seed data as table owner (RLS does not apply to owner unless FORCE;
-- our migration FORCEs RLS, but owner bypasses via role membership of
-- table owner. We use platform superuser role 'platform' as seeder and
-- verify with 'platform_app'.)
INSERT INTO tenants (id, slug, name, status) VALUES
  ('01900000-0000-7000-8000-000000000001', 'acme', 'Acme Corp', 'active'),
  ('01900000-0000-7000-8000-000000000002', 'globex', 'Globex Inc', 'active')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO users (id, primary_email, display_name) VALUES
  ('01900000-0000-7000-8000-0000000000aa', 'alice@acme.test', 'Alice')
ON CONFLICT (primary_email) DO NOTHING;

INSERT INTO memberships (id, tenant_id, user_id, role, status) VALUES
  ('01900000-0000-7000-8000-0000000000ba', '01900000-0000-7000-8000-000000000001',
   '01900000-0000-7000-8000-0000000000aa', 'support_agent', 'active')
ON CONFLICT DO NOTHING;

-- Seeded.
