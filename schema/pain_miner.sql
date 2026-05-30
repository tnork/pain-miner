-- NorkForce-HubRM: pain_posts table for the Pain Miner pipeline.
-- Run this once in the Supabase SQL editor for project vzyknkrcmerorwfguscx.
-- Safe to re-run: all statements use IF NOT EXISTS / IF EXISTS / OR REPLACE.
--
-- Steps:
--   1. Create pain_posts table
--   2. Indices: dedup on post_url + frontend query (status, discovered_at) + archive sweep
--   3. RLS on pain_posts: anon can SELECT only, no direct UPDATE/INSERT/DELETE
--   4. pain_miner_secrets table — holds the shared secret the RPC checks
--   5. RPC set_pain_post_status — only sanctioned mutation path for the frontend.
--      Requires p_secret matching the row in pain_miner_secrets so the public anon
--      key alone is not enough to mutate (the secret lives in config.js, which is
--      gated by nginx basic auth).
--   6. Grant EXECUTE on the RPC to anon
--
-- After running this once, you must seed the shared secret:
--   1. Generate a long random string (e.g. `openssl rand -hex 32`)
--   2. INSERT INTO pain_miner_secrets (key, value) VALUES ('rpc_shared_secret', '<value>');
--   3. Add the same value to /opt/lai-research/.env as PAINMINER_RPC_SECRET
--   4. Re-run deploy/droplet/painminer-deploy.sh so config.js picks it up

-- 1. Table
CREATE TABLE IF NOT EXISTS pain_posts (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  platform          text NOT NULL,                 -- reddit | hackernews | stackoverflow | x | other
  post_url          text NOT NULL,
  post_title        text NOT NULL,
  author_handle     text,
  posted_at         timestamptz,
  summary           text NOT NULL,
  opportunity       text,                          -- 1–2 sentence engagement angle
  categories        text[] NOT NULL DEFAULT '{}',
  competitors       text[] NOT NULL DEFAULT '{}',  -- e.g. {Reducto, Unstructured} when post mentions them
  status            text NOT NULL DEFAULT 'new',   -- new | replied | deleted | archived | completed
  discovered_at     timestamptz NOT NULL DEFAULT now(),
  status_changed_at timestamptz
);

-- competitors column may be missing from a pre-existing deployment — backfill safely.
ALTER TABLE pain_posts
  ADD COLUMN IF NOT EXISTS competitors text[] NOT NULL DEFAULT '{}';

-- Multi-tenant audit: who actioned a post and when.
ALTER TABLE pain_posts ADD COLUMN IF NOT EXISTS actioned_by text;
ALTER TABLE pain_posts ADD COLUMN IF NOT EXISTS actioned_at timestamptz;

-- 2. Indices
CREATE UNIQUE INDEX IF NOT EXISTS pain_posts_post_url_idx
  ON pain_posts (post_url);

CREATE INDEX IF NOT EXISTS pain_posts_status_discovered_idx
  ON pain_posts (status, discovered_at DESC);

CREATE INDEX IF NOT EXISTS pain_posts_status_age_idx
  ON pain_posts (status, discovered_at);

-- GIN index so the frontend's "any competitor mention" filter is fast.
CREATE INDEX IF NOT EXISTS pain_posts_competitors_gin
  ON pain_posts USING gin (competitors);

-- 3. RLS on pain_posts — no direct anon access. All reads go through secret-gated RPCs.
ALTER TABLE pain_posts ENABLE ROW LEVEL SECURITY;

-- Remove the old open-read policy; reads now require the RPC secret.
DROP POLICY IF EXISTS pain_posts_anon_select ON pain_posts;

-- 4. Secrets table — anon NEVER reads this. Service role inserts the secret;
--    the RPC reads it via SECURITY DEFINER. RLS with no policies = anon blocked.
CREATE TABLE IF NOT EXISTS pain_miner_secrets (
  key   text PRIMARY KEY,
  value text NOT NULL
);

ALTER TABLE pain_miner_secrets ENABLE ROW LEVEL SECURITY;
-- (No policies created → anon cannot select/insert/update/delete.)

