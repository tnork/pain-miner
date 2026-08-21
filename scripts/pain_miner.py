"""
pain_miner.py — Pain Miner pipeline (engagement triage, NOT lead-gen).

Surfaces practitioner posts on Reddit, Hacker News, StackOverflow, X, and other
developer forums where someone is discussing real pain around OCR / NLP /
intelligent document processing / clinical / pharma / scientific docs / etc.
The output is a queue of posts an outreach human can read and reply to in real
time — there is no Apollo enrollment, no contact resolution.

Modes:
  --mode discover  (default)  Grok web+X search → dedup → insert into Supabase
  --mode archive              Flip status='new' rows older than 4 days to 'archived';
                               also archives vendor-forum rows whose actual post
                               date is older than 14 days

Schedule (DO Droplet, all UTC; EDT shown in parens — see deploy/crontab.example):
  Discovery:  2 13 * * 1-6, 0 17 * * 1-6, 0 22 * * 1-6   (9:02a/1p/6p ET, Mon-Sat)
  Archive:    0 3 * * *                                  (3a UTC = 11p ET)

Required env: XAI_API_KEY, SUPABASE_URL, SUPABASE_KEY, BREVO_API_KEY
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import pathlib
import re
import ssl
import sys
import textwrap
import unicodedata
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import certifi
except ImportError:
    certifi = None

from pydantic import BaseModel, Field, ValidationError


# ---------------------------------------------------------------------------
# .env loader — runs before any os.environ read so cron jobs that don't go
# through a shell wrapper still pick up the keys. Looks for .env next to the
# repo root (the parent of scripts/). Silent no-op if the file is absent.
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    env_path = repo_root / ".env"
    if not env_path.exists():
        return
    pattern = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*(.*)$")
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = pattern.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        # Strip a single pair of matching quotes if present.
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        # Don't clobber values already set in the real environment (cron / systemd
        # env files / shell exports take precedence over the on-disk .env).
        os.environ.setdefault(key, val)


_load_dotenv()

# ---------------------------------------------------------------------------
# Categories taxonomy — must match the frontend chip list exactly.
# Posts with no category in this list are dropped.
# ---------------------------------------------------------------------------

ALLOWED_CATEGORIES = [
    "OCR",
    "IDP",
    "NLP",
    "table-extraction",
    "layout-extraction",
    "RPA",
    "forms-automation",
    "healthcare-RCM",
    "legal-doc",
    "financial-doc",
    "prior-auth",
    "clinical-documentation",
    "pharmaceutical-documentation",
    "scientific-literature",
    "insurance-doc",
    "logistics-doc",
    "ADE",
]
CATEGORY_DEFINITIONS = textwrap.dedent("""
    - OCR                          — basic optical character recognition pain (accuracy, handwriting, multi-language)
    - IDP                          — intelligent document processing as a category (end-to-end document AI)
    - NLP                          — natural language processing applied to documents (entity extraction, classification, summarization)
    - table-extraction             — extracting tables from PDFs or scans
    - layout-extraction            — bounding boxes, document structure, page segmentation
    - RPA                          — robotic process automation, document-driven workflow bots
    - forms-automation             — automating intake/submission/structured forms
    - healthcare-RCM               — revenue cycle management, claims, EOBs, denials, billing
    - legal-doc                    — legal documents, contracts, briefs, due diligence, e-discovery
    - financial-doc                — invoices, statements, financial filings, receipts, bank docs
    - prior-auth                   — healthcare prior authorization specifically
    - clinical-documentation       — patient records, charting, EHR notes, clinical operations
    - pharmaceutical-documentation — drug regulatory filings, clinical trial docs, drug labels, pharma SOPs
    - scientific-literature        — research papers, citations, PDF extraction for science/academia
    - insurance-doc                — P&C / life / annuity insurance — ACORD forms, loss runs, FNOL, claims packets, underwriting
    - logistics-doc                — logistics & transportation — freight/rail records, supply chain, warehousing, fleet/aerospace docs
    - ADE                          — post specifically names LandingAI / ADE (Agentic Document Extraction), including comparisons or reviews
""").strip()


# What LandingAI ADE actually solves vs commodity OCR. Pain Miner is most
# valuable when it surfaces posts where someone is struggling with visually
# complex documents — not just plain typed text on a page. This block is
# injected into every discovery + classifier prompt so Grok prioritizes
# posts that match ADE's real ICP (and so the classifier keeps them).
_ADE_FOCUS_BLOCK = textwrap.dedent("""
    LANDINGAI ADE FOCUS — what makes a post HIGH VALUE for this queue:

    ADE (Agentic Document Extraction) differentiates on **visually rich,
    structurally complex documents** where commodity OCR + generic LLMs
    break down. Prefer posts about pain extracting structured data from
    documents that are visually messy, multi-modal, or domain-specific:

    HIGH-VALUE document types — call these out aggressively, especially when
    practitioners describe accuracy / structure / cross-reference pain:

    - Healthcare clinical (payer / RCM):
      • Lab reports (multi-section, embedded charts, reference ranges, footnotes)
      • Prior authorization packets — clinical criteria, diagnosis codes,
        physician attestations arriving as mixed PDFs; payer policy PDFs that
        must be split by section
      • Patient referrals (1,000-page mixed PDFs: medical records,
        authorizations, legal docs, mixed handwriting + print, stamps/signatures)
      • Discharge summaries, operative notes
      • EOBs, prior auth requests/approvals, denial letters, coverage
        verification docs, claims forms (CMS-1500, UB-04)
    - Healthcare clinical (provider / diagnostics):
      • Requisition forms (patient demographics, ordering physician + NPI,
        ICD-10 codes, tests ordered, insurance/billing info, clinical history)
      • Pathology reports (gross/microscopic description, TNM/Gleason staging,
        IHC panel result tables, CAP synoptic cancer protocol templates)
      • NGS / molecular reports (variant tables: gene, nucleotide/amino acid
        change, VAF, classification, clinical significance, QC metrics)
      • Genetics / hereditary panel reports (variant classification tables,
        family history pedigree text, insurance coverage determination forms)
    - Healthcare scientific / pharma:
      • Scientific literature (figures + tables + equations + supplementary data,
        multi-column layouts, references)
      • Clinical trial protocols, IRB submissions, drug labels, regulatory filings
      • CRFs (case report forms), consent forms, site reports across global trials
        (varied formats, languages, handwriting)
      • Batch records, stability data, analytical reports validated against
        dossier templates pre-FDA/EMA filing (often still on paper)
    - Financial / investor-grade:
      • Appraisals — commercial + consumer lending, 200–400 page reports with
        200–300 fields for compliance/credit-risk decisioning
      • Loan bundles / packets (multi-document combos, 10–100 docs per loan;
        scanned/text mixed)
      • Valuations and appraisal reports (financial tables + footnotes + schedules)
      • Investor reports / quarterly fund letters (prose + tables + charts);
        PE/VC board decks (80-100 pages), Investment Committee memos, GP
        letters, capital account statements
      • W-2s, 1099s, K-1s, tax forms (structured fields, multi-copy variants,
        often bundled into one large scanned PDF)
      • Prospectuses, S-1s, 10-Ks (mixed prose + financials + footnotes)
      • Bank statements, brokerage statements, checks
    - Insurance (P&C, life, annuity):
      • ACORD forms, loss runs, FNOL (first notice of loss — narrative text)
      • Claim packets mixing litigation docs, medical forms, lab results,
        police reports, handwritten notes, Excel sheets with complex tabs, EOBs
      • Budget/underwriting documents (dense, repetitive-formatting reports)
      • Physician statements with handwritten notes and margin drawings
      • Carrier documents — annuity/life insurance statements, in-force policy
        analysis; every carrier formats differently
    - Logistics & Transportation:
      • Rail/freight: lease agreements, legal records, engineering drawings,
        audit records, scanned historical records spanning decades (mixed
        typed, handwritten, and degraded fax scans)
      • Supply chain / warehousing: submittal sheets, packing slips, purchase
        orders, spec sheets with dimensional tables (images + sub-diagrams)
      • Aerospace / auto: customer POs (PDF/Word/scanned, varying quality),
        repair manuals with diagrams, OEM equipment/maintenance manuals
    - Legal:
      • Contracts with embedded schedules / exhibits / addenda
      • Due diligence packets, litigation discovery bundles
      • Title documents, deeds, mortgage packets
    - Cross-domain "messy" features that strongly signal ADE fit:
      • Mixed layouts (tables next to charts next to prose)
      • Multi-page docs with cross-references between pages
      • Handwritten annotations + stamps + signatures alongside typed text
      • Forms with conditional logic / dependent fields
      • Charts, plots, scientific figures that need to be parsed semantically
      • Tables that span multiple pages with continuation rules

    LOWER-PRIORITY (still acceptable but less interesting):
    - Generic plain-text OCR ("how do I OCR a screenshot?")
    - Simple invoice line-item extraction (commodity OCR handles this fine)
    - Plain-text NLP unrelated to document structure
