# Pain Miner — Standalone Application

Engagement-triage queue. Surfaces practitioner pain posts on Reddit / Hacker News / StackOverflow / X / Bluesky / cloud-vendor Q&A forums (AWS re:Post, Microsoft Q&A, Google Cloud Community) where someone is discussing real OCR / NLP / IDP / document-processing pain, so a human can publicly reply with a helpful suggestion. **Not lead-gen** — no CRM enrollment, no contact resolution.

Extracted from the `lai-central-research-agent` monorepo on 2026-05-29 and split into this standalone repo for independent deployment and database migration. That monorepo was renamed `tnork/muisbien` on GitHub on 2026-08-18 (old `lai-central-research-agent` URLs redirect) — see **Deploy — current reality** below for why this matters.

**GitHub remote:** `git@github.com:tnork/pain-miner.git` (added 2026-05-29, pushed initial commit)

---

## Repo structure

```
pain_miner/
├── scripts/
│   ├── pain_miner.py        # Main script — all 4 modes
│   └── _pipeline_log.py     # Supabase pipeline observability helper
├── schema/
│   ├── pain_miner.sql            # Core Pain Miner tables + RPCs
│   └── pipeline_observability.sql # Observability tables (apply if running standalone)
├── web/
│   ├── index.html           # Triage queue
│   ├── login.html           # Auth gate
│   ├── reports.html         # Chart.js dashboard
│   ├── overview.html        # System explainer
│   ├── theme.css            # Shared visual system (warm-paper palette)
│   ├── error-shim.js        # Frontend error reporter
│   ├── config.example.js    # Template for config.js (rendered by deploy script)
│   ├── favicon.ico
│   ├── favicon-32.png
│   └── images/
│       ├── pain-miner-icon-cropped.png
│       ├── pain-miner-login-logo2.png
│       └── pain-miner-logo-cropped.png
├── deploy/
│   ├── painminer-deploy.sh  # One-step server deploy (idempotent)
│   ├── painminer-nginx.conf # nginx server block (Full SSL, config.js auth-gated)
│   └── crontab.example      # Cron entries for the 4 modes
├── .env                     # Local secrets (gitignored)
├── .env.example             # Template
├── requirements.txt
└── CLAUDE.md
```

---

## Infrastructure (current state → migration target)

| Component | Current (copied from LAI monorepo) | Migration target |
|---|---|---|
| **Supabase** | LAI NorkForce-HubRM (`vzyknkrcmerorwfguscx`) | Dedicated Pain Miner project |
| **Observability** | Writes `pipeline_runs`/`pipeline_errors` to LAI Supabase | Same dedicated project, or strip out `_pipeline_log` calls |
| **Server** | `/opt/lai-research` droplet (`104.248.54.204`) | `/opt/pain-miner` on same or new server |
| **Domain** | `painminer.muisbien.com` | Same or new domain |
| **Brevo** | Shared free-tier account (300/day · 9k/month) | Same or dedicated |

### Migration checklist (when ready)

1. Create a new Supabase project.
2. Run `schema/pain_miner.sql` in the new SQL editor.
3. Seed the RPC secret: `INSERT INTO pain_miner_secrets (key, value) VALUES ('rpc_shared_secret', '<your-secret>');`
4. If you want observability (`runs.muisbien.com` equivalent), also run `schema/pipeline_observability.sql`.
5. Update `.env`: `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_ANON_KEY`, `PAINMINER_RPC_SECRET`.
6. Re-deploy: `sudo bash deploy/painminer-deploy.sh`.

---

## Deploy — current reality (until the migration above happens)

**The "Migration target" column above hasn't happened yet.** `/opt/pain-miner` does not exist on the droplet. Confirmed by hand 2026-08-18 — don't assume the `## Deploy (server)` instructions further down work as written; they describe the post-migration target state.

Production actually runs `pain_miner.py` out of a **different repo**: `lai-central-research-agent` (droplet path `/opt/lai-research`), renamed `tnork/muisbien` on GitHub 2026-08-18. That copy is kept in sync with *this* standalone repo by hand, not by `git pull` — it has one deliberate, permanent divergence from this repo's `scripts/pain_miner.py`:

