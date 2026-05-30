"""Shared pipeline-observability helper for cron-driven scripts.

Centralizes:
  - run-level lifecycle (start_run / end_run)
  - step-level progress within a run (update_step)
  - error logging (log_error) — also fires a Brevo breaking-error alert

Tables: pipeline_runs, pipeline_errors. See schema/pipeline_observability.sql.

Design: every public function is BEST-EFFORT. A Supabase outage or schema
mismatch must NEVER break the actual cron job. All Supabase calls are
wrapped in try/except; failures print a one-line warning and return None.

Products use these short codes everywhere:
  RA = Research Agent (Project 1)
  CI = Conference Intel (Project 3, both historical + forward)
  PM = Pain Miner (Project 2 cron-script errors only)

Wiring pattern in a script's main():

    from _pipeline_log import start_run, update_step, end_run, log_error

    run_id = start_run("RA", "weekly_accounts_agent.py", mode=None)
    try:
        update_step(run_id, "grok_discovery", "running", sequence=1)
        # ... do work, count results ...
        update_step(run_id, "grok_discovery", "success", count=len(grok_accounts), sequence=1)

        update_step(run_id, "claude_synthesis", "running", sequence=2)
        # ... synthesize ...
        update_step(run_id, "claude_synthesis", "success", sequence=2)

        end_run(run_id, status="success", stats={"contacts_found": N, "in_apollo": M})
    except Exception as exc:
        import traceback as _tb
        log_error("RA", "weekly_accounts_agent.py", None,
                  message=str(exc), traceback=_tb.format_exc(), run_id=run_id)
        end_run(run_id, status="failed")
        raise
"""

from __future__ import annotations

import datetime as _dt
import json as _json
import os as _os
import pathlib as _pathlib
import ssl as _ssl
import sys as _sys
import urllib.error as _ue
import urllib.parse as _up
import urllib.request as _ur
from typing import Any, Optional

try:
    import certifi as _certifi
except ImportError:
    _certifi = None


# ── Config (read at import; load_dotenv typically already ran upstream) ──────

_ROOT = _pathlib.Path(__file__).resolve().parent.parent
_DOTENV = _ROOT / ".env"


def _ensure_env_loaded() -> None:
    """If the caller's main() didn't call load_dotenv yet, load it ourselves."""
    if "SUPABASE_URL" in _os.environ:
        return
    if not _DOTENV.exists():
        return
    for raw in _DOTENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in _os.environ:
            _os.environ[k] = v


_ensure_env_loaded()


def _ssl_ctx() -> _ssl.SSLContext:
    if _certifi is not None:
        return _ssl.create_default_context(cafile=_certifi.where())
    return _ssl.create_default_context()


def _supabase_url() -> str:
    return _os.environ.get("SUPABASE_URL", "").rstrip("/")


def _supabase_key() -> str:
    return _os.environ.get("SUPABASE_KEY", "")


# ── Low-level Supabase REST helpers ──────────────────────────────────────────

def _sb_request(
    method: str,
    path: str,
    body: Optional[dict | list] = None,
    *,
    return_representation: bool = False,
) -> Optional[Any]:
    """Single-shot Supabase REST call. Best-effort: returns None on any error."""
    url = _supabase_url()
    key = _supabase_key()
    if not url or not key:
        return None
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if return_representation:
        headers["Prefer"] = "return=representation"
    data = _json.dumps(body).encode("utf-8") if body is not None else None
    req = _ur.Request(f"{url}{path}", data=data, headers=headers, method=method)
    try:
        with _ur.urlopen(req, timeout=10, context=_ssl_ctx()) as resp:
            raw = resp.read().decode("utf-8")
            return _json.loads(raw) if raw else None
    except _ue.HTTPError as err:
        detail = ""
        try:
            detail = err.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        print(f"  [pipeline-log] Supabase {method} {path} → HTTP {err.code}: {detail}", file=_sys.stderr)
        return None
    except Exception as exc:
        print(f"  [pipeline-log] Supabase {method} {path} failed: {exc}", file=_sys.stderr)
        return None