""").strip()

ALLOWED_PLATFORMS = {"reddit", "hackernews", "stackoverflow", "x", "bluesky", "vendor-forum", "other", "ade"}

# Vendor-forum discovery has no recency window in its Grok prompt (see
# vendor_forum_prompt() — a hard date filter there reliably returns zero
# results on these low-volume archive sites). That means Grok can surface
# genuinely stale threads; this ceiling drops them post-hoc, both at
# ingestion (clean_post) and via the recurring archive sweep (archive_old),
# instead of constraining the search itself.
_VENDOR_FORUM_MAX_AGE_DAYS = 14

# ---------------------------------------------------------------------------
# Helpers (mirrors weekly_accounts_agent.py for consistency)
# ---------------------------------------------------------------------------

def https_context() -> ssl.SSLContext:
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


def extract_json_from_text(text: str) -> dict[str, object]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            return json.loads(stripped[start : end + 1])
        raise


def response_text(payload: dict[str, object]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") in {"output_text", "text"} and "text" in content:
                parts.append(content["text"])
    if parts:
        return "\n".join(parts)
    raise RuntimeError("Could not find xAI response text.")


# ---------------------------------------------------------------------------
# Brevo error alerts
# ---------------------------------------------------------------------------

_ALERT_TO = "tylerdnorkus@gmail.com"
_SUMMARY_TO = "tyler.norkus@landing.ai,andrea.kropp@landing.ai,vish.panchal@landing.ai,miki.ilic@landing.ai"

# The deployed triage-UI domain — linked in the daily summary email and probed
# by --mode health-check. Override via PAINMINER_DOMAIN so a fresh deploy to a
# different droplet/domain needs a .env edit, not a code change.
_DOMAIN = os.getenv("PAINMINER_DOMAIN", "painminer.muisbien.com")


def _brevo_send(to_addr: str, subject: str, *, text: str | None = None, html: str | None = None) -> None:
    """Send via Brevo. Pass text=, html=, or both. At least one must be set."""
    if not text and not html:
        raise ValueError("_brevo_send requires text= or html=")
    api_key = os.environ["BREVO_API_KEY"]
    from_raw = os.getenv("EMAIL_FROM", "LandingAI Weekly Research <research@mg.muisbien.com>")
    sender_email = from_raw.split("<")[-1].rstrip(">") if "<" in from_raw else from_raw
    sender_name  = from_raw.split("<")[0].strip()       if "<" in from_raw else "LandingAI Weekly Research"
    payload: dict[str, object] = {
        "sender":  {"email": sender_email, "name": sender_name},
        "to":      [{"email": a.strip()} for a in to_addr.split(",") if a.strip()],
        "subject": subject,
    }
    if text:
        payload["textContent"] = text
    if html:
        payload["htmlContent"] = html
    request = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={"api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30, context=https_context()) as response:
        response.read()


def _brevo_send_text(to_addr: str, subject: str, body: str) -> None:
    _brevo_send(to_addr, subject, text=body)


def send_error_alert(subject: str, detail: str) -> None:
    body = (
        f"The Pain Miner pipeline encountered an error and could not complete.\n\n"
        f"Time:  {dt.datetime.now().isoformat()}\n\n"
        f"Error:\n{detail}"
    )
    try:
        if os.getenv("BREVO_API_KEY"):
            _brevo_send_text(_ALERT_TO, subject, body)
            print(f"[alert] Error notification sent to {_ALERT_TO}.", file=sys.stderr)
        else:
            print(f"[alert] BREVO_API_KEY not set — error not emailed. {detail}", file=sys.stderr)
    except Exception as exc:
        print(f"[warn] Could not send error alert: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class PainPost(BaseModel):
    platform: str
    post_url: str
    post_title: str
    author_handle: str | None = None
    posted_at: str | None = None        # ISO 8601 string from Grok
    summary: str
    opportunity: str = ""
    categories: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)  # exact strings from COMPETITOR_NAMES


class PainPostsBatch(BaseModel):
    summary: str = ""
    posts: list[PainPost] = Field(default_factory=list)


# Competitor monitoring — these names are looked up explicitly in a fourth
# discovery pass and surfaced via the "competitors" filter chip in the UI.
# Posts that mention competitors are NOT required to also be pain posts; they
# can be neutral chatter, reviews, or comparison threads.
COMPETITOR_NAMES = ["Reducto", "Unstructured", "UiPath", "LlamaIndex"]

# Own-brand monitoring — LandingAI / ADE mentioned by name, including
# head-to-head comparison questions ("is LandingAI better than DIY?"). Looked
# up in a sixth discovery pass (see discover_ade_mentions) and force-tagged
# with the dedicated "ade" platform value (green chip in the UI) in code
# (run_discover), regardless of which site Grok actually found the post on —
# unlike vendor-forum, this source's search isn't restricted to a few site:
# filters, so it can't rely on Grok reliably echoing the platform literal.
ADE_KEYWORDS = ["LandingAI", "Landing AI", "landing.ai", "ade.landing.ai", "Agentic Document Extraction"]


# ---------------------------------------------------------------------------
# Grok discovery
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Native HN fetcher — Algolia API, free, no key.
# https://hn.algolia.com/api
# ---------------------------------------------------------------------------

# Keyword queries we run against Algolia. Each runs separately; results dedup
# by URL so duplicate hits across queries collapse cleanly.
_HN_QUERIES = [
    # Generic doc-AI categories
    "OCR",
    "IDP document",
    "document extraction",
    "document AI",
    "intelligent document",
    "PDF parsing",
    "PDF table extraction",
    "table extraction",
    "layout extraction",
    "Textract",
    "form recognizer",
    "Donut model",
    "LayoutLM",
    "Tesseract",
    "azure form",
    "invoice extraction",
    # Healthcare-clinical complex docs
    "lab report extraction",
    "prior authorization",
    "patient referral",
    "EOB extraction",
    "CMS-1500",
    "EHR documentation",
    "clinical documentation",
    "pathology report",
    "discharge summary",
    # Pharma + scientific
    "pharma documentation",
    "clinical trial documents",
    "scientific paper extraction",
    "research paper figures",
    "drug label",
    # Financial / investor-grade
    "10-K extraction",
    "prospectus extraction",
    "investor report parsing",
    "valuation report",
    "W-2 OCR",
    "tax form extraction",
    "bank statement extraction",
    "fund letter parsing",
    # Legal complex docs
    "contract extraction",
    "due diligence document",
    "title document OCR",
    # Visual-richness pain signals
    "handwritten form OCR",
    "scanned form extraction",
    "multi-column PDF",
    "PDF figure extraction",
]

# Extra context (story body / first comment) included in the classifier prompt
# so Grok can judge pain quality from more than the title alone.
_HN_BODY_FIELD = "story_text"


def fetch_hn_posts(hours_back: int = 168) -> list[PainPost]:
    """Pull HN stories matching doc-AI / OCR / IDP keywords from the last N hours.

    Returns skeleton PainPost objects (no summary/opportunity/categories) — those
    fields get filled in by classify_with_grok() in a second pass. Body text
    (when available) is stashed on the object and threaded into the classifier
    prompt for higher signal."""
    since = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_back)).timestamp())
    seen: set[str] = set()
    out: list[PainPost] = []
    # Per-post body snippets, keyed by post_url. Not part of the persisted schema —
    # passed transiently into the classifier and then discarded.
    out_bodies: dict[str, str] = {}
    for q in _HN_QUERIES:
        url = (
            "https://hn.algolia.com/api/v1/search"
            f"?query={urllib.parse.quote(q)}"
            "&tags=story"
            f"&numericFilters=created_at_i>{since}"
            "&hitsPerPage=20"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "lai-painminer/1.0"})
            with urllib.request.urlopen(req, timeout=30, context=https_context()) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            print(f"[warn] HN Algolia fetch failed for query={q!r}: {exc}", file=sys.stderr)
            continue
        for hit in data.get("hits", []):
            obj_id = hit.get("objectID")
            if not obj_id:
                continue
            permalink = f"https://news.ycombinator.com/item?id={obj_id}"
            if permalink in seen:
                continue
            seen.add(permalink)
            title = hit.get("title") or hit.get("story_title") or ""
            if not title.strip():
                continue
            body = (hit.get(_HN_BODY_FIELD) or "").strip()
            if body:
                # Strip simple HTML tags so the classifier prompt stays clean.
                body = re.sub(r"<[^>]+>", " ", body)
                body = re.sub(r"\s+", " ", body).strip()
                out_bodies[permalink] = body[:500]
            out.append(PainPost(
                platform="hackernews",
                post_url=permalink,
                post_title=title.strip()[:500],
                author_handle=(hit.get("author") or "").strip() or None,
                posted_at=hit.get("created_at"),
                summary="",      # filled by classifier
                opportunity="",  # filled by classifier
                categories=[],   # filled by classifier
            ))
    # Stash bodies on a module-level dict the classifier reads.
    _CLASSIFIER_BODIES.update(out_bodies)
    return out


# Module-level scratch space for body snippets passed transiently into the
# classifier prompt. Cleared at the top of each run_discover() call.
_CLASSIFIER_BODIES: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Native Stack Overflow fetcher — Stack Exchange API, free, no key.
# https://api.stackexchange.com/docs
# Anonymous calls: 300/day per IP. Registered key: 10,000/day.
# Set STACK_EXCHANGE_KEY in .env to use a registered key (register at stackapps.com).
# ---------------------------------------------------------------------------
_SE_KEY: str | None = os.environ.get("STACK_EXCHANGE_KEY")

_SO_TAGS = [
    "ocr",
    "azure-form-recognizer",
    "aws-textract",
    "tesseract",
    "document-ai",
    "pdf-parsing",
    "table-extraction",
    "google-cloud-document-ai",
    "form-recognition",
]

# Free-text title queries — fills the gap when SO has no recent tagged posts.
# `intitle=` only matches the question title.
#
# QUOTA: STACK_EXCHANGE_KEY is a registered key (10,000 requests/day). The
# fetcher does (len(_SO_TAGS) + len(_SO_INTITLE_QUERIES)) requests per
# discover run × 3 runs/day (Mon-Sat). At ~40 queries/cycle that's ~120/day —
# well under quota, plenty of headroom to add more. Without a key, the
# anonymous limit is 300 requests/day/IP — keep total queries per cycle low
# enough to fit that if STACK_EXCHANGE_KEY is ever unset.
_SO_INTITLE_QUERIES = [
    # Generic — high-signal title keywords
    "OCR",
    "IDP",
    "document extraction",
    "PDF extraction",
    "form extraction",
    "table extraction",
    "multi-column PDF",
    "Textract",
    "Document AI",
    "Form Recognizer",
    "LayoutLM",
    # Document type-specific — Tyler's ADE targets
    "lab report",
    "prior authorization",
    "EOB",
    "CMS-1500",
    "patient referral",
    "10-K",
    "prospectus",
    "investor report",
    "valuation",
    "W-2",
    "tax form",
    "bank statement",
    "scientific paper",
    "clinical trial",
    "due diligence",
    "ACORD form",
    "FNOL",
    "loss run",
    "claims adjudication",
    "bill of lading",
    "packing slip",
]


def _fetch_so_endpoint(url: str) -> tuple[list[dict], int | None]:
    """Shared fetch + JSON-parse for Stack Exchange API.

    Returns (items, quota_remaining). quota_remaining is None on transport
    error or when the API doesn't include it in the response. Stack Exchange
    sends `quota_remaining` and `quota_max` in every successful response so we
    can short-circuit the loop in fetch_so_posts when we're near zero."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "lai-painminer/1.0"})
        with urllib.request.urlopen(req, timeout=30, context=https_context()) as resp:
            body_bytes = resp.read()
            # Stack Exchange returns gzip even when we don't request it.
            try:
                text = body_bytes.decode("utf-8")
                # Probe — if the first chars aren't JSON-like, decompress.
                if text and text.lstrip()[:1] not in ("{", "["):
                    raise UnicodeDecodeError("se", b"", 0, 1, "not json")
            except UnicodeDecodeError:
                import gzip
                text = gzip.decompress(body_bytes).decode("utf-8")
            payload = json.loads(text) or {}
            quota_remaining = payload.get("quota_remaining")
            return (payload.get("items", []) or []), (
                int(quota_remaining) if isinstance(quota_remaining, int) else None
            )
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"[warn] Stack Exchange fetch failed ({url[:80]}...): {exc}", file=sys.stderr)
        return [], None


def fetch_so_posts(hours_back: int = 168) -> list[PainPost]:
    """Pull recent Stack Overflow questions via tag and intitle queries.

    Bails out of remaining queries once quota_remaining drops to 5 so we
    don't 100% exhaust the anonymous 300/day pool — leaves headroom for
    other Pain Miner runs later in the same UTC day."""
    fromdate = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_back)).timestamp())
    seen: set[str] = set()
    out: list[PainPost] = []
    quota_floor = 5
    quota_remaining: int | None = None

    def _quota_ok() -> bool:
        # First call always proceeds (quota_remaining starts None).
        return quota_remaining is None or quota_remaining > quota_floor

    # 1. Tag-based queries — high precision, low recall.
    for tag in _SO_TAGS:
        if not _quota_ok():
            print(f"[warn] Stack Exchange quota near zero ({quota_remaining} left); "
                  f"skipping remaining tag queries.", file=sys.stderr)
            break
        _key_param = f"&key={_SE_KEY}" if _SE_KEY else ""
        url = (
            "https://api.stackexchange.com/2.3/questions"
            f"?fromdate={fromdate}"
            "&order=desc&sort=creation"
            f"&tagged={urllib.parse.quote(tag)}"
            f"&site=stackoverflow&pagesize=30{_key_param}"
        )
        items, quota_remaining = _fetch_so_endpoint(url)
        for item in items:
            _so_collect(item, seen, out)

    # 2. Title-text queries — broader recall, picks up untagged posts.
    for q in _SO_INTITLE_QUERIES:
        if not _quota_ok():
            print(f"[warn] Stack Exchange quota near zero ({quota_remaining} left); "
                  f"skipping remaining intitle queries.", file=sys.stderr)
            break
        _key_param = f"&key={_SE_KEY}" if _SE_KEY else ""
        url = (
            "https://api.stackexchange.com/2.3/search"
            f"?fromdate={fromdate}"
            "&order=desc&sort=creation"
            f"&intitle={urllib.parse.quote(q)}"
            f"&site=stackoverflow&pagesize=30{_key_param}"
        )
        items, quota_remaining = _fetch_so_endpoint(url)
        for item in items:
            _so_collect(item, seen, out)

    if quota_remaining is not None and quota_remaining < 50:
        print(f"[pain_miner] Stack Exchange quota_remaining={quota_remaining} "
              f"(daily 300/IP) — pipeline still ran but watch this.", file=sys.stderr)
    return out