```python
# near the top, after the pydantic import:
import pathlib as _pathlib  # noqa: E402
import sys as _sys  # noqa: E402
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
from scripts.lib import retry as _retry_lib  # noqa: E402
from scripts.lib import brevo as _brevo_lib  # noqa: E402
_xai_post_with_retry = _retry_lib.xai_post_with_retry
# ...and _brevo_send / _brevo_send_text delegate to _brevo_lib instead of
# doing their own urllib POST — the monorepo shares one retry/Brevo
# implementation across all its pipelines (weekly_accounts_agent.py, etc.)
```

**To ship a change from this repo to production:**
1. Commit + push here as normal.
2. On the droplet, take this repo's `scripts/pain_miner.py` and re-apply that shim (drop the local `_xai_post_with_retry` function body in favor of the one-line assignment; make `_brevo_send`/`_brevo_send_text` delegate to `_brevo_lib`) — **do not blindly overwrite** `/opt/lai-research/scripts/pain_miner.py`, that silently reintroduces duplicate retry/Brevo code the monorepo maintainer explicitly removed in a past sync. Diffing the two files first will show ONLY this shim as the pre-existing divergence — anything else in the diff is your real change.
3. Copy the changed `web/*.html`/`.css` files straight across to `/opt/lai-research/web/painminer/` — these have no monorepo-specific divergence, confirmed by hand 2026-08-18.
4. Commit + push in `/opt/lai-research` too (matching the existing sync-commit history there — search `git log --oneline -- scripts/pain_miner.py` for the pattern).
5. Run `bash /opt/lai-research/deploy/droplet/painminer-deploy.sh` (idempotent — copies `web/painminer/*` → `/var/www/painminer/`, regenerates `config.js`, reloads nginx).
6. No crontab change needed — cron already points at `/opt/lai-research/scripts/pain_miner.py`, so step 2 alone is what makes a code change live for the next scheduled run.

**Fixed 2026-08-18:** `/opt/lai-research`'s `git remote -v` used to have a GitHub PAT embedded in plaintext in the origin URL. Now uses `origin = https://github.com/tnork/muisbien.git` with auth via a stored credential helper (`git config credential.helper store`, token in `~/.git-credentials`, `chmod 600`) — confirmed neither `git remote -v` nor `.git/config` expose the token anymore, and `git fetch` against the renamed repo works. Plain `git pull` inside `/opt/lai-research` is safe again (no token in the URL to leak via shell history/`ps`).

This is orthogonal to the shim-preservation caveat above: `git pull` only matters for picking up commits already pushed to `tnork/muisbien`'s own history (where the shim is just normal committed file content, nothing special to reconcile). The caveat above is about the separate step of copying code *from this standalone pain-miner repo* into `/opt/lai-research` — that's a manual merge, not a `git pull`, and still needs the shim re-applied by hand every time.

---

## Environment variables

```bash
# Grok discovery (xAI)
XAI_API_KEY=xai-...
XAI_MODEL=grok-4.3

# Supabase — service role (server-side) + anon key (frontend)
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_KEY=eyJ...         # service_role key
SUPABASE_ANON_KEY=eyJ...    # anon public key

# RPC secret — must match pain_miner_secrets table
PAINMINER_RPC_SECRET=

# nginx login credentials (managed by deploy/painminer-deploy.sh)
PAINMINER_USER=painminer
PAINMINER_PASS=

# Brevo transactional email
BREVO_API_KEY=
EMAIL_FROM="Pain Miner <research@mg.muisbien.com>"

# Stack Exchange API key — registered at stackapps.com (10k req/day)
STACK_EXCHANGE_KEY=

# Triage UI domain — used in the daily summary email link and the
# health-check HTTP probe. Defaults to painminer.muisbien.com if unset.
PAINMINER_DOMAIN=
```

---

## Local development

```bash
# 1. Create config.js from .env values (gitignored — do this once after cloning)
#    Copy the three values from .env → web/config.js matching config.example.js shape.

# 2. Start frontend dev server (serves web/ at http://localhost:8787)
python3 -m http.server 8787 --directory web

# Note: nginx Basic-auth gate for /config.js does not apply locally.
# The Python server serves config.js directly — auth layer is prod-only.
```

---

## Commands