# ── Public API ───────────────────────────────────────────────────────────────

def start_run(product: str, script: str, mode: Optional[str] = None) -> Optional[str]:
    """Insert a pipeline_runs row with status='running' and return its id (uuid string).

    Returns None if Supabase is unreachable — callers should treat run_id=None as
    "observability disabled for this run" and continue with the actual work.
    """
    payload = [{
        "product": product,
        "script":  script,
        "mode":    mode,
        "status":  "running",
        # started_at / steps / stats use DB defaults
    }]
    rows = _sb_request("POST", "/rest/v1/pipeline_runs", body=payload, return_representation=True)
    if not rows or not isinstance(rows, list):
        return None
    return rows[0].get("id")


def update_step(
    run_id: Optional[str],
    name: str,
    status: str,                       # 'running' | 'success' | 'failed' | 'skipped'
    *,
    count: Optional[int] = None,
    note: Optional[str] = None,
    sequence: Optional[int] = None,
) -> None:
    """Append or update a step entry in the run's steps[] JSONB array.

    Idempotent on (run_id, name): a second call with the same name updates the
    existing step in place (e.g. 'running' → 'success'). The sequence field
    establishes ordering when first creating the step.
    """
    if not run_id:
        return

    # Read current steps array, mutate, write back. Two round-trips per call —
    # acceptable since steps are coarse-grained (3-7 per run).
    rows = _sb_request(
        "GET",
        f"/rest/v1/pipeline_runs?id=eq.{_up.quote(run_id)}&select=steps",
    )
    if not isinstance(rows, list) or not rows:
        return
    steps = rows[0].get("steps") or []
    if not isinstance(steps, list):
        steps = []

    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()

    # Find existing entry by name; otherwise append.
    idx = next((i for i, s in enumerate(steps) if isinstance(s, dict) and s.get("name") == name), -1)
    if idx == -1:
        steps.append({
            "name":       name,
            "started_at": now_iso,
            "ended_at":   None if status == "running" else now_iso,
            "status":     status,
            "count":      count,
            "note":       note,
            "sequence":   sequence if sequence is not None else len(steps) + 1,
        })
    else:
        existing = steps[idx]
        existing["status"] = status
        if status != "running":
            existing["ended_at"] = now_iso
        if count is not None:
            existing["count"] = count
        if note is not None:
            existing["note"] = note

    _sb_request(
        "PATCH",
        f"/rest/v1/pipeline_runs?id=eq.{_up.quote(run_id)}",
        body={"steps": steps},
    )