def _so_collect(item: dict, seen: set[str], out: list[PainPost]) -> None:
    """Mutator helper — turns a Stack Exchange JSON item into a PainPost row."""
    link = (item.get("link") or "").strip()
    if not link or link in seen:
        return
    seen.add(link)
    title = (item.get("title") or "").strip()
    if not title:
        return
    ts = item.get("creation_date")
    posted_at_iso = (
        dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()
        if isinstance(ts, (int, float)) else None
    )
    owner = item.get("owner") or {}
    tags = item.get("tags") or []
    if tags:
        # Stash tags as a body snippet so the classifier sees them.
        _CLASSIFIER_BODIES[link] = "Tags: " + ", ".join(tags[:8])
    out.append(PainPost(
        platform="stackoverflow",
        post_url=link,
        post_title=title[:500],
        author_handle=(owner.get("display_name") or "").strip() or None,
        posted_at=posted_at_iso,
        summary="",
        opportunity="",
        categories=[],
    ))


# ---------------------------------------------------------------------------
# Native Bluesky fetcher — AT Protocol public search, free, no key.
# https://docs.bsky.app/docs/api/app-bsky-feed-search-posts
#
# NOTE: "public.api.bsky.app" (the documented "public" host) 403s on
# feed.searchPosts even fully unauthenticated — Bluesky locked that specific
# endpoint down there to deter scraping. The plain "api.bsky.app" host (no
# "public." prefix) still serves it with no auth required. That host also
# rate-limits bursts with a bare 403 (no Retry-After header) — space calls
# out or you'll get spurious 403s that have nothing to do with being blocked.
# ---------------------------------------------------------------------------

_BSKY_REQUEST_INTERVAL = 1.5  # seconds between calls — stays under the burst limit


def _fetch_bsky_endpoint(query: str, since_iso: str, limit: int = 40) -> list[dict]:
    url = (
        "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
        f"?q={urllib.parse.quote(query)}&sort=latest&limit={limit}"
        f"&since={urllib.parse.quote(since_iso)}&lang=en"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "lai-painminer/1.0"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=30, context=https_context()) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("posts", []) or []
        except urllib.error.HTTPError as exc:
            if exc.code == 403 and attempt == 0:
                time.sleep(5)  # likely a burst-rate 403, not a hard block — back off once
                continue
            print(f"[warn] Bluesky fetch failed for query={query!r}: HTTP {exc.code}", file=sys.stderr)
            return []
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            print(f"[warn] Bluesky fetch failed for query={query!r}: {exc}", file=sys.stderr)
            return []
    return []


def fetch_bluesky_posts(hours_back: int = 168) -> list[PainPost]:
    """Pull recent Bluesky posts matching doc-AI / OCR / IDP keywords.

    Reuses _HN_QUERIES for keyword coverage. Returns skeleton PainPost objects
    (no summary/opportunity/categories) — filled in by classify_with_grok()
    alongside HN/SO, same pattern as the other native sources. Skips accounts
    Bluesky itself labels "bot" — cross-posting bots that just mirror Reddit
    threads or RSS feeds add noise without a real practitioner voice."""
    since_iso = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen: set[str] = set()
    out: list[PainPost] = []
    for i, q in enumerate(_HN_QUERIES):
        if i > 0:
            time.sleep(_BSKY_REQUEST_INTERVAL)
        for post in _fetch_bsky_endpoint(q, since_iso):
            author = post.get("author") or {}
            labels = [l.get("val") for l in (author.get("labels") or [])]
            if "bot" in labels:
                continue
            uri = post.get("uri") or ""
            handle = author.get("handle") or ""
            if not uri or not handle:
                continue
            rkey = uri.rsplit("/", 1)[-1]
            permalink = f"https://bsky.app/profile/{handle}/post/{rkey}"
            if permalink in seen:
                continue
            text = ((post.get("record") or {}).get("text") or "").strip()
            if not text:
                continue
            seen.add(permalink)
            out.append(PainPost(
                platform="bluesky",
                post_url=permalink,
                post_title=text[:500],
                author_handle=f"@{handle}",
                posted_at=(post.get("record") or {}).get("createdAt"),
                summary="",
                opportunity="",
                categories=[],
            ))
    return out


# ---------------------------------------------------------------------------
# Grok discovery for Reddit + X — narrowed scope, since HN/SO are now native.
# ---------------------------------------------------------------------------

def grok_discovery_prompt() -> str:
    today = dt.date.today().isoformat()
    return textwrap.dedent(f"""
        Today's date is {today}.

        You are scouting **Reddit and X (Twitter)** for practitioner posts describing
        real, current pain points around document AI. HN and Stack Overflow are
        handled separately by direct APIs, so DO NOT return posts from those
        platforms — focus exclusively on Reddit and X.

        These posts will be triaged by a human who may publicly reply with a helpful
        suggestion. NOT lead-gen, engagement opportunities. Quality is everything.

        SEARCH SCOPE:
        - Use web_search aggressively against `site:reddit.com` to find posts in
          subreddits like r/MachineLearning, r/dataengineering, r/Python,
          r/datascience, r/healthcareIT, r/legaltech, r/aws, r/MLQuestions,
          r/LangChain, r/learnmachinelearning, r/computervision, r/LocalLLaMA,
          r/ChatGPT, etc. — but don't limit to that list.
        - Use x_search to find X/Twitter posts from devs, researchers, clinicians,
          and founders discussing concrete document-AI problems.
        - Posts must be from the last 7 days. Newer is better.

        QUALIFYING POST:
        - Real practitioner describing a concrete, current document-processing problem.
        - Specific pain — "Textract messes up tables on lab reports", "fine-tuning
          Donut for EOBs" — NOT generic buzzword posts.
        - Replyable: open thread, responsive author, public platform.

        DISQUALIFY:
        - Vendor marketing, sponsored posts, launch/announcement posts — including
          "I built X" project showcases (an open-source tool, app, or workflow
          product the author made), even when the project is document-processing
          related (a PDF editor, OCR app, doc-workspace tool). The author showing
          off a thing they built is NOT the same as the author being stuck on a
          problem. Only keep if the post is the author describing their OWN
          current pain, not presenting a finished project.
        - Generic "what's the best OCR library?" discussions with no concrete pain.
        - Posts older than 7 days.
        - Hacker News or Stack Overflow posts (we get those elsewhere).
        - Any post NOT written primarily in English — skip non-English posts entirely.

        {_ADE_FOCUS_BLOCK}

        TOPICS:
        {CATEGORY_DEFINITIONS}

        OUTPUT — return ONLY valid JSON, no prose:
        {{
          "summary": "1-sentence summary of what you found this run",
          "posts": [
            {{
              "platform": "reddit | x",
              "post_url": "https://...",
              "post_title": "string",
              "author_handle": "u/foo | @foo | null",
              "posted_at": "ISO-8601 best-estimate timestamp",
              "summary": "2-3 sentences: what the post is about and why it's a real pain",
              "opportunity": "1-2 sentences: how an ADE rep could helpfully reply. Always frame the value around LandingAI ADE specifically — NEVER suggest discussing or recommending a competitor's product (Reducto, Unstructured, or others) as the solution.",
              "categories": ["exact strings from the list above"]
            }}
          ]
        }}

        Return up to 25 posts. Cast a wide net — return any post that's a plausible
        engagement opportunity, not just slam-dunk pain posts. Return only Reddit + X.
    """).strip()


def _xai_post_with_retry(req: urllib.request.Request, timeout: int, ssl_ctx) -> dict:
    """urlopen wrapper with exponential backoff on 429 or 5xx (up to 3 retries)."""
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if (exc.code == 429 or exc.code >= 500) and attempt < 3:
                wait = 15 * (2 ** attempt)  # 15s, 30s, 60s
                print(f"[warn] xAI HTTP {exc.code} — backing off {wait}s (attempt {attempt + 1}/3)", flush=True)
                time.sleep(wait)
            else:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"xAI HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < 3:
                wait = 15 * (2 ** attempt)
                print(f"[warn] xAI network error — backing off {wait}s (attempt {attempt + 1}/3)", flush=True)
                time.sleep(wait)
            else:
                raise RuntimeError(f"xAI network error: {exc}") from exc
    raise RuntimeError("xAI request failed after 3 retries")