```bash
# Setup (first time on a new server)
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Discovery (production cron mode)
python3 scripts/pain_miner.py --mode discover

# Dry-run — validates env + Supabase connectivity, no Grok calls, no writes
python3 scripts/pain_miner.py --mode discover --dry-run

# Archive sweep — flip new posts >4 days old to 'archived'
python3 scripts/pain_miner.py --mode archive

# Daily summary email (last 24h aggregated to _SUMMARY_TO — the landing.ai team)
python3 scripts/pain_miner.py --mode daily-summary

# Frontend error digest — bundle new frontend errors by fingerprint, email each
python3 scripts/pain_miner.py --mode error-digest
```

---

## Deploy (server) — target state, post-migration

**This section describes deploying a standalone `/opt/pain-miner` clone — the migration target, not the current setup.** Until the migration checklist above is actually run, use "Deploy — current reality" instead; these commands assume `/opt/pain-miner` exists, which as of 2026-08-18 it does not.

```bash
# First-time or re-deploy after pulling changes:
git clone git@github.com:tnork/pain-miner.git /opt/pain-miner
cd /opt/pain-miner
cp .env.example .env   # then fill in real values
sudo bash deploy/painminer-deploy.sh

# After any code change:
git pull && sudo bash deploy/painminer-deploy.sh
```

The deploy script:
- Installs nginx + apache2-utils if missing
- Copies all web assets to `/var/www/painminer/`
- Renders `config.js` (Supabase URL + anon key + RPC secret) from `.env`
- Sets `config.js` to `chmod 640` (nginx-readable only — not publicly served)
- Creates/updates htpasswd for the login gate
- Installs + reloads the nginx server block

---

## Cron schedule

Install with `crontab /opt/pain-miner/deploy/crontab.example` (or `crontab -e` to merge manually).

| Mode | Schedule | What it does |
|---|---|---|
| `discover` | 3×/day Mon–Sat (13:02, 17:00, 22:00 UTC = 9:02 AM / 1 PM / 6 PM ET in EDT) | Grok web+X search → dedup → insert to Supabase |
| `archive` | Daily 03:00 UTC | Flip `status='new'` posts >4 days old to `'archived'` |
| `daily-summary` | Daily 03:30 UTC | Email last-24h digest to the landing.ai team (`_SUMMARY_TO`) |
| `error-digest` | Every 15 min | Bundle frontend errors by fingerprint → email per fingerprint |

Note: the UTC values are calibrated for EDT (UTC-4). In EST (UTC-5, Nov-Mar) the actual ET run times shift one hour earlier — harmless, just cosmetic.

The `error-digest` uses `fcntl.flock` to prevent overlapping runs — safe to run frequently.

---

## Architecture

### 7 discovery sources per `discover` run

