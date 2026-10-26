-- =====================================================================
-- Run on the RDS PostgreSQL target AFTER CDC has drained and BEFORE you
-- point the app at RDS.
--
-- Why: DMS inserts rows with their original ids, so the identity/serial
-- sequences SCT created are still at 1. The first INSERT from the app would
-- fail with "duplicate key value violates unique constraint".
-- This bumps every sequence in the shopdb schema to MAX(id).
--
--   psql "host=<rds-endpoint> dbname=shopdb user=postgres" -f db/pg_post_cutover.sql
-- =====================================================================

DO $$
DECLARE
    r       record;
    max_id  bigint;
BEGIN
    FOR r IN
        SELECT n.nspname AS schema_name,
               c.relname AS table_name,
               a.attname AS column_name,
               pg_get_serial_sequence(format('%I.%I', n.nspname, c.relname), a.attname) AS seq
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        WHERE n.nspname = 'shopdb'
          AND c.relkind = 'r'
          AND pg_get_serial_sequence(format('%I.%I', n.nspname, c.relname), a.attname) IS NOT NULL
    LOOP
        EXECUTE format('SELECT COALESCE(MAX(%I), 0) FROM %I.%I',
                       r.column_name, r.schema_name, r.table_name) INTO max_id;
        PERFORM setval(r.seq, GREATEST(max_id, 1), max_id > 0);
        RAISE NOTICE '%.% -> sequence % set to %', r.table_name, r.column_name, r.seq, max_id;
    END LOOP;
END $$;

-- Grant the app user access (create it first if needed:
--   CREATE USER shopapp WITH PASSWORD '...';)
GRANT USAGE ON SCHEMA shopdb TO shopapp;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA shopdb TO shopapp;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA shopdb TO shopapp;
