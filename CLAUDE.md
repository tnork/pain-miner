# Pain Miner — Standalone Application

Engagement-triage queue. Surfaces practitioner pain posts on Reddit / Hacker News / StackOverflow / X where someone is discussing real OCR / NLP / IDP / document-processing pain, so a human can publicly reply with a helpful suggestion. **Not lead-gen** — no CRM enrollment, no contact resolution.

Extracted from the `lai-central-research-agent` monorepo on 2026-05-29 and split into this standalone repo for independent deployment and database migration.

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

## Deploy (server)

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

### 4 discovery sources per `discover` run

1. **Hacker News** — Algolia API (`hn.algolia.com/api/v1/search`), free, no key. ~20 keyword queries.
2. **Stack Overflow** — Stack Exchange API (`api.stackexchange.com/2.3`), `STACK_EXCHANGE_KEY` (10k/day). 9 tag queries + 32 `intitle` queries per run.
3. **Reddit + X via Grok** — `web_search` + `x_search` tools on `/v1/responses`. Prompt excludes HN/SO (native sources cover those).
4. **Competitor mentions** — Separate Grok call scoped to Reducto, Unstructured, UiPath, or LlamaIndex mentions (14-day window, neutral chatter counts). See `COMPETITOR_NAMES` in `pain_miner.py`.

### Two-pass Grok

- **Pass 1 (classifier)** — Grok turns native HN/SO title-only rows into `{summary, opportunity, categories[]}`. Default posture: KEEP (drops only obvious vendor marketing / off-topic). Output post-filtered to prevent Grok inventing URLs.
- **Pass 2 (discovery)** — Reddit/X + competitor mentions each get their own Grok call. Failures are non-fatal — other sources still run.

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

---

## Categories

Defined in `scripts/pain_miner.py:ALLOWED_CATEGORIES` — mirrored in `web/index.html:ALLOWED_CATEGORIES` for display ordering. **Both must be updated together** when adding a new category.

`OCR`, `IDP`, `NLP`, `table-extraction`, `layout-extraction`, `RPA`, `forms-automation`, `healthcare-RCM`, `legal-doc`, `financial-doc`, `prior-auth`, `clinical-documentation`, `pharmaceutical-documentation`, `scientific-literature`, `insurance-doc`, `logistics-doc`

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

---

## Git remote

```bash
cd /Users/tnork/Desktop/pain-miner
git remote -v   # origin → https://github.com/tnork/pain-miner.git
git push origin main
```