1. **Hacker News** — Algolia API (`hn.algolia.com/api/v1/search`), free, no key. ~20 keyword queries.
2. **Stack Overflow** — Stack Exchange API (`api.stackexchange.com/2.3`), `STACK_EXCHANGE_KEY` (10k/day). 9 tag queries + 32 `intitle` queries per run.
3. **Bluesky** — AT Protocol public search (`api.bsky.app/xrpc/app.bsky.feed.searchPosts`), free, no key. Reuses the same ~45 keyword queries as HN (`_HN_QUERIES`). **Not** `public.api.bsky.app` — that host 403s `feed.searchPosts` outright even unauthenticated; the plain `api.bsky.app` host (no "public." prefix) is the one that actually serves it. Rate-limits bursts with a bare 403 (no `Retry-After`) — `fetch_bluesky_posts()` throttles to one request per `_BSKY_REQUEST_INTERVAL` (1.5s). Skips accounts Bluesky itself labels `"bot"` (mostly Reddit/RSS mirror bots).
4. **Reddit + X via Grok** — `web_search` + `x_search` tools on `/v1/responses`. Prompt excludes HN/SO (native sources cover those).
5. **Competitor mentions** — Separate Grok call scoped to Reducto, Unstructured, UiPath, or LlamaIndex mentions (14-day window, neutral chatter counts). See `COMPETITOR_NAMES` in `pain_miner.py`.
6. **Cloud-vendor Q&A forums** — Separate Grok call (`discover_vendor_forum_mentions()`) scoped via `site:` filters to AWS re:Post (`repost.aws`), Microsoft Q&A (`learn.microsoft.com/en-us/answers`), and Google Cloud Community (`googlecloudcommunity.com`) — practitioners stuck directly on Textract / Azure Document Intelligence / Google Document AI. The Grok **prompt itself** deliberately has **no recency window** — these are low-volume, evergreen Q&A archives (not an ephemeral feed like Reddit/HN), and a strict day-count filter *in the search instruction* reliably returns zero even though the sites are full of on-topic pain threads (confirmed by hand 2026-08-17: unconstrained search surfaced specific, current threads like "Inconsistent table extraction with Amazon Textract" that a 14/30-day filter dropped entirely). The prompt asks Grok to prefer the last 12 months but not hard-reject older threads, and reports `posted_at` on every result. That `posted_at` is then enforced **post-hoc, in code**, not in the prompt: `clean_post()` drops any vendor-forum result whose parsed `posted_at` is older than `_VENDOR_FORUM_MAX_AGE_DAYS` (14 days) before it's ever inserted, and the recurring `archive` mode sweep (`archive_stale_vendor_forum()`) separately catches already-inserted `status='new'` vendor-forum rows that cross that same 14-day line — independent of the normal 4-day `discovered_at` sweep, since a vendor-forum row can be freshly *discovered* while the underlying thread itself is old. Rows with an unparseable/null `posted_at` are left alone in both places — staleness can't be judged, so they're kept for a human to triage. Platform value: `vendor-forum`.
7. **LandingAI/ADE own-brand mentions** — Separate Grok call (`discover_ade_mentions()`) scoped to posts naming LandingAI/ADE directly — see `ADE_KEYWORDS` in `pain_miner.py` (`LandingAI`, `Landing AI`, `landing.ai`, `ade.landing.ai`, `Agentic Document Extraction`). Same lower-bar-than-pain-posts model as competitor mentions (neutral chatter/reviews count, 14-day window), but explicitly prioritizes head-to-head comparison questions ("is LandingAI better than DIY?", "ADE vs Textract"). `clean_post()` bypasses the normal categories-or-competitors gate for `platform == "ade"` since a pure comparison/brand-mention post often has no topic category fit. Unlike vendor-forum (whose search is hard-scoped to 3 `site:` filters, so Grok has nothing else to put in the `platform` field), this source's search is broad — Reddit/HN/SO/X are all in scope — so `run_discover()` force-sets `platform = "ade"` in code on every post this source returns rather than trusting Grok to echo the prompt's `"platform": "ade"` schema literal; without that, a category-less comparison post that Grok correctly attributes to its real origin site would silently fail the categories-or-competitors gate instead of reaching the ADE bypass. Platform value: `ade` — its own filter chip in the UI (green, after "other"), distinct from `competitors[]` since LandingAI isn't a competitor of itself.

### Two-pass Grok

- **Pass 1 (classifier)** — Grok turns native HN/SO/Bluesky title-only rows into `{summary, opportunity, categories[]}`. Default posture: KEEP (drops only obvious vendor marketing / off-topic / bot cross-posts). Output post-filtered to prevent Grok inventing URLs.
- **Pass 2 (discovery)** — Reddit/X, competitor mentions, vendor-forum mentions, and ADE mentions each get their own Grok call. Failures are non-fatal — other sources still run.

### Storage (Supabase)

| Table | Purpose |
|---|---|
| `pain_posts` | Queue — status enum: `new\|replied\|deleted\|archived\|completed`. Dedup key: `post_url` UNIQUE. `competitors text[]` with GIN index. |
| `pain_miner_secrets` | RPC secret gate — fully blocked to anon (RLS on, no policies). |
| `pain_miner_errors` | Frontend error capture — fingerprint + rate limit (20/fingerprint/hour). |

### 4 secret-gated RPCs

All RPCs validate `p_secret` against `pain_miner_secrets` before executing.

| RPC | Used by |
|---|---|
| `get_active_pain_posts(p_secret)` | `index.html` — loads triage queue |
| `get_archive_pain_posts(p_secret, p_cutoff)` | `index.html` — archive overlay |
| `set_pain_post_status(p_id, p_status, p_secret, p_user)` | `index.html` — Done/Delete/Reply actions |
| `log_painminer_error(p_message, p_stack, p_page_url, p_user_agent, p_fingerprint, p_secret)` | `error-shim.js` — frontend error reporter |
| `get_pain_posts_for_reports(p_secret, p_cutoff)` | `reports.html` — chart data |

