-- Local-only credentials for the non-owner application role. Production must
-- provide APP_DATABASE_APP_URL from its secret manager instead.
CREATE ROLE platform_app
  LOGIN
  PASSWORD 'platform_app'
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOINHERIT
  NOBYPASSRLS;
GRANT CONNECT ON DATABASE platform TO platform_app;