def _grok_call(prompt: str, *, with_search: bool = True) -> dict[str, object]:
    """Shared xAI Responses API caller. Returns the parsed JSON payload."""
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        raise RuntimeError("XAI_API_KEY is not configured.")
    body: dict[str, object] = {
        "model": os.getenv("XAI_MODEL", "grok-4.3"),
        "input": [{"role": "user", "content": prompt}],
    }
    if with_search:
        body["tools"] = [{"type": "web_search"}, {"type": "x_search"}]
    request = urllib.request.Request(
        "https://api.x.ai/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    return _xai_post_with_retry(request, 900, https_context())


def _parse_grok_batch(payload: dict[str, object]) -> PainPostsBatch:
    raw = response_text(payload)
    try:
        data = extract_json_from_text(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"xAI returned unparseable JSON: {exc}\nFirst 500 chars: {raw[:500]}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"xAI returned non-object JSON: {type(data).__name__}")
    # Validate each post individually — drop malformed entries rather than
    # failing the whole batch when Grok omits a required field on one post.
    raw_posts = data.get("posts", [])
    valid: list[PainPost] = []
    for i, p in enumerate(raw_posts if isinstance(raw_posts, list) else []):
        try:
            valid.append(PainPost.model_validate(p))
        except ValidationError as exc:
            print(f"[warn] _parse_grok_batch: dropping post[{i}] (missing fields): {exc}", file=sys.stderr)
    return PainPostsBatch(summary=str(data.get("summary", "")), posts=valid)


def discover_with_grok() -> PainPostsBatch:
    """Reddit + X discovery via Grok with web_search + x_search tools."""
    return _parse_grok_batch(_grok_call(grok_discovery_prompt(), with_search=True))


# ---------------------------------------------------------------------------
# Grok classifier — second pass that scores native HN/SO candidates.
# Filters out non-pain posts and fills in summary / opportunity / categories.
# ---------------------------------------------------------------------------

def _classify_prompt(candidates: list[PainPost]) -> str:
    today = dt.date.today().isoformat()
    payload = []
    for p in candidates:
        item = {
            "platform":      p.platform,
            "post_url":      p.post_url,
            "post_title":    p.post_title,
            "author_handle": p.author_handle,
            "posted_at":     p.posted_at,
        }
        body = _CLASSIFIER_BODIES.get(p.post_url)
        if body:
            item["body"] = body
        payload.append(item)
    candidate_json = json.dumps(payload, indent=2)
    return textwrap.dedent(f"""
        Today's date is {today}.

        You are CLASSIFYING a list of Hacker News + Stack Overflow + Bluesky posts
        that we already pulled via their public APIs (Algolia for HN, Stack
        Exchange for SO, the AT Protocol search endpoint for Bluesky). Each one
        is REAL, ON-TOPIC, and was matched on a doc-AI / OCR / IDP / document-
        extraction keyword. The pre-filter has already done the relevance work —
        your job is to ENRICH each post and only drop the obviously-bad ones.
        Bluesky posts are short, X-like microblog posts (not always phrased as a
        question) — judge pain the same way you would an X post: is the author
        stuck on / venting about a document-processing problem?

        DEFAULT POSTURE: KEEP posts where the author is describing a document-
        processing PAIN or asking a technical question about it. A junior
        analyst-style "where do I start with OCR?" question is still a fine reply
        opportunity. So is a Stack Overflow question with no answer yet. Keep them.

        DROP if the post is clearly:
        - A "Show HN:" or launch/announcement post where the author BUILT and is
          PRESENTING a project, app, tool, workflow, or open-source library —
          even if it's a document-processing tool (a PDF editor, an OCR app, a
          doc-workspace product). These are project showcases, not pain. The
          giveaway: the title/body describes a thing the author made and its
          features, not a problem the author is stuck on. DROP these regardless
          of how relevant the tool sounds.
        - Vendor marketing or self-promotion ("I built X, check it out")
        - A blog post / tutorial / announcement (not a question or pain)
        - A news/discussion thread about a THIRD PARTY's product release, model
          launch, or version update (e.g. "Mistral OCR 4.1", "Company X launches
          document AI tool") — even when the title mentions OCR/IDP capabilities
          or invites comparisons to existing tools. Commenters MIGHT eventually
          gripe about a tool in the thread, but the post itself is release news,
          not a practitioner stuck on a problem. DROP unless the title/body is
          itself someone describing a concrete pain (not merely "reactions to
          a launch").
        - Completely off-topic (matched the keyword in passing — e.g. "OCR" was in
          a list of unrelated tools, not the subject of the post)
        - An automated bot cross-post (e.g. a bot account that mirrors Reddit
          threads, RSS feeds, or GitHub commits onto Bluesky/X verbatim) — no
          real practitioner voice behind it, just a mirrored feed
        - Locked / deleted / not replyable
        - Not written primarily in English — drop non-English posts entirely

        Ask yourself: is the author STUCK on a document-processing problem (KEEP),
        or SHOWING OFF something they built that touches documents (DROP)?

        For every post you keep, fill in:
        - summary: 2-3 sentences on what the post is about and what kind of help
          the author seems to want. Use the body snippet if provided.
        - opportunity: 1-2 sentences on a concrete angle for an ADE rep's reply
          (specific reference to the pain, not generic). Frame the value around
          LandingAI ADE specifically — NEVER suggest discussing or recommending
          a competitor's product (Reducto, Unstructured, or others) as the fix.
        - categories: one or more EXACT strings from the list below.

        You MAY use web_search to peek at a post's body if title + body snippet
        aren't enough. Do NOT search for or add new posts.

        {_ADE_FOCUS_BLOCK}

        TOPICS (categories must be EXACT strings):
        {CATEGORY_DEFINITIONS}

        CANDIDATES (filter + enrich only — do NOT add posts):
        {candidate_json}

        OUTPUT — return ONLY valid JSON, no prose:
        {{
          "summary": "1-sentence summary of what you kept this run",
          "posts": [
            {{
              "platform": "<copy from input>",
              "post_url": "<copy from input>",
              "post_title": "<copy from input>",
              "author_handle": "<copy from input or null>",
              "posted_at": "<copy from input>",
              "summary": "...",
              "opportunity": "...",
              "categories": ["..."]
            }}
          ]
        }}
    """).strip()


def classify_with_grok(candidates: list[PainPost]) -> PainPostsBatch:
    """Send native HN/SO candidates to Grok for filtering + categorization.

    Empty input returns an empty batch (no Grok call). Grok may drop low-fit
    posts; the returned list is a subset of the input. We post-filter the
    response to enforce that — Grok occasionally pulls in extra URLs via
    web_search, or rewrites a canonical URL — without this guard, those
    invented posts would slip in as if they came from the HN/SO APIs."""
    if not candidates:
        return PainPostsBatch(summary="No native candidates to classify.", posts=[])
    raw = _parse_grok_batch(_grok_call(_classify_prompt(candidates), with_search=True))
    allowed = {c.post_url for c in candidates}
    kept = [p for p in raw.posts if p.post_url in allowed]
    if len(kept) != len(raw.posts):
        print(f"[pain_miner] Classifier returned {len(raw.posts) - len(kept)} post(s) "
              f"outside the input set; dropped.", file=sys.stderr)
    return PainPostsBatch(summary=raw.summary, posts=kept)


# ---------------------------------------------------------------------------
# Competitor mention monitoring — fourth source. Surfaces posts that name
# any of the doc-AI competitors we track (see COMPETITOR_NAMES). Lower bar
# than pain posts; these posts can be neutral chatter, reviews, or
# comparisons. Filed in the same pain_posts table with `competitors[]`
# populated so the frontend "competitors" chip can isolate them.
# ---------------------------------------------------------------------------

def competitor_mentions_prompt() -> str:
    today = dt.date.today().isoformat()
    names_list = ", ".join(COMPETITOR_NAMES)
    return textwrap.dedent(f"""
        Today's date is {today}.

        You are scouting Reddit, Hacker News, Stack Overflow, X (Twitter), and
        developer forums for posts that **mention these document-AI competitors
        by name**: {names_list}.

        These posts will appear in a "Competitors" filter on a triage UI so the
        outreach team can see what people are saying about competitors. They
        do NOT need to be pain posts. They can be:
        - Reviews ("we tried Reducto and it was...")
        - Comparison threads ("Reducto vs Unstructured vs Textract")
        - Help requests ("Unstructured is failing on our PDFs")
        - Neutral mentions in tooling discussions
        - Negative experiences (these are gold)

        SEARCH SCOPE:
        - Use web_search and x_search broadly. Cover Reddit, HN, Stack Overflow,
          X, dev blogs.
        - Posts must be from the last 14 days (slightly wider window than the
          pain-post pipeline since competitor mentions are rarer).

        DROP only:
        - Vendor self-promotion BY the competitor (e.g. Reducto's own marketing)
        - Generic crypto/finance/sports/RPA posts that happen to use one of these
          names out of context (e.g. "unstructured data" as a plain phrase, or
          "UiPath" in an unrelated RPA-only automation post with no document-AI angle)
        - Locked / deleted threads

        For each kept post, identify which competitor(s) are mentioned. The
        `competitors` array MUST contain only exact strings from this list:
        {names_list}.

        TOPICS (categories — fill in if relevant, leave empty array if pure
        competitor chatter with no doc-AI category fit):
        {CATEGORY_DEFINITIONS}

        OUTPUT — return ONLY valid JSON, no prose:
        {{
          "summary": "1-sentence summary of competitor chatter this run",
          "posts": [
            {{
              "platform": "reddit | hackernews | stackoverflow | x | other",
              "post_url": "https://...",
              "post_title": "string",
              "author_handle": "u/foo | @foo | null",
              "posted_at": "ISO-8601 best-estimate timestamp",
              "summary": "2-3 sentences: what the post says about the competitor",
              "opportunity": "1-2 sentences: how an ADE rep could helpfully engage (often: stay out of vendor-bashing, but useful as signal). If a reply is warranted, frame the value around LandingAI ADE specifically — NEVER suggest discussing or recommending a competitor's product, including the one named in this post, as the solution.",
              "categories": ["topic categories if any, else []"],
              "competitors": ["exact strings from the competitor list above, e.g. \"Reducto\", \"Unstructured\", \"UiPath\", \"LlamaIndex\""]
            }}
          ]
        }}

        Return up to 15 posts. If you find none, return an empty array.
    """).strip()


def discover_competitor_mentions() -> PainPostsBatch:
    """Fourth source — Grok with web_search + x_search scoped to competitor mentions."""
    return _parse_grok_batch(_grok_call(competitor_mentions_prompt(), with_search=True))


# ---------------------------------------------------------------------------
# Cloud-vendor Q&A forum monitoring — fifth source. AWS re:Post, Microsoft
# Q&A, and Google Cloud Community are where practitioners get stuck on the
# cloud OCR/IDP services directly (Textract, Azure AI Document Intelligence,
# Google Document AI) — high pain-signal, and each is a public, publicly
# repliable Q&A thread, same shape as Stack Overflow. No native API for any
# of the three (unlike SO's Stack Exchange API), so this rides Grok's
# web_search the same way the Reddit/X pass does, just scoped by site:.
# ---------------------------------------------------------------------------

def vendor_forum_prompt() -> str:
    today = dt.date.today().isoformat()
    return textwrap.dedent(f"""
        Today's date is {today}.

        You are scouting **cloud-vendor Q&A/community forums** for practitioners
        stuck on a document-processing problem with a cloud OCR/IDP service:
        - AWS re:Post — site:repost.aws (Amazon Textract, Amazon Comprehend threads)
        - Microsoft Q&A — site:learn.microsoft.com/en-us/answers (Azure AI Document
          Intelligence / Form Recognizer threads)
        - Google Cloud Community — site:googlecloudcommunity.com (Document AI threads)

        Use web_search scoped to these three site: filters. DO NOT return Reddit,
        HN, Stack Overflow, or X posts — those are covered by other passes.

        QUALIFYING POST:
        - A practitioner asking a concrete question about Textract / Azure
          Document Intelligence (Form Recognizer) / Google Document AI —
          accuracy problems, table/layout parsing failures, cost complaints,
          API limitations, integration struggles.
        - DO NOT hard-reject on a recency window. These forums are low-volume
          and function as evergreen Q&A archives (unlike Reddit/HN's ephemeral
          front page) — a thread from months ago is still a live, findable,
          repliable page, and a strict recency filter will come back empty
          even though the sites are full of exactly this content. Prefer
          posts from the last 12 months when you have enough of them; only
          reach further back if recent threads are too thin. Report your
          best-effort posted_at for each post (or null if you can't tell) and
          let a human judge staleness — do not reject a post just because you
          can't confirm it's recent.
        - Open, publicly repliable thread — not locked.

        DISQUALIFY:
        - Official vendor announcements or docs pages (not a practitioner asking
          for help)
        - Fully resolved threads with an accepted answer that already solves it
          well (no reply value left)
        - Posts unrelated to document/OCR/IDP processing (these forums cover
          all of AWS/Azure/GCP, not just doc-AI — most hits will be irrelevant,
          stay strict)
        - Not written primarily in English

        {_ADE_FOCUS_BLOCK}

        TOPICS (categories must be EXACT strings):
        {CATEGORY_DEFINITIONS}

        OUTPUT — return ONLY valid JSON, no prose:
        {{
          "summary": "1-sentence summary of what you found this run",
          "posts": [
            {{
              "platform": "vendor-forum",
              "post_url": "https://...",
              "post_title": "string",
              "author_handle": "string or null",
              "posted_at": "ISO-8601 best-estimate timestamp",
              "summary": "2-3 sentences: what the practitioner is stuck on",
              "opportunity": "1-2 sentences: how an ADE rep could helpfully reply. Frame the value around LandingAI ADE specifically — NEVER suggest discussing or recommending a competitor's product as the fix.",
              "categories": ["exact strings from the list above"]
            }}
          ]
        }}

        Return up to 15 posts. If you find none, return an empty array — these
        forums are lower-volume, a quiet run is expected and fine.
    """).strip()


def discover_vendor_forum_mentions() -> PainPostsBatch:
    """Fifth source — Grok with web_search scoped to AWS re:Post / MS Q&A / GCP Community."""
    return _parse_grok_batch(_grok_call(vendor_forum_prompt(), with_search=True))


# ---------------------------------------------------------------------------
# LandingAI / ADE own-brand mention monitoring — sixth source. Mirrors
# discover_competitor_mentions but scoped to our own name (see ADE_KEYWORDS)
# instead of a competitor's. Surfaces posts where someone is asking about,
# evaluating, or directly comparing LandingAI/ADE — including head-to-head
# questions like "is LandingAI better than DIY?" — so the team can jump into
# the conversation with a grounded answer. Filed under the dedicated "ade"
# platform value (not the site it was found on) so it gets its own filter
# chip — run_discover() force-sets platform="ade" on every post this source
# returns; the "platform": "ade" literal below is just for Grok's own
# consistency and isn't load-bearing.
# ---------------------------------------------------------------------------

def ade_mentions_prompt() -> str:
    today = dt.date.today().isoformat()
    keywords_list = ", ".join(f'"{k}"' for k in ADE_KEYWORDS)
    return textwrap.dedent(f"""
        Today's date is {today}.

        You are scouting Reddit, Hacker News, Stack Overflow, X (Twitter), and
        developer forums for posts that **mention LandingAI or its Agentic
        Document Extraction (ADE) product by name**. Match any of: {keywords_list}.

        These posts will appear under a dedicated "ADE" filter on a triage UI
        so the team can see what people are saying about LandingAI/ADE and
        jump into the conversation. They do NOT need to be pain posts. They
        can be:
        - Head-to-head comparison questions — e.g. "Is LandingAI better than
          building this in-house?", "ADE vs Textract/Unstructured/Reducto",
          "should I use LandingAI or DIY this?" (HIGH PRIORITY — these are the
          best reply opportunities)
        - Someone evaluating or asking for opinions on LandingAI/ADE
        - Reviews or experience reports (positive or negative — negative ones
          are especially valuable to see)
        - Neutral mentions in tooling/stack discussions

        SEARCH SCOPE:
        - Use web_search and x_search broadly. Cover Reddit, HN, Stack
          Overflow, X, dev blogs, and forums.
        - Posts must be from the last 14 days.

        DROP only:
        - LandingAI's own official posts/marketing/job listings (self-
          promotion by us — not useful for triage, we already know about it)
        - False-positive keyword collisions unrelated to the company — e.g.
          generic use of the phrase "landing page" or "landing.ai" matching
          an unrelated domain/handle
        - Locked / deleted threads

        For each kept post, briefly note in `summary` what's actually being
        asked or said, and whether it's a head-to-head comparison question.

        TOPICS (categories — fill in if relevant, leave empty array if pure
        brand mention/comparison chatter with no doc-AI category fit):
        {CATEGORY_DEFINITIONS}

        OUTPUT — return ONLY valid JSON, no prose:
        {{
          "summary": "1-sentence summary of ADE/LandingAI chatter this run",
          "posts": [
            {{
              "platform": "ade",
              "post_url": "https://...",
              "post_title": "string",
              "author_handle": "u/foo | @foo | null",
              "posted_at": "ISO-8601 best-estimate timestamp",
              "summary": "2-3 sentences: what the post says about LandingAI/ADE, and whether it's a head-to-head comparison question",
              "opportunity": "1-2 sentences: how the team could helpfully reply — a grounded, factual answer or proof point. No hype, no overt pitch.",
              "categories": ["topic categories if any, else []"]
            }}
          ]
        }}

        Return up to 15 posts. If you find none, return an empty array — this
        is a low-volume source, a quiet run is expected and fine.
    """).strip()


def discover_ade_mentions() -> PainPostsBatch:
    """Sixth source — Grok with web_search + x_search scoped to LandingAI/ADE mentions."""
    return _parse_grok_batch(_grok_call(ade_mentions_prompt(), with_search=True))


# ---------------------------------------------------------------------------
# Filtering / normalization
# ---------------------------------------------------------------------------

# Tracking params we drop. Anything outside this list is preserved — Hacker News
# and many other platforms encode the post identifier in the query string
# (?id=12345), so a blanket strip would collapse distinct posts into one row.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_referrer",
    "fbclid", "gclid", "msclkid", "yclid", "dclid",
    "mc_cid", "mc_eid", "ref", "ref_src", "ref_url", "_ga", "igshid",
}


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url
    parsed = urllib.parse.urlsplit(url)
    # Strip fragment, drop only tracking params, preserve order of the rest.
    kept = [
        (k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    new_query = urllib.parse.urlencode(kept, doseq=True)
    path = parsed.path
    # Strip trailing slash from path UNLESS the path is just "/".
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, new_query, ""))


def _is_english(text: str) -> bool:
    """Return False if more than 20% of alphabetic chars are non-Latin-script.

    Catches CJK, Arabic, Hebrew, Cyrillic, Thai, etc. without a third-party
    library. Latin-script European languages (French, German, Spanish) pass
    because they use the same script as English.
    """
    letters = [c for c in text if unicodedata.category(c).startswith("L")]
    if not letters:
        return True
    non_latin = sum(1 for c in letters if ord(c) > 0x024F)
    return (non_latin / len(letters)) < 0.20


def _normalize_posted_at(raw: str | None) -> str | None:
    """Grok sometimes returns relative dates ("3 days ago") instead of the
    requested ISO-8601 timestamp. `posted_at` is a `timestamptz` column in
    Supabase — an unparseable value there rejects the ENTIRE insert batch
    (including unrelated valid posts), so normalize non-ISO strings to None
    rather than let them reach the DB."""
    value = (raw or "").strip()
    if not value:
        return None
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value
    except ValueError:
        return None


def clean_post(post: PainPost) -> PainPost | None:
    """Coerce to allowed values; drop the post if it's unsalvageable."""
    platform = (post.platform or "").lower().strip()
    if "reddit" in platform:
        platform = "reddit"
    elif "hacker" in platform or platform == "hn":
        platform = "hackernews"
    elif "stack" in platform or platform == "so":
        platform = "stackoverflow"
    elif platform in {"twitter", "x.com", "x"}:
        platform = "x"
    elif platform in {"bsky", "bluesky"}:
        platform = "bluesky"
    elif "vendor" in platform or "repost" in platform or "re:post" in platform or platform in {
        "aws", "aws-repost", "ms-qna", "microsoft q&a", "gcp-community",
        "google cloud community", "cloud-forum",
    }:
        platform = "vendor-forum"
    elif platform not in ALLOWED_PLATFORMS:
        platform = "other"

    url = normalize_url(post.post_url or "")
    # Defense against XSS via javascript:/data:/file: hrefs — we render this URL
    # in the frontend as an <a href="..."> target. The frontend has its own
    # safeHref() check, but rejecting at ingest time is the cleaner gate.
    if not (url.startswith("https://") or url.startswith("http://")):
        return None

    cats = [c for c in (post.categories or []) if c in ALLOWED_CATEGORIES]

    # Normalize competitor names to the canonical casing in COMPETITOR_NAMES.
    canon = {c.lower(): c for c in COMPETITOR_NAMES}
    comps_seen: list[str] = []
    for c in (post.competitors or []):
        key = (c or "").strip().lower()
        if key in canon and canon[key] not in comps_seen:
            comps_seen.append(canon[key])

    # Allow a post through if it has EITHER a real category, a competitor
    # mention, or is an ADE own-brand mention. Category-less chatter/
    # comparison threads are still legit cards for these sources.
    if not cats and not comps_seen and platform != "ade":
        return None

    title = (post.post_title or "").strip()
    if not title:
        return None

    if not _is_english(title):
        return None

    summary = (post.summary or "").strip()
    if not summary:
        return None

    posted_at = _normalize_posted_at(post.posted_at)
    if platform == "vendor-forum" and posted_at is not None:
        posted_dt = dt.datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        if posted_dt.tzinfo is None:
            # fromisoformat() accepts offset-less strings (e.g. Grok returning
            # "2026-08-19T10:00:00" with no "Z"/offset) as valid ISO-8601, but
            # they come out naive — comparing naive to the aware "now" below
            # raises TypeError. Assume UTC, matching how we treat "Z".
            posted_dt = posted_dt.replace(tzinfo=dt.timezone.utc)
        if posted_dt < dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=_VENDOR_FORUM_MAX_AGE_DAYS):
            return None

    return PainPost(
        platform=platform,
        post_url=url,
        post_title=title[:500],
        author_handle=(post.author_handle or "").strip() or None,
        posted_at=posted_at,
        summary=summary,
        opportunity=(post.opportunity or "").strip(),
        categories=cats,
        competitors=comps_seen,
    )


