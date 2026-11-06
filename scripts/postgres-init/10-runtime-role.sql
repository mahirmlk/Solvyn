-- The official PostgreSQL image runs this once for a fresh data directory.
-- Keep POSTGRES_USER as the bootstrap administrator. Migrations own schema
-- objects; the API and worker use a separate role that cannot alter policies.
\getenv migration_user POSTGRES_MIGRATION_USER
\getenv migration_password POSTGRES_MIGRATION_PASSWORD
\getenv runtime_user POSTGRES_APP_USER
\getenv runtime_password POSTGRES_APP_PASSWORD
\getenv app_database POSTGRES_DB

CREATE ROLE :"migration_user" LOGIN PASSWORD :'migration_password' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE :"runtime_user" LOGIN PASSWORD :'runtime_password' NOSUPERUSER NOBYPASSRLS;
ALTER DATABASE :"app_database" OWNER TO :"migration_user";
GRANT CONNECT ON DATABASE :"app_database" TO :"runtime_user";
ALTER SCHEMA public OWNER TO :"migration_user";
GRANT USAGE ON SCHEMA public TO :"runtime_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_user" IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"runtime_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_user" IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO :"runtime_user";
