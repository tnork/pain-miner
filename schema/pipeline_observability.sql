-- NorkForce-HubRM: Pipeline observability — central error log + run history.
-- Run this once in the Supabase SQL editor for project vzyknkrcmerorwfguscx.
-- Safe to re-run: every statement uses IF NOT EXISTS / IF EXISTS / OR REPLACE.
--
-- Two tables, server-side write only (RLS-locked, no anon policies):
--   1. pipeline_errors — every cron-script error, regardless of product
--   2. pipeline_runs   — every cron-script firing (started_at → ended_at) with
--                        in-flight steps stored as a JSONB array
--
-- Two read-only RPCs for the frontend (runs.muisbien.com), gated by the
-- existing pain_miner_secrets.rpc_shared_secret. nginx basic auth + the
-- shared secret in the SAME role as pain miner — no new secret needed.
--
-- Products use these short codes everywhere:
--   RA = Research Agent (Project 1)
--   CI = Conference Intel (Project 3, both historical + forward)
--   PM = Pain Miner (Project 2, cron-script errors only — frontend errors
--                    stay in pain_miner_errors)

-- 1. pipeline_errors — central error log
CREATE TABLE IF NOT EXISTS pipeline_errors (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  occurred_at timestamptz NOT NULL DEFAULT now(),
  product     text NOT NULL,                           -- RA | CI | PM
  script      text NOT NULL,                           -- e.g. 'weekly_accounts_agent.py'
  mode        text,                                    -- e.g. 'enroll', 'discover', null for single-mode scripts
  message     text NOT NULL,                           -- short one-line summary
  traceback   text,                                    -- full Python traceback when available
  resolved    boolean NOT NULL DEFAULT false,
  resolved_at timestamptz,
  resolved_by text,                                    -- who marked it resolved (free text)
  run_id      uuid                                     -- forward FK to pipeline_runs.id when known
);

CREATE INDEX IF NOT EXISTS pipeline_errors_occurred_idx
  ON pipeline_errors (occurred_at DESC);
CREATE INDEX IF NOT EXISTS pipeline_errors_product_idx
  ON pipeline_errors (product, occurred_at DESC);
CREATE INDEX IF NOT EXISTS pipeline_errors_unresolved_idx
  ON pipeline_errors (resolved, occurred_at DESC) WHERE resolved = false;