# ---------------------------------------------------------------------------
# Supabase IO
# ---------------------------------------------------------------------------

def supabase_client():
    from supabase import create_client
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    return create_client(url, key)


def existing_post_urls(sb, urls: list[str]) -> set[str]:
    """Return the subset of `urls` that already exist in pain_posts."""
    if not urls:
        return set()
    found: set[str] = set()
    # Supabase IN clause has a soft limit — chunk to be safe.
    for i in range(0, len(urls), 100):
        chunk = urls[i : i + 100]
        resp = sb.table("pain_posts").select("post_url").in_("post_url", chunk).execute()
        for row in (resp.data or []):
            found.add(row["post_url"])
    return found


def merge_existing_competitors(sb, candidates: list[PainPost], existing_urls: set[str]) -> int:
    """For URLs that are already in pain_posts but the new candidate has
    competitors[] set, merge those competitors into the existing row.

    Without this, the competitors filter undercounts overlap exactly where it
    matters most: a pain post discovered first by the regular pipeline that
    later turns up in a competitor scan would never get its competitors[]
    populated, because plain dedup drops the duplicate before insert."""
    incoming: dict[str, set[str]] = {}
    for p in candidates:
        if not p.competitors or p.post_url not in existing_urls:
            continue
        incoming.setdefault(p.post_url, set()).update(p.competitors)
    if not incoming:
        return 0
    # Fetch current competitors for each URL, then UPDATE only if new ones to add.
    resp = (
        sb.table("pain_posts")
        .select("post_url, competitors")
        .in_("post_url", list(incoming.keys()))
        .execute()
    )
    updated = 0
    for row in (resp.data or []):
        url = row["post_url"]
        existing = set(row.get("competitors") or [])
        merged = sorted(existing | incoming.get(url, set()))
        if set(merged) == existing:
            continue
        sb.table("pain_posts").update({"competitors": merged}).eq("post_url", url).execute()
        updated += 1
    return updated