### Security model (3 layers)

1. **nginx perimeter** — Cloudflare DDoS + TLS. `config.js` (which holds the keys) requires htpasswd. All other files are public-served HTML/CSS/JS that have no embedded secrets.
2. **Supabase RLS** — `pain_posts` anon SELECT only. `pain_miner_secrets` and `pain_miner_errors` fully blocked to anon — no read, no write. Service role key (in `.env`) bypasses RLS for server-side inserts.
3. **RPC secret** — the anon key alone cannot read or mutate protected state. Every write and sensitive read goes through a secret-gated RPC.

### Frontend auth flow

`login.html` validates credentials via `fetch('/config.js', {Authorization: 'Basic ...'})` — if nginx returns 200, the credential token is stored in `sessionStorage('pm_auth')` and config.js is loaded. Every app page checks `pm_auth` and redirects to `login.html` if missing. Username is passed as `p_user` to `set_pain_post_status` so every action is audit-logged in `actioned_by + actioned_at`.

### Triage queue sort order

`web/index.html` `render()` sorts `vendor-forum` cards after every other platform, regardless of which platform filter chips are active — a stable partition (`filtered.sort((a, b) => (a.platform === "vendor-forum") - (b.platform === "vendor-forum"))`), so within each group (non-vendor / vendor) the existing relative order is untouched. This only visibly matters when the active filter selection mixes vendor-forum with other platforms; if vendor-forum is the only platform selected there's nothing else to sort around it.

---

## Categories

Defined in `scripts/pain_miner.py:ALLOWED_CATEGORIES` — mirrored in `web/index.html:ALLOWED_CATEGORIES` for display ordering. **Both must be updated together** when adding a new category.

`OCR`, `IDP`, `NLP`, `table-extraction`, `layout-extraction`, `RPA`, `forms-automation`, `healthcare-RCM`, `legal-doc`, `financial-doc`, `prior-auth`, `clinical-documentation`, `pharmaceutical-documentation`, `scientific-literature`, `insurance-doc`, `logistics-doc`, `ADE`

Note: the `ADE` **category** (any post naming LandingAI/ADE, tagged by the classifier) is distinct from the `ade` **platform** value (source 7 — see Platforms/Architecture below). A post can be platform `ade` without category `ADE` and vice versa.

---

## Platforms

`platform` is a free-text column (no DB enum/CHECK) — `pain_miner.py:ALLOWED_PLATFORMS` is the actual allowlist. Current values: `reddit`, `hackernews`, `stackoverflow`, `x`, `bluesky`, `vendor-forum`, `other`, `ade`.

Adding a new platform value touches **more places than it looks like**, and `web/index.html` in particular has two easy-to-miss ones — CSS/chip markup can look completely correct while the JS filter state silently drops every post of the new platform (hit this exactly, 2026-08-17, when Bluesky/vendor-forum posts rendered zero cards despite the RPC returning them fine):

| Concern | File:location |
|---|---|
| Allowed set | `scripts/pain_miner.py` `ALLOWED_PLATFORMS` |
| Normalization branch (Grok variant spellings → canonical) | `scripts/pain_miner.py` `clean_post()` |
| Daily-summary email label | `scripts/pain_miner.py` `_PLATFORM_LABEL` |
| Health-check "silent platform" list — **only add here if the source is genuinely high-volume/expected-daily**; low-volume sources belong out of this list or they'll false-alarm on a normal quiet week | `scripts/pain_miner.py` `run_health_check()` `expected_platforms` |
| Badge CSS | `web/index.html` `.platform-badge.*` |
| Filter chip markup | `web/index.html` `.platform-section` |
| **`state.filterPlatforms` default Set — easy to miss, chip HTML can look "active" while this hardcoded list silently filters the platform out of every render()** | `web/index.html` (chip-handler section, `const state = {...}`) |
| **"clear filters" button reset list — same hardcoded-list trap, second copy** | `web/index.html` (`clear-btn` click handler) |
| Label map (JS) | `web/index.html` `platformLabel()` |
| CSS color var | `web/theme.css` `:root` |
| Pill CSS | `web/reports.html` `.pill.*` |
| Label/color maps (JS) | `web/reports.html` `PLATFORM_LABEL` / `PLATFORM_COLOR` |

