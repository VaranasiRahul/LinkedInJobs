#! python3
# -*- coding: utf-8 -*-
"""
notifier.py — Discord notification helper for LinkedIn Jobs Monitor.

One embed per job, one message per job.
Each message can be reacted to in Discord (✅ = applied, ❌ = not interested).

Embed format:
  ┌─ LinkedIn Blue ─────────────────────────────────────────────┐
  │  💼  Senior DevOps Engineer                   (clickable)   │
  │  ★ PRIORITY COMPANY                           (if MNC)      │
  │                                                             │
  │  Wipro                                                      │
  │  Chennai, Tamil Nadu, India                                 │
  │  19 hours ago                                               │
  │  3+ years experience required                               │
  └─────────────────────────────────────────────────────────────┘
"""

import logging
import subprocess
import time
from typing import List

import requests

import constants

logger = logging.getLogger(__name__)

def _get_webhook_url() -> str:
    import os
    return os.environ.get("DISCORD_WEBHOOK_URL") or getattr(constants, "DISCORD_WEBHOOK_URL", "")

_COLOR_LINKEDIN = 0x0A66C2   # LinkedIn brand blue
_COLOR_PRIORITY = 0x2ECC71   # Green for priority/MNC companies
_COLOR_WARN     = 0xE74C3C   # Red for errors

_MSG_DELAY = 0.9   # seconds between messages (Discord rate limit safety)



# ── Public API ─────────────────────────────────────────────────────────────────

def send_new_jobs(jobs: List[dict]) -> None:
    """
    Send one Discord embed message per job.
    No cap — every job gets its own message.
    Priority company jobs use a green colour.
    """
    if not jobs:
        return

    count = len(jobs)

    # Brief header when multiple jobs arrive at once
    if count > 1:
        _post({"content": f"**{count} New DevOps Jobs Found**"})
        time.sleep(_MSG_DELAY)

    for job in jobs:
        _send_job_embed(job)
        time.sleep(_MSG_DELAY)


def send_status(text: str, warn: bool = False) -> bool:
    """Send a plain status or warning message."""
    return _post({
        "embeds": [{
            "description": text,
            "color": _COLOR_WARN if warn else _COLOR_LINKEDIN,
        }]
    })


# ── Internal ───────────────────────────────────────────────────────────────────

def _send_job_embed(job: dict) -> bool:
    """Build and POST a single Discord embed for one job."""
    title        = job.get("title", "Unknown Title")
    company      = job.get("company", "")
    location     = job.get("location", "")
    posted_time  = job.get("posted_time", "")
    url          = job.get("url", "")
    yoe          = job.get("yoe_found")          # int or None
    is_priority  = job.get("is_priority", False) # True if company is in PRIORITY_COMPANIES
    is_duplicate = job.get("is_duplicate", False)

    # Embed title — only 💼 emoji, nothing else
    embed_title = f"💼  {title}"
    if is_duplicate:
        embed_title += "  [POSSIBLE DUPLICATE]"

    # Description — clean text lines, no emojis
    lines = []
    if is_priority:
        lines.append("★  PRIORITY COMPANY")
        lines.append("")
    if company:
        lines.append(company)
    if location:
        lines.append(location)
    if posted_time:
        lines.append(posted_time)
    if yoe is not None:
        lines.append(f"{yoe}+ years experience required")
    else:
        lines.append("Experience requirements — see description")

    desc = "\n".join(lines)

    embed: dict = {
        "title":       embed_title,
        "description": desc,
        "color":       _COLOR_PRIORITY if is_priority else _COLOR_LINKEDIN,
    }
    if url:
        embed["url"] = url   # makes entire title clickable

    return _post({"embeds": [embed]})


def _post(payload: dict) -> bool:
    """POST payload to Discord webhook. Returns True on success."""
    url = _get_webhook_url()
    if not url:
        logger.error("Discord send failed: DISCORD_WEBHOOK_URL is not configured.")
        return False
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        logger.info("Discord message sent (HTTP %d).", resp.status_code)
        return True
    except requests.RequestException as e:
        logger.error("Discord send failed: %s", e)
        return False


# ── macOS notifications (optional) ────────────────────────────────────────────

def notify_macos(title: str, message: str) -> None:
    if not constants.ENABLE_MACOS_NOTIFICATION:
        return
    try:
        t = title.replace('"', '\\"')
        m = message.replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{m}" with title "{t}" sound name "Glass"'],
            check=True, capture_output=True,
        )
    except Exception as e:
        logger.warning("macOS notification failed: %s", e)