def insert_posts(sb, posts: list[PainPost]) -> int:
    if not posts:
        return 0
    rows = []
    for p in posts:
        rows.append({
            "platform":      p.platform,
            "post_url":      p.post_url,
            "post_title":    p.post_title,
            "author_handle": p.author_handle,
            "posted_at":     p.posted_at,
            "summary":       p.summary,
            "opportunity":   p.opportunity or None,
            "categories":    p.categories,
            "competitors":   p.competitors,
            # status, discovered_at, status_changed_at default in the table
        })
    inserted = 0
    failures: list[str] = []
    for i in range(0, len(rows), 100):
        batch = rows[i : i + 100]
        try:
            resp = (
                sb.table("pain_posts")
                .upsert(batch, on_conflict="post_url", ignore_duplicates=True)
                .execute()
            )
            inserted += len(resp.data or [])
        except Exception as exc:
            # Log + keep going so a single bad batch doesn't sink the whole run,
            # but remember the failure so we can raise at the end and trip the
            # Brevo alert. Silent failure here used to mask Supabase outages.
            msg = f"batch [{i}:{i+len(batch)}] failed: {exc}"
            print(f"[warn] {msg}", file=sys.stderr)
            failures.append(msg)
    if failures:
        raise RuntimeError(
            f"{len(failures)} of {(len(rows) + 99) // 100} insert batches failed; "
            f"first error: {failures[0]}"
        )
    return inserted


