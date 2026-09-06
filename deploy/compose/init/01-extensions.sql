-- Runs once on first boot of an empty data directory.
--
-- Only extensions live here. Tables, hypertables and policies belong in Alembic
-- migrations (W2): a schema that exists because someone ran psql once is not
-- reproducible, and cannot be rolled back.
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Toolkit is a SEPARATE extension from timescaledb (conflict C-08). It is
-- present in the timescaledb-ha image but not in a plain timescaledb install,
-- so it is created here and any dependent aggregate must degrade without it.
CREATE EXTENSION IF NOT EXISTS timescaledb_toolkit;