`reports.html`'s chart/drill-in logic is fully data-driven (no hardcoded platform list) — it needs the label/color map entries for polish but works correctly even without them.

---

## ADE document focus (target pain types)

The Grok discovery prompts steer toward posts about **visually-rich, structurally complex documents** — NOT commodity plain-text OCR:

- **Healthcare clinical (payer/RCM):** lab reports (multi-section, embedded charts, reference ranges), prior auth packets (clinical criteria, diagnosis codes, physician attestations, payer policy PDFs split by section), 1,000-page mixed patient referral bundles, CMS-1500/UB-04 claims, EOBs, denial letters
- **Healthcare clinical (provider/diagnostics):** requisition forms, pathology reports (TNM/Gleason staging, IHC panels, CAP synoptic templates), NGS/molecular variant reports, genetics/hereditary panel reports
- **Scientific/pharma:** clinical trial protocols, CRFs/consent forms/site reports, drug labels, regulatory filings, batch/stability records, scientific literature (figures + tables + equations)
- **Financial:** appraisals (lending, 200-400pg), loan bundles (multi-doc packets), valuations, PE/VC board decks + IC memos + GP letters, W-2s/1099s/K-1s, prospectuses/S-1s/10-Ks, bank statements, checks
- **Insurance (P&C, life, annuity):** ACORD forms, loss runs, FNOL narratives, claim packets (litigation docs, medical forms, lab results, police reports, handwritten notes), physician statements, carrier/annuity statements
- **Logistics & Transportation:** rail/freight lease agreements + engineering drawings + scanned historical archives, supply-chain submittal sheets + packing slips + spec sheets, aerospace/auto customer POs + repair manuals + OEM manuals
- **Legal:** contracts with schedules, due diligence packets, litigation discovery, title documents
- **Cross-domain signals:** mixed layouts, handwritten annotations + stamps, tables spanning multiple pages, charts needing semantic parsing

Full detail lives in `_ADE_FOCUS_BLOCK` in `pain_miner.py` — that's what's actually injected into the prompts; this list is a summary.

**Reply-angle guardrail:** every prompt's `opportunity` field instruction explicitly forbids suggesting a competitor's product as the fix — even for competitor-mention posts. (Historical bug: Grok would sometimes see a post about e.g. Unstructured pricing pain and suggest "discuss Reducto value" instead of ADE. Fixed 2026-08-17 by adding an explicit NEVER-recommend-a-competitor instruction to all three `opportunity` field prompts.)

**Project-launch filter:** the HN/SO classifier and Reddit/X discovery prompts both explicitly DROP "Show HN"-style posts where the author is presenting something they built (a PDF editor, an OCR app, a doc workspace) rather than describing their own pain. Fixed 2026-08-17 — these were slipping through under the old "vendor marketing" wording since a solo dev's project showcase isn't marketing in the traditional sense.

**Third-party release-discussion filter:** the HN/SO classifier now also explicitly DROPs news/discussion threads about a third party's product release or model launch (e.g. an HN thread titled "Mistral OCR 4.1" linking to a vendor's announcement) — distinct from the Show HN case since the poster isn't the builder. Fixed 2026-08-17 — the classifier was keeping these because the summary/title mentioned OCR capabilities and invited "comparisons to existing tools," which read as pain-adjacent even though no one in the post is actually stuck on a problem.

---

## API resilience

Every external API call uses exponential-backoff retry. Standard helpers are in `pain_miner.py` (`_xai_post_with_retry`). Timeouts:

| Call type | Timeout |
|---|---|
| Grok discovery (web+X search) | 900s |
| Grok classify / fast calls | 120s |

**xAI endpoint rule:** all discovery calls use `/v1/responses` with `web_search` + `x_search` tools. The chat completions endpoint has no search tools — do not use it for discovery.

---

## Error alerting

Brevo alert to `tylerdnorkus@gmail.com` on any unhandled exception. Per-source Grok failures are non-fatal.

