-- The official PostgreSQL image runs this once for a fresh data directory.
-- Keep POSTGRES_USER as the bootstrap administrator. Migrations own schema
-- objects; the API and worker use a separate role that cannot alter policies.
\getenv migration_user POSTGRES_MIGRATION_USER
\getenv migration_password POSTGRES_MIGRATION_PASSWORD
\getenv runtime_user POSTGRES_APP_USER
\getenv runtime_password POSTGRES_APP_PASSWORD
\getenv backup_user POSTGRES_BACKUP_USER
\getenv backup_password POSTGRES_BACKUP_PASSWORD
\getenv restore_user POSTGRES_RESTORE_USER
\getenv restore_password POSTGRES_RESTORE_PASSWORD
\getenv app_database POSTGRES_DB

CREATE ROLE :"migration_user" LOGIN PASSWORD :'migration_password' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE :"runtime_user" LOGIN PASSWORD :'runtime_password' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE :"backup_user" LOGIN PASSWORD :'backup_password' NOSUPERUSER BYPASSRLS;
CREATE ROLE :"restore_user" LOGIN PASSWORD :'restore_password' NOSUPERUSER BYPASSRLS;
GRANT :"migration_user" TO :"restore_user";
ALTER DATABASE :"app_database" OWNER TO :"migration_user";
GRANT CONNECT ON DATABASE :"app_database" TO :"runtime_user";
GRANT CONNECT ON DATABASE :"app_database" TO :"backup_user";
GRANT CONNECT ON DATABASE :"app_database" TO :"restore_user";
ALTER SCHEMA public OWNER TO :"migration_user";
GRANT USAGE ON SCHEMA public TO :"runtime_user";
GRANT USAGE ON SCHEMA public TO :"backup_user";
GRANT USAGE ON SCHEMA public TO :"restore_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_user" IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"runtime_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_user" IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO :"runtime_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_user" IN SCHEMA public
  GRANT SELECT ON TABLES TO :"backup_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_user" IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO :"backup_user";