-- 5. RPC: only sanctioned write path for the frontend.
--    SECURITY DEFINER bypasses RLS so the function can read the secret + update the row.
--    p_secret must match the seeded shared secret, otherwise the function aborts.
--    p_user (optional) is the authenticated username parsed from the htpasswd session —
--    written to actioned_by for audit logging in multi-tenant deployments.
CREATE OR REPLACE FUNCTION set_pain_post_status(
  p_id     uuid,
  p_status text,
  p_secret text,
  p_user   text DEFAULT NULL
)
RETURNS void
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
  IF p_status NOT IN ('new', 'replied', 'deleted', 'archived', 'completed') THEN
    RAISE EXCEPTION 'invalid status: %', p_status;
  END IF;
  UPDATE pain_posts
     SET status            = p_status,
         status_changed_at = now(),
         actioned_by       = p_user,
         actioned_at       = now()
   WHERE id = p_id;
END;
$$;

-- 6. Allow anon to call the RPC. Without the right p_secret, every call raises.
--    Grant covers both old 3-arg and new 4-arg signatures so re-runs are safe.
GRANT EXECUTE ON FUNCTION set_pain_post_status(uuid, text, text, text) TO anon;

-- Drop the old 2-arg signature if it exists from a prior migration so anon can't
-- bypass the secret check via the older overload.
DROP FUNCTION IF EXISTS set_pain_post_status(uuid, text);
-- Drop old 3-arg grant (replaced by 4-arg above; old overload no longer exists after OR REPLACE).
DROP FUNCTION IF EXISTS set_pain_post_status(uuid, text, text);

-- 7. Frontend error reporting — production-grade in-stack alternative to Sentry.
--    Browser captures errors via window.onerror / unhandledrejection / explicit
--    try/catch and posts them through log_painminer_error. Cron-driven
--    --mode error-digest (15 min cadence) emails new fingerprints via Brevo.
--    Persisted indefinitely as an audit trail.
CREATE TABLE IF NOT EXISTS pain_miner_errors (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  occurred_at  timestamptz NOT NULL DEFAULT now(),
  message      text NOT NULL,
  stack        text,
  page_url     text,
  user_agent   text,
  fingerprint  text NOT NULL,    -- short hash of message + first stack frame
  alerted_at   timestamptz       -- set by error-digest job once Brevo email sent
);

-- Digest job filters: WHERE alerted_at IS NULL ORDER BY occurred_at.
CREATE INDEX IF NOT EXISTS pain_miner_errors_alerted_idx
  ON pain_miner_errors (alerted_at, occurred_at);

-- RPC rate limit query: COUNT WHERE fingerprint = X AND occurred_at > now() - 1h.
CREATE INDEX IF NOT EXISTS pain_miner_errors_fingerprint_idx
  ON pain_miner_errors (fingerprint, occurred_at DESC);

-- RLS — anon CANNOT read or write directly. The RPC is the only write path,
-- and only the service role (server-side digest job) reads.
ALTER TABLE pain_miner_errors ENABLE ROW LEVEL SECURITY;
-- (No policies created → anon is blocked on every operation.)