-- 2. pipeline_runs — every cron-script firing
CREATE TABLE IF NOT EXISTS pipeline_runs (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  product     text NOT NULL,                           -- RA | CI | PM
  script      text NOT NULL,
  mode        text,
  started_at  timestamptz NOT NULL DEFAULT now(),
  ended_at    timestamptz,                             -- null while running
  status      text NOT NULL DEFAULT 'running',         -- running | success | failed | partial
  steps       jsonb NOT NULL DEFAULT '[]'::jsonb,      -- [{name, started_at, ended_at, status, count, note, sequence}, ...]
  stats       jsonb NOT NULL DEFAULT '{}'::jsonb,      -- {contacts_found, in_apollo, in_wiza, ...} — flexible per product
  error_id    uuid REFERENCES pipeline_errors(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS pipeline_runs_started_idx
  ON pipeline_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS pipeline_runs_product_status_idx
  ON pipeline_runs (product, status, started_at DESC);
CREATE INDEX IF NOT EXISTS pipeline_runs_running_idx
  ON pipeline_runs (status, started_at DESC) WHERE status = 'running';

-- 3. RLS — server-side only. Anon is fully blocked.
ALTER TABLE pipeline_errors ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline_runs   ENABLE ROW LEVEL SECURITY;
-- (No policies created → anon cannot select/insert/update/delete directly.
--  Frontend reads via the secret-gated RPCs below.)

-- 4. Read-only RPC for the frontend — list runs.
--    Reuses pain_miner_secrets.rpc_shared_secret (same nginx-basic-auth gate
--    serves runs.muisbien.com, so the secret already lives in /var/www
--    behind the same htpasswd). No new secret to manage.
CREATE OR REPLACE FUNCTION get_pipeline_runs(
  p_secret      text,
  p_product     text DEFAULT NULL,    -- 'RA' | 'CI' | 'PM' | NULL for all
  p_status      text DEFAULT NULL,    -- 'running' | 'success' | 'failed' | NULL for all
  p_limit       int  DEFAULT 100,
  p_since_days  int  DEFAULT 90
) RETURNS TABLE (
  id          uuid,
  product     text,
  script      text,
  mode        text,
  started_at  timestamptz,
  ended_at    timestamptz,
  status      text,
  steps       jsonb,
  stats       jsonb,
  error_id    uuid
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  expected text;
BEGIN
  SELECT value INTO expected
    FROM pain_miner_secrets
   WHERE key = 'rpc_shared_secret'
   LIMIT 1;
  IF expected IS NULL OR p_secret IS NULL OR p_secret <> expected THEN
    RAISE EXCEPTION 'unauthorized';
  END IF;

  RETURN QUERY
    SELECT r.id, r.product, r.script, r.mode, r.started_at, r.ended_at,
           r.status, r.steps, r.stats, r.error_id
      FROM pipeline_runs r
     WHERE r.started_at > now() - make_interval(days => COALESCE(p_since_days, 90))
       AND (p_product IS NULL OR r.product = p_product)
       AND (p_status  IS NULL OR r.status  = p_status)
     ORDER BY r.started_at DESC
     LIMIT LEAST(COALESCE(p_limit, 100), 500);
END;
$$;

GRANT EXECUTE ON FUNCTION get_pipeline_runs(text, text, text, int, int) TO anon;

-- 5. Read-only RPC for the frontend — list errors.
CREATE OR REPLACE FUNCTION get_pipeline_errors(
  p_secret      text,
  p_product     text DEFAULT NULL,
  p_unresolved_only boolean DEFAULT false,
  p_limit       int  DEFAULT 100,
  p_since_days  int  DEFAULT 30
) RETURNS TABLE (
  id          uuid,
  occurred_at timestamptz,
  product     text,
  script      text,
  mode        text,
  message     text,
  traceback   text,
  resolved    boolean,
  resolved_at timestamptz,
  run_id      uuid
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  expected text;
BEGIN
  SELECT value INTO expected
    FROM pain_miner_secrets
   WHERE key = 'rpc_shared_secret'
   LIMIT 1;
  IF expected IS NULL OR p_secret IS NULL OR p_secret <> expected THEN
    RAISE EXCEPTION 'unauthorized';
  END IF;

  RETURN QUERY
    SELECT e.id, e.occurred_at, e.product, e.script, e.mode, e.message,
           e.traceback, e.resolved, e.resolved_at, e.run_id
      FROM pipeline_errors e
     WHERE e.occurred_at > now() - make_interval(days => COALESCE(p_since_days, 30))
       AND (p_product IS NULL OR e.product = p_product)
       AND (NOT p_unresolved_only OR e.resolved = false)
     ORDER BY e.occurred_at DESC
     LIMIT LEAST(COALESCE(p_limit, 100), 500);
END;
$$;

GRANT EXECUTE ON FUNCTION get_pipeline_errors(text, text, boolean, int, int) TO anon;

-- 6. Mark-an-error-resolved RPC. Same secret check.
CREATE OR REPLACE FUNCTION resolve_pipeline_error(
  p_id         uuid,
  p_secret     text,
  p_resolved_by text DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  expected text;
BEGIN
  SELECT value INTO expected
    FROM pain_miner_secrets
   WHERE key = 'rpc_shared_secret'
   LIMIT 1;
  IF expected IS NULL OR p_secret IS NULL OR p_secret <> expected THEN
    RAISE EXCEPTION 'unauthorized';
  END IF;

  UPDATE pipeline_errors
     SET resolved    = true,
         resolved_at = now(),
         resolved_by = COALESCE(p_resolved_by, 'frontend')
   WHERE id = p_id;
END;
$$;

GRANT EXECUTE ON FUNCTION resolve_pipeline_error(uuid, text, text) TO anon;
