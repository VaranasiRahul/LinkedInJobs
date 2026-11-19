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
import re
import tempfile
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Set

import constants

logger = logging.getLogger(__name__)

_STORE_PATH = constants.SEEN_JOBS_FILE
_TTL_HOURS  = constants.SEEN_JOBS_TTL_HOURS


# ── Internal helpers ───────────────────────────────────────────────────────────

def _fingerprint(title: str, company: str) -> str:
    """Create a normalised title+company fingerprint for content-based dedup.

    This catches the same job reposted under a different LinkedIn URL/ID.
    Example: "DevOps Engineer | Wipro" today has ID 123456, tomorrow it's
    reposted as ID 789012 — the fingerprint "devops engineer|wipro" matches.

    Requires BOTH title and company to be non-empty so we never falsely match
    different companies posting similarly titled roles.
    """
    if not title or not company:
        return ''
    t = title.lower()
    t = re.sub(r'[^a-z0-9\s]', ' ', t)
    # Remove insignificant stopwords and collapse whitespace
    stopwords = {"and", "for", "the", "at", "in", "of", "with", "on", "a", "an", "to"}
    t_words = [w for w in t.split() if w not in stopwords]
    t_norm = " ".join(t_words)
    c = re.sub(r'[^a-z0-9]', '', company.lower())
    if not t_norm or not c:
        return ''
    return f"{t_norm}|{c}"

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

def filter_new(job_ids: List[str], jobs: Optional[List[dict]] = None) -> List[str]:
    """
    Given a list of job IDs, return only the ones NOT seen before.

    Two-layer deduplication:
      1. Job ID match — exact numeric LinkedIn job ID
      2. Fingerprint match — normalised 'title|company' catches the same
         job reposted under a different URL/ID (LinkedIn re-lists jobs)

    Args:
        job_ids: List of extracted job IDs to check.
        jobs:    Optional list of job dicts (same order as job_ids).
                 If provided, enables fingerprint dedup.

    This is the primary deduplication function — called once per run.
    Loads the store ONCE (efficient — no repeated disk reads).
    Does NOT write anything; call mark_seen_batch() after notifying.
    """
    store = _load()

    # Build set of known fingerprints from the store for O(1) lookup
    known_fps: Set[str] = set()
    for jid, val in store.items():
        # Store format is either a plain ISO string (old) or a dict with 'ts' + 'fp'
        if isinstance(val, dict):
            fp = val.get("fp", "")
            if fp:
                known_fps.add(fp)

    new_ids = []
    for i, jid in enumerate(job_ids):
        # Layer 1: exact job ID match
        if jid in store:
            continue
        # Layer 2: fingerprint match (if job dicts provided)
        if jobs and i < len(jobs):
            job = jobs[i]
            fp = _fingerprint(job.get("title", ""), job.get("company", ""))
            if fp and fp in known_fps:
                logger.info(
                    "Fingerprint dedup: job %s (%s @ %s) matches a seen job.",
                    jid, job.get("title", "")[:40], job.get("company", "")[:20],
                )
                continue
        new_ids.append(jid)

    logger.info(
        "Dedup check: %d total → %d already seen → %d new",
        len(job_ids),
        len(job_ids) - len(new_ids),
        len(new_ids),
    )
    return new_ids


def is_new(job_id: str, job: Optional[dict] = None) -> bool:
    """Return True if this job has NOT been seen before (by ID or fingerprint)."""
    store = _load()
    if job_id in store:
        return False
    if job:
        fp = _fingerprint(job.get("title", ""), job.get("company", ""))
        if fp:
            for val in store.values():
                if isinstance(val, dict) and val.get("fp") == fp:
                    return False
    return True


def mark_seen_batch(job_ids: List[str], jobs: Optional[List[dict]] = None) -> None:
    """
    Atomically mark multiple job IDs as seen in a single disk write.

    If job dicts are provided (same order as job_ids), also stores a
    title+company fingerprint for content-based dedup on future runs.

    Storage format per entry (new):
        { "ts": "2026-10-21T01:30:00+00:00", "fp": "devops engineer|wipro" }
    Preserves original first-seen timestamp and fingerprint for existing entries.
    """
    if not job_ids:
        return
    store = _load()
    now_iso = datetime.now(timezone.utc).isoformat()
    for i, jid in enumerate(job_ids):
        fp = ""
        if jobs and i < len(jobs):
            job = jobs[i]
            fp = _fingerprint(job.get("title", ""), job.get("company", ""))

        existing = store.get(jid)
        existing_ts = None
        existing_fp = ""
        if isinstance(existing, dict):
            existing_ts = existing.get("ts")
            existing_fp = existing.get("fp", "")
        elif isinstance(existing, str):
            existing_ts = existing

        store[jid] = {
            "ts": existing_ts or now_iso,
            "fp": fp or existing_fp,
        }
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
    cutoff   = datetime.now(timezone.utc) - timedelta(hours=_TTL_HOURS)
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


def _is_expired(ts_or_entry, cutoff: datetime) -> bool:
    """Return True if timestamp is before the cutoff (or malformed).

    Handles both formats:
      - Old: plain ISO string (e.g., "2026-10-21T01:30:00+00:00")
      - New: dict with {"ts": "...", "fp": "..."}
    """
    try:
        if isinstance(ts_or_entry, dict):
            ts_str = ts_or_entry.get("ts", "")
        else:
            ts_str = str(ts_or_entry)
        if not ts_str:
            return True
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts < cutoff
    except (ValueError, TypeError):
        return True   # malformed entry → purge it