def end_run(
    run_id: Optional[str],
    status: str = "success",           # 'success' | 'failed' | 'partial'
    stats: Optional[dict] = None,
    error_id: Optional[str] = None,
) -> None:
    """Finalize a run: set ended_at, status, stats, optional error_id."""
    if not run_id:
        return
    payload: dict = {
        "status":   status,
        "ended_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    if stats is not None:
        payload["stats"] = stats
    if error_id is not None:
        payload["error_id"] = error_id
    _sb_request(
        "PATCH",
        f"/rest/v1/pipeline_runs?id=eq.{_up.quote(run_id)}",
        body=payload,
    )


def log_error(
    product: str,
    script: str,
    mode: Optional[str],
    message: str,
    traceback: Optional[str] = None,
    run_id: Optional[str] = None,
    *,
    send_email: bool = True,
) -> Optional[str]:
    """Insert into pipeline_errors and (optionally) fire a breaking-error Brevo alert.

    Returns the inserted error id (uuid string) or None on insert failure.
    Email is best-effort — never raises.
    """
    payload = [{
        "product":  product,
        "script":   script,
        "mode":     mode,
        "message":  (message or "")[:500],
        "traceback": traceback,
        "run_id":   run_id,
    }]
    rows = _sb_request(
        "POST",
        "/rest/v1/pipeline_errors",
        body=payload,
        return_representation=True,
    )
    err_id = None
    if isinstance(rows, list) and rows:
        err_id = rows[0].get("id")

    if send_email:
        _send_breaking_error_alert(product, script, mode, message, traceback, err_id, run_id)

    return err_id


# ── Brevo breaking-error alert ───────────────────────────────────────────────

_PRODUCT_NAMES = {"RA": "Research Agent", "CI": "Conference Intel", "HR": "Honoree Reachout", "PM": "Pain Miner", "WW": "WeedWatcher", "SEC": "Security Hook", "DI": "Date Intelligence", "MF": "MainForge Sync", "MFW": "MainForge Watch", "BG": "Book Group Finder"}
_ALERT_TO = "tylerdnorkus@gmail.com"


def _send_breaking_error_alert(
    product: str,
    script: str,
    mode: Optional[str],
    message: str,
    traceback: Optional[str],
    error_id: Optional[str],
    run_id: Optional[str],
) -> None:
    """Per Tyler 2026-04-27: only email on actual breaking errors. Successful
    runs are silent — observability happens on runs.muisbien.com instead."""
    api_key = _os.environ.get("BREVO_API_KEY", "")
    if not api_key:
        print(f"  [error-alert] BREVO_API_KEY unset — alert not sent for {product}/{script}", file=_sys.stderr)
        return

    pname = _PRODUCT_NAMES.get(product, product)
    suffix = f" {mode}" if mode else ""
    subject = f"❌ {pname} BROKEN — {script}{suffix} — {_dt.date.today().isoformat()}"

    body_lines = [
        f"Product:  {pname} ({product})",
        f"Script:   {script}",
        f"Mode:     {mode or '—'}",
        f"Time:     {_dt.datetime.now().isoformat()}",
        f"Run ID:   {run_id or '—'}",
        f"Error ID: {error_id or '—'}",
        "",
        "Error:",
        message or "(no message)",
    ]
    if traceback:
        body_lines += ["", "─" * 60, "Traceback:", traceback]
    body_lines += [
        "",
        "─" * 60,
        "Triage at runs.muisbien.com (Now tab → Errors).",
        "Mark resolved once fixed so it stops appearing in the unresolved list.",
    ]

    from_raw = _os.environ.get("EMAIL_FROM", "LandingAI Research <research@mg.muisbien.com>")
    if "<" in from_raw and ">" in from_raw:
        sender_name  = from_raw.split("<")[0].strip().strip('"')
        sender_email = from_raw.split("<")[1].rstrip(">").strip()
    else:
        sender_name, sender_email = "LandingAI Research", from_raw.strip()

    payload = {
        "sender":      {"email": sender_email, "name": sender_name},
        "to":          [{"email": _ALERT_TO}],
        "subject":     subject,
        "textContent": "\n".join(body_lines),
    }
    req = _ur.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=_json.dumps(payload).encode("utf-8"),
        headers={"api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _ur.urlopen(req, timeout=15, context=_ssl_ctx()) as resp:
            resp.read()
    except Exception as exc:
        print(f"  [error-alert] Brevo send failed (non-fatal): {exc}", file=_sys.stderr)


# ── Convenience: catch-all wrapper ────────────────────────────────────────────

def fail_run(
    run_id: Optional[str],
    product: str,
    script: str,
    mode: Optional[str],
    exc: BaseException,
) -> None:
    """Convenience for the unhandled-exception path.

    Call from a top-level catch-all to: log the error, link it to the run,
    flip the run to status='failed', and fire the Brevo alert. After this
    call, the script is free to re-raise or return non-zero.
    """
    import traceback as _tb
    detail = _tb.format_exception(type(exc), exc, exc.__traceback__)
    err_id = log_error(
        product=product,
        script=script,
        mode=mode,
        message=str(exc) or exc.__class__.__name__,
        traceback="".join(detail),
        run_id=run_id,
    )
    end_run(run_id, status="failed", error_id=err_id)