-- 8. Frontend → Supabase error logging RPC.
--    Mirrors the secret-gated pattern from set_pain_post_status. Adds a hard
--    rate limit (max 20 events per fingerprint per hour) so a runaway browser
--    error loop can't fill the table or trigger an email storm.
CREATE OR REPLACE FUNCTION log_painminer_error(
  p_message     text,
  p_stack       text,
  p_page_url    text,
  p_user_agent  text,
  p_fingerprint text,
  p_secret      text
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  expected     text;
  recent_count int;
  fp           text;
BEGIN
  -- Auth: same shared-secret as set_pain_post_status.
  SELECT value INTO expected
    FROM pain_miner_secrets
   WHERE key = 'rpc_shared_secret'
   LIMIT 1;
  IF expected IS NULL OR p_secret IS NULL OR p_secret <> expected THEN
    RAISE EXCEPTION 'unauthorized';
  END IF;

  -- Required fields.
  IF p_message IS NULL OR length(p_message) = 0 THEN
    RAISE EXCEPTION 'message required';
  END IF;
  IF p_fingerprint IS NULL OR length(p_fingerprint) = 0 THEN
    RAISE EXCEPTION 'fingerprint required';
  END IF;

  -- Truncate the fingerprint BEFORE the rate-limit check. Otherwise a caller
  -- with the secret could vary a suffix past 64 chars to bypass the per-hour
  -- cap while still collapsing into the same stored fingerprint.
  fp := left(p_fingerprint, 64);

  -- Per-fingerprint rate limit. Past 20/hour, drop silently to keep the table
  -- (and email volume) sane during a runaway client-side error loop.
  SELECT COUNT(*) INTO recent_count
    FROM pain_miner_errors
   WHERE fingerprint = fp
     AND occurred_at > now() - interval '1 hour';
  IF recent_count >= 20 THEN
    RETURN;
  END IF;

  INSERT INTO pain_miner_errors (message, stack, page_url, user_agent, fingerprint)
  VALUES (
    left(p_message,    1000),
    left(p_stack,      4000),
    left(p_page_url,    500),
    left(p_user_agent,  500),
    fp
  );
END;
$$;

GRANT EXECUTE ON FUNCTION log_painminer_error(text, text, text, text, text, text) TO anon;


-- 9. Secret-gated read RPCs — replace the former anon SELECT policy.
--    The frontend calls these instead of querying pain_posts directly, so the
--    anon key alone (without the RPC secret from config.js) cannot read any data.

-- Active queue: status = 'new', newest-first, capped at 500 rows.
CREATE OR REPLACE FUNCTION get_active_pain_posts(p_secret text)
RETURNS SETOF pain_posts
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE expected text;
BEGIN
  SELECT value INTO expected FROM pain_miner_secrets WHERE key = 'rpc_shared_secret' LIMIT 1;
  IF expected IS NULL OR p_secret IS NULL OR p_secret <> expected THEN
    RAISE EXCEPTION 'unauthorized';
  END IF;
  RETURN QUERY
    SELECT * FROM pain_posts
    WHERE status = 'new'
    ORDER BY discovered_at DESC
    LIMIT 500;
END;
$$;

GRANT EXECUTE ON FUNCTION get_active_pain_posts(text) TO anon;

-- Archive view: status IN ('archived','completed','replied'), optional time cutoff on actioned_at.
CREATE OR REPLACE FUNCTION get_archive_pain_posts(
  p_secret text,
  p_cutoff timestamptz DEFAULT NULL
)
RETURNS SETOF pain_posts
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE expected text;
BEGIN
  SELECT value INTO expected FROM pain_miner_secrets WHERE key = 'rpc_shared_secret' LIMIT 1;
  IF expected IS NULL OR p_secret IS NULL OR p_secret <> expected THEN
    RAISE EXCEPTION 'unauthorized';
  END IF;
  RETURN QUERY
    SELECT * FROM pain_posts
    WHERE status IN ('archived', 'completed', 'replied')
      AND (p_cutoff IS NULL OR actioned_at >= p_cutoff)
    ORDER BY actioned_at DESC NULLS LAST
    LIMIT 500;
END;
$$;

GRANT EXECUTE ON FUNCTION get_archive_pain_posts(text, timestamptz) TO anon;

-- Reports view: all statuses, filtered by discovered_at >= p_cutoff.
-- Used by reports.html instead of a direct table query (anon SELECT policy was dropped).
CREATE OR REPLACE FUNCTION get_pain_posts_for_reports(
  p_secret text,
  p_cutoff timestamptz DEFAULT NULL
)
RETURNS SETOF pain_posts
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE expected text;
BEGIN
  SELECT value INTO expected FROM pain_miner_secrets WHERE key = 'rpc_shared_secret' LIMIT 1;
  IF expected IS NULL OR p_secret IS NULL OR p_secret <> expected THEN
    RAISE EXCEPTION 'unauthorized';
  END IF;
  RETURN QUERY
    SELECT * FROM pain_posts
    WHERE (p_cutoff IS NULL OR discovered_at >= p_cutoff)
    ORDER BY discovered_at DESC
    LIMIT 10000;
END;
$$;

GRANT EXECUTE ON FUNCTION get_pain_posts_for_reports(text, timestamptz) TO anon;