def archive_old(sb, days: int = 4) -> int:
    """Flip status='new' rows older than `days` (by discovered_at) to 'archived'."""
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    resp = (
        sb.table("pain_posts")
        .update({"status": "archived", "status_changed_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        .eq("status", "new")
        .lt("discovered_at", cutoff)
        .execute()
    )
    return len(resp.data or [])


def archive_stale_vendor_forum(sb, days: int = _VENDOR_FORUM_MAX_AGE_DAYS) -> int:
    """Flip status='new' vendor-forum rows whose actual post date (`posted_at`)
    is older than `days` to 'archived' — independent of `archive_old`'s
    discovered_at-based sweep, since vendor-forum posts can sit fresh in the
    queue (recently discovered) while the underlying thread itself is old.
    Rows with posted_at NULL are left alone; staleness can't be judged."""
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    resp = (
        sb.table("pain_posts")
        .update({"status": "archived", "status_changed_at": dt.datetime.now(dt.timezone.utc).isoformat()})
        .eq("status", "new")
        .eq("platform", "vendor-forum")
        .lt("posted_at", cutoff)
        .execute()
    )
    return len(resp.data or [])


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def run_discover(dry_run: bool = False) -> int:
    print(f"[pain_miner] {dt.datetime.now().isoformat()} — starting discovery run")

    if dry_run:
        # Smoke-test / dev mode: validate env + Supabase connectivity only.
        # Skips all Grok API calls so the check completes in seconds rather than
        # minutes. The smoke test runs this weekly to catch import errors and DB
        # outages — NOT to exercise Grok discovery.
        print("[pain_miner] --dry-run: checking env + Supabase connectivity (no Grok calls).")
        sb = supabase_client()
        result = sb.table("pain_posts").select("id", count="exact").limit(0).execute()
        print(f"[pain_miner] --dry-run OK — pain_posts reachable ({result.count} rows in table).")
        return 0

    # Reset transient classifier-context dict from any prior in-process call.
    _CLASSIFIER_BODIES.clear()

    # ---- Source 1: HN via Algolia (native, free, no key) ----
    print("[pain_miner] Fetching HN via Algolia...")
    try:
        hn_raw = fetch_hn_posts()
    except Exception as exc:
        print(f"[warn] HN fetch failed: {exc}", file=sys.stderr)
        hn_raw = []
    print(f"  HN candidates: {len(hn_raw)}")

    # ---- Source 2: Stack Overflow via Stack Exchange (native, free, no key) ----
    print("[pain_miner] Fetching Stack Overflow via Stack Exchange API...")
    try:
        so_raw = fetch_so_posts()
    except Exception as exc:
        print(f"[warn] SO fetch failed: {exc}", file=sys.stderr)
        so_raw = []
    print(f"  SO candidates: {len(so_raw)}")

    # ---- Source 2b: Bluesky via AT Protocol public search (native, free, no key) ----
    print("[pain_miner] Fetching Bluesky via public search...")
    try:
        bsky_raw = fetch_bluesky_posts()
    except Exception as exc:
        print(f"[warn] Bluesky fetch failed: {exc}", file=sys.stderr)
        bsky_raw = []
    print(f"  Bluesky candidates: {len(bsky_raw)}")

    # ---- Classify HN+SO+Bluesky with Grok (drops non-pain, fills summary/opportunity/categories) ----
    _grok_alert_parts: list[str] = []  # collect per-source failures; one combined alert at end
    classified_native: list[PainPost] = []
    native_pool = hn_raw + so_raw + bsky_raw
    if native_pool:
        print(f"[pain_miner] Classifying {len(native_pool)} HN/SO/Bluesky candidates with Grok...")
        try:
            classify_batch = classify_with_grok(native_pool)
            classified_native = list(classify_batch.posts)
            print(f"  Grok kept {len(classified_native)} of {len(native_pool)}: "
                  f"{classify_batch.summary or '(no summary)'}")
        except Exception as exc:
            # Non-fatal — Reddit/X still goes through. Collect for end-of-run summary.
            print(f"[warn] Grok classify failed, dropping HN/SO/Bluesky this run: {exc}", file=sys.stderr)
            _grok_alert_parts.append(f"classify (HN/SO/Bluesky): {type(exc).__name__}: {exc}")

    # ---- Source 3: Reddit + X via Grok (narrowed prompt) ----
    # Source-level Grok failures are non-fatal but DO alert — a silent xAI
    # outage that drops Reddit/X coverage looks identical to "low news week"
    # without an alert.
    print("[pain_miner] Querying Grok for Reddit + X discovery...")
    try:
        grok_batch = discover_with_grok()
        grok_posts = list(grok_batch.posts)
        print(f"  Grok returned {len(grok_posts)} Reddit/X posts: "
              f"{grok_batch.summary or '(no summary)'}")
    except Exception as exc:
        print(f"[warn] Grok Reddit/X discovery failed: {exc}", file=sys.stderr)
        _grok_alert_parts.append(f"reddit/x discovery: {type(exc).__name__}: {exc}")
        grok_posts = []

    # ---- Source 4: Competitor mention monitoring (see COMPETITOR_NAMES) ----
    # Lower bar than pain posts — neutral chatter, reviews, comparisons all count.
    # Surfaced in the UI under the "competitors" filter chip.
    print("[pain_miner] Querying Grok for competitor mentions...")
    try:
        comp_batch = discover_competitor_mentions()
        comp_posts = list(comp_batch.posts)
        print(f"  Grok returned {len(comp_posts)} competitor-mention posts: "
              f"{comp_batch.summary or '(no summary)'}")
    except Exception as exc:
        print(f"[warn] Competitor mention discovery failed: {exc}", file=sys.stderr)
        _grok_alert_parts.append(f"competitor mentions: {type(exc).__name__}: {exc}")
        comp_posts = []

    # ---- Source 5: Cloud-vendor Q&A forums (AWS re:Post, MS Q&A, GCP Community) ----
    # Lower, sporadic volume expected — these are niche forums. A quiet run is fine.
    print("[pain_miner] Querying Grok for cloud-vendor Q&A forum discovery...")
    try:
        vendor_batch = discover_vendor_forum_mentions()
        vendor_posts = list(vendor_batch.posts)
        print(f"  Grok returned {len(vendor_posts)} vendor-forum posts: "
              f"{vendor_batch.summary or '(no summary)'}")
    except Exception as exc:
        print(f"[warn] Vendor-forum discovery failed: {exc}", file=sys.stderr)
        _grok_alert_parts.append(f"vendor-forum discovery: {type(exc).__name__}: {exc}")
        vendor_posts = []

    # ---- Source 6: LandingAI/ADE own-brand mention monitoring (see ADE_KEYWORDS) ----
    # Lower bar than pain posts — comparison questions, reviews, neutral mentions all count.
    # Surfaced in the UI under the dedicated "ade" platform filter chip.
    print("[pain_miner] Querying Grok for LandingAI/ADE mentions...")
    try:
        ade_batch = discover_ade_mentions()
        ade_posts = list(ade_batch.posts)
        # Force platform to "ade" in code rather than trusting Grok to follow the
        # prompt's schema literal. Unlike vendor_forum_prompt() (hard-scoped to 3
        # site: filters, so Grok has nothing else to put in the field), this
        # source's search is broad — Reddit/HN/SO/X are all in scope — so Grok can
        # legitimately report a post's true origin site instead. Every post this
        # source returns is, by definition, an ADE mention, so it always gets the
        # "ade" tag regardless of what Grok wrote.
        for p in ade_posts:
            p.platform = "ade"
        print(f"  Grok returned {len(ade_posts)} ADE-mention posts: "
              f"{ade_batch.summary or '(no summary)'}")
    except Exception as exc:
        print(f"[warn] ADE mention discovery failed: {exc}", file=sys.stderr)
        _grok_alert_parts.append(f"ADE mentions: {type(exc).__name__}: {exc}")
        ade_posts = []

    # One combined alert per run if any Grok source failed — never one per failure.
    if _grok_alert_parts:
        send_error_alert(
            subject=f"Pain Miner — Grok failed ({len(_grok_alert_parts)} source(s)) {dt.date.today().isoformat()}",
            detail="\n\n".join(_grok_alert_parts),
        )
        try:
            from _pipeline_log import log_error as _log_error  # type: ignore
            _log_error("PM", "pain_miner.py", "discover",
                       f"Grok failed for {len(_grok_alert_parts)} source(s): {'; '.join(p.split(':')[0] for p in _grok_alert_parts)}",
                       send_email=False)
        except Exception:
            pass

    # ---- Combine sources ----
    combined = classified_native + grok_posts + comp_posts + vendor_posts + ade_posts
    if not combined:
        print("[pain_miner] No candidates from any source. Exiting.")
        return 0
    print(f"[pain_miner] Combined pool: {len(combined)} posts "
          f"({len(classified_native)} HN/SO/Bluesky + {len(grok_posts)} Reddit/X "
          f"+ {len(comp_posts)} competitor mentions + {len(vendor_posts)} vendor-forum "
          f"+ {len(ade_posts)} ADE mentions)")

    # ---- Validate + URL-clean + drop posts missing required fields ----
    cleaned: list[PainPost] = []
    for raw in combined:
        c = clean_post(raw)
        if c is not None:
            cleaned.append(c)
    dropped = len(combined) - len(cleaned)
    if dropped:
        print(f"[pain_miner] Dropped {dropped} posts during clean (bad URL, missing fields, no allowed category)")

    # In-batch URL dedup (cross-source overlap can happen if Grok finds an HN/SO
    # link, or the competitor/ADE scans find the same URL as a regular discovery
    # pass). Keep the first occurrence but union in competitors[] from any later
    # duplicate so a competitor-mention post never loses its tag just because a
    # plain discovery pass happened to surface the same URL first. Same idea for
    # platform="ade": ade_posts is concatenated last in `combined`, so an ADE
    # mention that's ALSO a regular pain post (e.g. "tried LandingAI and X, still
    # stuck on Y") would otherwise lose its "ade" tag to whichever source's
    # occurrence came first — promote the kept post to "ade" instead of dropping it.
    seen_urls: dict[str, int] = {}
    deduped: list[PainPost] = []
    for p in cleaned:
        idx = seen_urls.get(p.post_url)
        if idx is not None:
            existing = deduped[idx]
            updates: dict[str, object] = {}
            if p.competitors:
                merged_competitors = existing.competitors + [
                    c for c in p.competitors if c not in existing.competitors
                ]
                if merged_competitors != existing.competitors:
                    updates["competitors"] = merged_competitors
            if p.platform == "ade" and existing.platform != "ade":
                updates["platform"] = "ade"
            if updates:
                deduped[idx] = existing.model_copy(update=updates)
            continue
        seen_urls[p.post_url] = len(deduped)
        deduped.append(p)
    if len(deduped) != len(cleaned):
        print(f"[pain_miner] Cross-source dedup removed {len(cleaned) - len(deduped)} duplicate URLs")

    # ---- Dedup against rows already in pain_posts ----
    sb = supabase_client()
    urls = [p.post_url for p in deduped]
    already = existing_post_urls(sb, urls)
    new = [p for p in deduped if p.post_url not in already]
    by_platform = _by_platform(new)
    print(f"[pain_miner] {len(already)} already in DB · {len(new)} new "
          f"(by platform: {by_platform})")

    # When a competitor scan finds a URL that's already in the queue from an
    # earlier discovery, MERGE the new competitors[] onto the existing row
    # rather than drop it. Without this we'd silently undercount overlaps.
    if dry_run:
        overlaps = [p for p in deduped if p.post_url in already and p.competitors]
        if overlaps:
            print(f"[pain_miner] (dry-run) would merge competitors[] onto "
                  f"{len(overlaps)} pre-existing row(s).")
    else:
        merged = merge_existing_competitors(sb, deduped, already)
        if merged:
            print(f"[pain_miner] Merged competitors[] onto {merged} pre-existing row(s).")

    if dry_run:
        print("[pain_miner] --dry-run set, not inserting. New posts:")
        for p in new[:15]:
            print(f"  - [{p.platform}] {p.post_title[:80]}  ({p.post_url})")
        return 0

    inserted = insert_posts(sb, new)
    print(f"[pain_miner] Inserted {inserted} rows into pain_posts.")
    return inserted


def _by_platform(posts: list[PainPost]) -> str:
    """Quick platform breakdown for the discover-run log line."""
    counts: dict[str, int] = {}
    for p in posts:
        counts[p.platform] = counts.get(p.platform, 0) + 1
    if not counts:
        return "none"
    return ", ".join(f"{n} {p}" for p, n in sorted(counts.items(), key=lambda kv: -kv[1]))


def run_archive() -> int:
    print(f"[pain_miner] {dt.datetime.now().isoformat()} — archive sweep")
    sb = supabase_client()
    n = archive_old(sb, days=4)
    print(f"[pain_miner] Archived {n} stale post(s) (>4 days old, status=new).")
    n_vendor = archive_stale_vendor_forum(sb)
    print(f"[pain_miner] Archived {n_vendor} stale vendor-forum post(s) "
          f"(>{_VENDOR_FORUM_MAX_AGE_DAYS} days old by posted_at, status=new).")
    return n + n_vendor


# ---------------------------------------------------------------------------
# Daily summary
# ---------------------------------------------------------------------------

def _html_escape(s: str | None) -> str:
    if s is None:
        return ""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;"))


_PLATFORM_LABEL = {
    "reddit": "Reddit", "hackernews": "Hacker News",
    "stackoverflow": "Stack Overflow", "x": "X", "bluesky": "Bluesky",
    "vendor-forum": "Vendor Forum (AWS/MS/GCP)", "other": "Other",
    "ade": "ADE (LandingAI mentions)",
}


def _fetch_posts_window(sb, hours: int) -> list[dict]:
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat()
    resp = (
        sb.table("pain_posts")
        .select("id, platform, post_url, post_title, summary, opportunity, "
                "categories, status, posted_at, discovered_at")
        .gte("discovered_at", cutoff)
        .order("discovered_at", desc=True)
        .execute()
    )
    return resp.data or []


def _build_daily_summary_html(posts: list[dict], window_label: str) -> tuple[str, str]:
    """Return (html, plaintext) — Brevo accepts both for client fallback."""
    total = len(posts)
    by_platform: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for p in posts:
        by_platform[p.get("platform") or "other"] = by_platform.get(p.get("platform") or "other", 0) + 1
        for cat in (p.get("categories") or []):
            by_category[cat] = by_category.get(cat, 0) + 1

    plat_rows = sorted(by_platform.items(), key=lambda kv: -kv[1])
    cat_rows  = sorted(by_category.items(), key=lambda kv: -kv[1])[:5]
    top_picks = posts[:5]

    plat_summary = ", ".join(f"{n} on {_PLATFORM_LABEL.get(p, p)}" for p, n in plat_rows) or "—"
    cat_summary  = (
        f"Main topic was <strong>{_html_escape(cat_rows[0][0])}</strong>"
        + (f", second most common was <strong>{_html_escape(cat_rows[1][0])}</strong>." if len(cat_rows) > 1 else ".")
        if cat_rows else "No categories tagged."
    )

    plat_lis = "".join(
        f"<li>{n} <span style='color:#5b5868'>on {_html_escape(_PLATFORM_LABEL.get(p, p))}</span></li>"
        for p, n in plat_rows
    )
    cat_lis = "".join(
        f"<li><strong>{_html_escape(c)}</strong> <span style='color:#5b5868'>· {n} post{'s' if n != 1 else ''}</span></li>"
        for c, n in cat_rows
    ) or "<li style='color:#5b5868'>None tagged.</li>"

    pick_html = "".join(
        f"""
        <li style="margin:0 0 14px;padding:0;list-style:none">
          <div style="font-size:11px;color:#a16207;text-transform:uppercase;letter-spacing:0.08em;font-weight:700;margin-bottom:4px">
            {_html_escape(_PLATFORM_LABEL.get(p.get('platform') or 'other', 'Other'))}
          </div>
          <div style="font-size:15px;font-weight:600;line-height:1.3;margin-bottom:4px">
            <a href="{_html_escape(p.get('post_url') or '#')}" style="color:#1a1626;text-decoration:none;border-bottom:1px dotted #5b5868">
              {_html_escape(p.get('post_title') or '(untitled)')}
            </a>
          </div>
          <div style="font-size:13px;color:#5b5868;line-height:1.5">{_html_escape(p.get('summary') or '')}</div>
        </li>
        """ for p in top_picks
    ) or "<li style='color:#5b5868;list-style:none'>No posts in this window.</li>"

    plat_text = ", ".join(f"{n} on {_PLATFORM_LABEL.get(p, p)}" for p, n in plat_rows) or "none"
    cat_text  = ", ".join(f"{c} ({n})" for c, n in cat_rows) or "none"
    pick_text = "\n\n".join(
        f"[{_PLATFORM_LABEL.get(p.get('platform') or 'other', 'Other')}] "
        f"{(p.get('post_title') or '').strip()}\n"
        f"{p.get('post_url') or ''}"
        for p in top_picks
    ) or "(no posts in this window)"

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f4efe6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1a1626">
  <div style="max-width:640px;margin:0 auto;padding:32px 20px">
    <div style="font-size:11px;color:#a16207;text-transform:uppercase;letter-spacing:0.12em;font-weight:700;margin-bottom:8px">
      LAI Daily Pain Miner Summary · {_html_escape(window_label)}
    </div>
    <h1 style="font-family:'Iowan Old Style','Palatino Linotype',Palatino,serif;font-size:36px;line-height:1.05;margin:0 0 18px;letter-spacing:-0.02em">
      Found {total} post{'s' if total != 1 else ''} today
    </h1>
    <p style="font-size:15px;color:#5b5868;line-height:1.6;margin:0 0 24px">
      {_html_escape(plat_summary)}.<br>{cat_summary}
    </p>

    <div style="background:rgba(255,251,245,0.95);border:1px solid rgba(49,38,23,0.08);border-radius:18px;padding:22px 24px;margin-bottom:18px">
      <div style="font-size:11px;color:#a16207;text-transform:uppercase;letter-spacing:0.1em;font-weight:700;margin-bottom:10px">By platform</div>
      <ul style="margin:0;padding:0 0 0 18px;font-size:14px;line-height:1.7;color:#1a1626">{plat_lis}</ul>
    </div>

    <div style="background:rgba(255,251,245,0.95);border:1px solid rgba(49,38,23,0.08);border-radius:18px;padding:22px 24px;margin-bottom:18px">
      <div style="font-size:11px;color:#a16207;text-transform:uppercase;letter-spacing:0.1em;font-weight:700;margin-bottom:10px">Top topics</div>
      <ul style="margin:0;padding:0 0 0 18px;font-size:14px;line-height:1.7;color:#1a1626">{cat_lis}</ul>
    </div>

    <div style="background:rgba(255,251,245,0.95);border:1px solid rgba(49,38,23,0.08);border-radius:18px;padding:22px 24px;margin-bottom:18px">
      <div style="font-size:11px;color:#a16207;text-transform:uppercase;letter-spacing:0.1em;font-weight:700;margin-bottom:14px">Standout posts</div>
      <ol style="margin:0;padding:0">{pick_html}</ol>
    </div>

    <p style="font-size:12px;color:#5b5868;line-height:1.6;margin:18px 0 0">
      Generated by <code>pain_miner.py --mode daily-summary</code>.
      Triage queue: <a href="http://{_DOMAIN}" style="color:#7c3aed">{_DOMAIN}</a>.
    </p>
  </div>
</body></html>"""

    text = (
        f"LAI Daily Pain Miner Summary · {window_label}\n\n"
        f"Found {total} post(s) today.\n"
        f"By platform: {plat_text}\n"
        f"Top topics: {cat_text}\n\n"
        f"Standout posts:\n{pick_text}\n\n"
        f"Triage queue: http://{_DOMAIN}\n"
    )
    return html, text


def run_daily_summary(window_hours: int = 24) -> int:
    print(f"[pain_miner] {dt.datetime.now().isoformat()} — daily summary (window={window_hours}h)")
    sb = supabase_client()
    posts = _fetch_posts_window(sb, hours=window_hours)
    today = dt.date.today().isoformat()
    window_label = f"last {window_hours}h · {today}"
    html, text = _build_daily_summary_html(posts, window_label)
    if not os.getenv("BREVO_API_KEY"):
        print("[pain_miner] BREVO_API_KEY missing — printing summary instead of sending.")
        print(text)
        return 0
    _brevo_send(_SUMMARY_TO, f"LAI Daily Pain Miner Summary — {today}", text=text, html=html)
    print(f"[pain_miner] Sent daily summary ({len(posts)} posts) to {_SUMMARY_TO}.")
    return len(posts)


# ---------------------------------------------------------------------------
# Frontend error digest — polls pain_miner_errors and emails new fingerprints.
# ---------------------------------------------------------------------------

_ERROR_DIGEST_LOCK = "/tmp/pain-miner-error-digest.lock"


def run_error_digest() -> int:
    print(f"[pain_miner] {dt.datetime.now().isoformat()} — error digest")

    # Hard-stop early if Brevo isn't configured. Skipping the run entirely is
    # safer than draining rows + marking them alerted with no email sent —
    # fixing the env later would otherwise miss every error in this window.
    if not os.getenv("BREVO_API_KEY"):
        print("[pain_miner] BREVO_API_KEY missing — skipping error-digest run "
              "(rows stay unalerted so they fire once the key is set).")
        return 0

    # Single-runner lock. The */15 cron can collide with itself if Brevo is
    # slow (30s timeout × N fingerprints) — without the lock the second run
    # would re-select the same alerted_at IS NULL rows and double-send.
    # O_NOFOLLOW: refuse to open the lock path if it's a symlink, so a
    # pre-planted symlink at this fixed /tmp path can't redirect the open
    # (and truncation) onto an arbitrary file this process can write.
    try:
        lock_fd = os.open(_ERROR_DIGEST_LOCK, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        print(f"[pain_miner] could not open lock file {_ERROR_DIGEST_LOCK}: {exc}", file=sys.stderr)
        return 0
    lock_fp = os.fdopen(lock_fd, "w")
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[pain_miner] another error-digest is running, skipping this cycle.")
        lock_fp.close()
        return 0

    try:
        sb = supabase_client()
        resp = (
            sb.table("pain_miner_errors")
            .select("id, occurred_at, message, stack, page_url, user_agent, fingerprint")
            .is_("alerted_at", "null")
            .order("occurred_at", desc=False)
            .limit(500)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            print("[pain_miner] No new frontend errors.")
            return 0

        # Group by fingerprint so a runaway error sends ONE email, not 20.
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(r["fingerprint"], []).append(r)

        sent = 0
        for fingerprint, occurrences in groups.items():
            first = occurrences[0]
            count = len(occurrences)
            msg   = first.get("message") or "(no message)"
            stack = first.get("stack") or "(no stack)"
            url   = first.get("page_url") or "(no url)"
            ua    = first.get("user_agent") or "(no user agent)"
            when  = first.get("occurred_at") or "?"
            subject = f"Pain Miner frontend error — {msg[:80]}"
            body = (
                f"A frontend error fired in the Pain Miner UI.\n\n"
                f"Fingerprint: {fingerprint}\n"
                f"Occurrences in this digest: {count}\n"
                f"First seen: {when}\n"
                f"Page: {url}\n"
                f"User agent: {ua}\n\n"
                f"Message:\n{msg}\n\n"
                f"Stack:\n{stack}\n"
            )
            try:
                _brevo_send(_ALERT_TO, subject, text=body)
                sent += 1
            except Exception as exc:
                # Don't crash the digest if a single email fails — keep going,
                # and DON'T mark the rows alerted so the next run retries them.
                print(f"[warn] Brevo send failed for fingerprint {fingerprint}: {exc}", file=sys.stderr)
                continue
            # Mark this fingerprint's rows alerted only after a successful send.
            ids = [r["id"] for r in occurrences]
            sb.table("pain_miner_errors").update(
                {"alerted_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            ).in_("id", ids).execute()

        print(f"[pain_miner] Frontend error digest: {len(groups)} fingerprint(s), {len(rows)} row(s), {sent} email(s) sent.")
        return sent
    finally:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
        lock_fp.close()


# ---------------------------------------------------------------------------
# 7-day health check — produces an emailed report for ops triage.
# Designed to be triggered by a one-shot cron entry. Uses only local
# resources: Supabase REST, the local /var/log/pain-miner.log, and a
# public HTTP probe of _DOMAIN.
# ---------------------------------------------------------------------------

_HEALTH_LOG_PATH = "/var/log/pain-miner.log"


def run_health_check() -> int:
    print(f"[pain_miner] {dt.datetime.now().isoformat()} — 7-day health check")
    sb = supabase_client()
    findings: list[str] = []
    severity = "HEALTHY"

    def downgrade(level: str) -> None:
        nonlocal severity
        order = {"HEALTHY": 0, "NEEDS_ATTENTION": 1, "BROKEN": 2}
        if order[level] > order[severity]:
            severity = level

    seven_days_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat()

    # ---- a/b/c. pain_posts stats from Supabase ----
    try:
        resp = (
            sb.table("pain_posts")
            .select("discovered_at, platform, competitors")
            .gte("discovered_at", seven_days_ago)
            .limit(20000)
            .execute()
        )
        posts = resp.data or []
    except Exception as exc:
        findings.append(f"BROKEN: Supabase pain_posts query failed: {exc}")
        downgrade("BROKEN")
        posts = []

    by_day: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    competitor_count = 0
    for p in posts:
        day = (p.get("discovered_at") or "")[:10]
        if day:
            by_day[day] = by_day.get(day, 0) + 1
        plat = p.get("platform") or "?"
        by_platform[plat] = by_platform.get(plat, 0) + 1
        if p.get("competitors"):
            competitor_count += 1

    # Check (a) — zero-volume days indicate cron failure. Discovery only runs
    # Mon-Sat (see deploy/crontab.example) — skip Sundays or this alarms weekly.
    today = dt.datetime.now(dt.timezone.utc).date()
    expected_days = [
        (today - dt.timedelta(days=i)).isoformat()
        for i in range(1, 8)
        if (today - dt.timedelta(days=i)).weekday() != 6  # Sunday
    ]
    zero_days = [d for d in expected_days if by_day.get(d, 0) == 0]
    if zero_days:
        findings.append(f"NEEDS_ATTENTION: zero-volume days: {', '.join(zero_days)} — discovery cron may be broken")
        downgrade("NEEDS_ATTENTION")
    runaway = [(d, n) for d, n in by_day.items() if n > 100]
    if runaway:
        findings.append(f"NEEDS_ATTENTION: runaway days (>100 posts): {runaway}")
        downgrade("NEEDS_ATTENTION")

    # Check (b) — silent platforms. Bluesky and vendor-forum are deliberately
    # excluded here — both are lower/sporadic-volume sources where a quiet
    # week is expected and normal, not a sign of a broken pipeline.
    expected_platforms = ["reddit", "hackernews", "stackoverflow", "x"]
    silent_platforms = [p for p in expected_platforms if by_platform.get(p, 0) == 0]
    if silent_platforms:
        findings.append(
            f"NEEDS_ATTENTION: source(s) silent for 7 days: {', '.join(silent_platforms)} — check the relevant pipeline source"
        )
        downgrade("NEEDS_ATTENTION")

    # Check (c) — competitor scan health
    if competitor_count == 0:
        findings.append("NEEDS_ATTENTION: zero competitor mentions in 7 days — competitor scan may be broken")
        downgrade("NEEDS_ATTENTION")

    # ---- d. pain_miner_errors volume ----
    try:
        err_resp = (
            sb.table("pain_miner_errors")
            .select("id")
            .gte("occurred_at", seven_days_ago)
            .limit(20000)
            .execute()
        )
        err_count = len(err_resp.data or [])
    except Exception as exc:
        findings.append(f"BROKEN: pain_miner_errors query failed: {exc}")
        downgrade("BROKEN")
        err_count = -1
    if err_count > 50:
        findings.append(f"NEEDS_ATTENTION: {err_count} frontend errors in 7 days (>50 threshold)")
        downgrade("NEEDS_ATTENTION")

    # ---- e. log scan ----
    log_hits: list[str] = []
    try:
        cutoff_dt = dt.datetime.now() - dt.timedelta(days=7)
        with open(_HEALTH_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "ERROR" in line or "BROKEN" in line:
                    # Best-effort timestamp filter: log lines start with ISO date "YYYY-MM-DD".
                    m = re.match(r"^.*?(\d{4}-\d{2}-\d{2})", line)
                    keep = True
                    if m:
                        try:
                            line_date = dt.datetime.strptime(m.group(1), "%Y-%m-%d")
                            keep = line_date >= cutoff_dt - dt.timedelta(days=1)
                        except ValueError:
                            pass
                    if keep:
                        log_hits.append(line.strip())
        if log_hits:
            findings.append(f"NEEDS_ATTENTION: {len(log_hits)} ERROR/BROKEN line(s) in {_HEALTH_LOG_PATH}")
            downgrade("NEEDS_ATTENTION")
    except OSError as exc:
        findings.append(f"NEEDS_ATTENTION: could not read {_HEALTH_LOG_PATH}: {exc}")
        downgrade("NEEDS_ATTENTION")

    # ---- f. frontend reachable check ----
    # Only /config.js is nginx basic-auth-gated (it holds Supabase keys + the
    # RPC secret); the root page is intentionally public behind its own
    # client-side login.html gate. Check /config.js, not /, for the 401.
    frontend_status = "?"
    try:
        req = urllib.request.Request(
            f"https://{_DOMAIN}/config.js",
            headers={"User-Agent": "lai-painminer-health-check/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15, context=https_context()) as resp:
            frontend_status = str(resp.status)
        # 200 on config.js would mean basic auth got removed — bad
        findings.append(f"BROKEN: {_DOMAIN}/config.js returned {frontend_status} (expected 401)")
        downgrade("BROKEN")
    except urllib.error.HTTPError as exc:
        frontend_status = str(exc.code)
        if exc.code == 401:
            pass  # expected — basic auth challenge
        else:
            findings.append(f"BROKEN: {_DOMAIN}/config.js returned HTTP {exc.code} (expected 401)")
            downgrade("BROKEN")
    except Exception as exc:
        findings.append(f"BROKEN: {_DOMAIN} unreachable: {exc}")
        downgrade("BROKEN")
        frontend_status = "unreachable"

    # ---- Build report ----
    by_day_lines = "\n".join(f"  {d}: {n} posts" for d, n in sorted(by_day.items())) or "  (no data)"
    plat_lines = (
        "\n".join(f"  {p}: {n}" for p, n in sorted(by_platform.items(), key=lambda kv: -kv[1]))
        or "  (no data)"
    )
    findings_block = "\n".join(f"  - {f}" for f in findings) or "  - All checks pass."
    log_block = "\n".join(f"    {ln}" for ln in log_hits[-15:]) if log_hits else "    (none)"

    body = (
        "Pain Miner — 7-day health check\n"
        f"{dt.datetime.now(dt.timezone.utc).isoformat()}\n\n"
        f"VERDICT: {severity}\n\n"
        "7-day stats:\n"
        f"  Total posts:           {len(posts)}\n"
        f"  Competitor mentions:   {competitor_count}\n"
        f"  Frontend errors:       {err_count}\n"
        f"  Frontend HTTP status:  {frontend_status} (expected 401)\n\n"
        "Posts per day:\n"
        f"{by_day_lines}\n\n"
        "Posts per platform:\n"
        f"{plat_lines}\n\n"
        "Findings:\n"
        f"{findings_block}\n\n"
        "Recent ERROR/BROKEN log lines (last 15 if any):\n"
        f"{log_block}\n\n"
        "— generated by `pain_miner.py --mode health-check`\n"
    )
    print(body)
    if os.getenv("BREVO_API_KEY"):
        _brevo_send(_ALERT_TO, f"Pain Miner — 7-day health check ({severity})", text=body)
        print(f"[pain_miner] Health check report emailed to {_ALERT_TO}.")
    else:
        print("[pain_miner] BREVO_API_KEY missing — report printed only.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Pain Miner — engagement triage queue.")
    parser.add_argument(
        "--mode",
        choices=["discover", "archive", "daily-summary", "error-digest", "health-check"],
        default="discover",
    )
    parser.add_argument("--dry-run", action="store_true", help="Discover only — print, do not insert.")
    args = parser.parse_args()

    # Pipeline observability — best-effort. Pain Miner already has its own
    # frontend-error pipeline (pain_miner_errors); this layer is for backend
    # cron-script errors so the runs.muisbien.com dashboard sees them too.
    from _pipeline_log import start_run, end_run, fail_run  # type: ignore
    run_id = start_run("PM", "pain_miner.py", mode=args.mode)

    try:
        if args.mode == "discover":
            run_discover(dry_run=args.dry_run)
        elif args.mode == "archive":
            run_archive()
        elif args.mode == "daily-summary":
            run_daily_summary()
        elif args.mode == "error-digest":
            run_error_digest()
        elif args.mode == "health-check":
            run_health_check()
        end_run(run_id, status="success")
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        print(f"[pain_miner] ERROR: {detail}", file=sys.stderr)
        send_error_alert(
            subject=f"Pain Miner BROKEN — {dt.date.today().isoformat()}",
            detail=detail,
        )
        fail_run(run_id, "PM", "pain_miner.py", args.mode, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
