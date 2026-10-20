#! python3
# -*- coding: utf-8 -*-
"""
job_store.py — Persistent seen-jobs tracker.

Answers two questions every run:
  1. "Have I already notified about this job?" → is_new()
  2. "Which of these are new to me?"           → filter_new()

Storage:
  data/seen_jobs.json  →  { "job_id": "2026-10-21T01:30:00" (UTC first-seen time) }

TTL logic:
  - LinkedIn "last 24 hours" filter ensures we only scrape recent jobs.
  - We remember job IDs for SEEN_JOBS_TTL_HOURS (default 48h).
  - After 48 hours a job will never appear in the 24h search anyway,
    so it's safe to forget it — the store stays lean automatically.
  - Cleanup runs at the END of every run (after notifications are sent).

Write safety:
  - All writes are atomic: write to a temp file, then os.replace().
  - A crash mid-write can NEVER corrupt the store file.
  - If the store is missing or corrupt, we start fresh (worst case:
    one duplicate notification batch — then back to normal).
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from typing import List

import constants

logger = logging.getLogger(__name__)

_STORE_PATH = constants.SEEN_JOBS_FILE
_TTL_HOURS  = constants.SEEN_JOBS_TTL_HOURS


# ── Internal helpers ───────────────────────────────────────────────────────────

def _load() -> dict:
    """
    Load the seen-jobs dict from disk.
    Returns {} if file is missing (first run) or corrupt (safe recovery).
    """
    if not os.path.exists(_STORE_PATH):
        return {}
    try:
        with open(_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Validate it's a dict (guards against accidental truncation)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("seen_jobs.json unreadable (%s) — starting fresh.", e)
        return {}


def _save(store: dict) -> None:
    """
    Atomically write the store dict to disk using a temp-file + os.replace.
    A crash or power loss mid-write will NEVER leave a corrupt file.
    """
    os.makedirs(os.path.dirname(os.path.abspath(_STORE_PATH)), exist_ok=True)
    dir_path = os.path.dirname(os.path.abspath(_STORE_PATH))
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2)
        os.replace(tmp_path, _STORE_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── Public API ─────────────────────────────────────────────────────────────────

def filter_new(job_ids: List[str]) -> List[str]:
    """
    Given a list of job IDs, return only the ones NOT seen before.

    This is the primary deduplication function — called once per run.
    Loads the store ONCE (efficient — no repeated disk reads).
    Does NOT write anything; call mark_seen_batch() after notifying.

    How "no miss" is guaranteed:
      - LinkedIn URL uses f_TPR=r86400 → fetches ALL jobs from last 24h every run.
      - So a job posted 5 mins ago is fetched on the NEXT 30-min tick.
      - filter_new() returns it as new because it wasn't seen before.
      - After notification, it's marked seen → never notified again.
    """
    store = _load()
    new_ids = [jid for jid in job_ids if jid not in store]
    logger.info(
        "Dedup check: %d total → %d already seen → %d new",
        len(job_ids),
        len(job_ids) - len(new_ids),
        len(new_ids),
    )
    return new_ids


def is_new(job_id: str) -> bool:
    """Return True if this job_id has NOT been seen before (single-check convenience)."""
    return job_id not in _load()


def mark_seen_batch(job_ids: List[str]) -> None:
    """
    Atomically mark multiple job IDs as seen in a single disk write.
    Call this AFTER sending all notifications so a crash mid-notify
    doesn't mark jobs as seen before the Discord message went out.
    """
    if not job_ids:
        return
    store = _load()
    now_iso = datetime.utcnow().isoformat()
    for jid in job_ids:
        store[jid] = now_iso
    _save(store)
    logger.info("Marked %d job ID(s) as seen.", len(job_ids))


def cleanup() -> int:
    """
    Remove job IDs older than SEEN_JOBS_TTL_HOURS from the store.

    Why 48h TTL is the right value:
      - LinkedIn search window = 24 hours
      - Our TTL = 48 hours (2× the search window)
      - After 48h, a job CANNOT appear in the 24h search → safe to forget
      - This keeps seen_jobs.json lean and prevents indefinite growth

    Returns the number of entries removed.
    """
    store    = _load()
    cutoff   = datetime.utcnow() - timedelta(hours=_TTL_HOURS)
    n_before = len(store)

    expired = [
        jid for jid, ts_str in store.items()
        if _is_expired(ts_str, cutoff)
    ]
    for jid in expired:
        del store[jid]

    if expired:
        _save(store)
        logger.info(
            "Purged %d expired job ID(s) (older than %dh). Store: %d remaining.",
            len(expired), _TTL_HOURS, len(store),
        )

    return len(expired)


def count() -> int:
    """Return total number of job IDs currently in the store."""
    return len(_load())


def _is_expired(ts_str: str, cutoff: datetime) -> bool:
    """Return True if timestamp string is before the cutoff (or malformed)."""
    try:
        return datetime.fromisoformat(ts_str) < cutoff
    except (ValueError, TypeError):
        return True   # malformed entry → purge it