| Mode | Alert subject | When |
|---|---|---|
| `discover` | `Pain Miner BROKEN — {date}` | Unhandled exception |
| `discover` | `Pain Miner — Grok classify failed {date}` | HN/SO classifier fails (native pool dropped this cycle) |
| `discover` | `Pain Miner — Grok Reddit/X discovery failed {date}` | Reddit/X pass fails (other sources continue) |
| `discover` | `Pain Miner — Competitor mention discovery failed {date}` | Competitor pass fails (pain queue still populated) |
| `archive` | `Pain Miner BROKEN — {date}` | Unhandled exception during archive sweep |
| `daily-summary` | `Pain Miner BROKEN — {date}` | Aggregation or Brevo send failure |
| `error-digest` | `Pain Miner BROKEN — {date}` | Lock, Supabase query, or batched update failure |
| `error-digest` | `Pain Miner frontend error — {first 80 chars}` | Per new fingerprint (rate-capped 20/fingerprint/hour at RPC) |

---

## Pipeline observability (`_pipeline_log.py`)

`_pipeline_log.py` writes run-level lifecycle data (`pipeline_runs` table) and errors (`pipeline_errors` table) to Supabase. Currently pointing at the LAI NorkForce-HubRM project.

**When migrating the database:** either run `schema/pipeline_observability.sql` in your new Supabase project (it creates both tables + the RPCs), or remove the `_pipeline_log` import + calls from `pain_miner.py` if you don't need centralized observability.

Product code in observability: `PM`.

---

## Known gotchas

- **`sudo` env-var ordering** — `sudo` strips env vars. Use `sudo PAINMINER_USER=x bash deploy.sh`, not `PAINMINER_USER=x sudo bash`.
- **Cloudflare SSL mode** — the nginx conf requires a Cloudflare Origin CA cert (Full/strict mode). If you switch to Let's Encrypt, update the `ssl_certificate` paths in `deploy/painminer-nginx.conf`.
- **`SUPABASE_ANON_KEY` is different from `SUPABASE_KEY`** — the anon key is public-by-design and used by the browser. The service role key (`SUPABASE_KEY`) must never reach the browser.
- **Stack Exchange key quota** — 10,000 requests/day. Without a key the anonymous limit is 300/day per IP. The script reads `quota_remaining` from each response and bails at 5 left.
- **Grok `posted_at` non-ISO values** — Grok sometimes returns `"3 days ago"` for `posted_at`. `clean_post()` (`_normalize_posted_at`) validates it parses as ISO-8601 before writing to Supabase and stores NULL otherwise — an unparseable string in a `timestamptz` column would otherwise reject the ENTIRE insert batch, silently dropping every other valid post in that run.
- **`--dry-run` must skip all API calls** — dry-run is for env validation + Supabase connectivity only. No Grok calls. This is enforced — the weekly smoke test will timeout in 120s if an API call slips in.
- **PostgREST NULL-status pattern** — never use `.not_.in_(col, [...])` when rows can have NULL in that column. SQL `NULL NOT IN (...)` evaluates to NULL, silently dropping rows. Use `.or_("status.not.in.(x,y),status.is.null")` instead.
- **Bluesky's "public" API host isn't actually public for search** — `public.api.bsky.app/xrpc/app.bsky.feed.searchPosts` 403s unconditionally, even fully unauthenticated (Bluesky locked that endpoint down there to deter scraping). The unauthenticated host that actually serves it is `api.bsky.app` (no "public." prefix) — confirmed by hand 2026-08-17. That host also rate-limits bursts with a bare 403 and no `Retry-After` header, which looks identical to a hard block unless you space requests out (see `_BSKY_REQUEST_INTERVAL`).
- **Grok's `web_search` can't reliably date-filter niche/low-volume domains** — a `site:repost.aws` / `site:learn.microsoft.com/answers` / `site:googlecloudcommunity.com` search with a "last N days" instruction reliably returns zero results even though the content exists and is indexed (confirmed by hand: the same query with no date constraint immediately surfaced specific, on-topic threads). High-traffic domains (Reddit, HN, X) don't show this problem — likely a freshness-metadata gap specific to these smaller forums. Fix: don't hard-filter on recency for low-volume sources: ask for a soft preference instead and let `posted_at` (best-effort, often `null`) carry the staleness signal into triage.

---

## Git remote

```bash
cd /Users/tnork/Desktop/pain-miner
git remote -v   # origin → https://github.com/tnork/pain-miner.git
git push origin main
```
