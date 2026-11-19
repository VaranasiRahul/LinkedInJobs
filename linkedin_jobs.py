#! python3
# -*- coding: utf-8 -*-
"""
linkedin_jobs.py — LinkedIn DevOps Jobs Monitor

Scrapes LinkedIn's public job search for DevOps (and related) jobs posted
in the last 24 hours, filters by experience level, deduplicates, and sends
Discord notifications for every new match.

Features:
  - Multi-keyword search (DevOps, SRE, Platform Engineer, ...)
  - Pagination — scrapes multiple result pages per keyword
  - Company blocklist — silently skip unwanted companies
  - Priority company badge — ★ for Indian IT MNCs
  - Within-run duplicate detection
  - Log rotation (5 MB max, 5 backups)
  - Proxy rotation support
  - Deep scan of job descriptions for YOE extraction
  - External portal scan when LinkedIn desc has no YOE

YOE filtering (3+ YOE target):
  - "3-5 years" extracts 3 (the MINIMUM of the range) → PASS
  - "5+ years of experience with 2+ years Kubernetes" → 5 primary → SKIP
  - Sub-skill years (AWS, Kubernetes, Docker) are classified as SECONDARY
    and do not lower the extracted primary requirement.

Usage:
    python linkedin_jobs.py            # headless run (GitHub Actions / cron)
    python linkedin_jobs.py --manual   # visible Chrome (debug only)
    python linkedin_jobs.py --schedule # daemon every 30 min
    python linkedin_jobs.py --test     # send 2 test embeds to Discord
"""

import logging
import logging.handlers
import os
import re
import sys
import time
import urllib.parse
from random import choice, randint
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

try:
    import feedparser          # RSS fallback (rarely succeeds — LinkedIn deprecated RSS)
except ImportError:
    feedparser = None

import requests
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    from webdriver_manager.chrome import ChromeDriverManager
    _WDM_AVAILABLE = True
except ImportError:
    _WDM_AVAILABLE = False

try:
    import schedule as schedule_lib
except ImportError:
    schedule_lib = None

import constants
import job_store
import notifier

# ── Logging (with rotation, anchored to script directory) ─────────────────────
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "linkedin_jobs.log")
_log_handler_file = logging.handlers.RotatingFileHandler(
    _LOG_PATH,
    maxBytes=5 * 1024 * 1024,   # 5 MB per file
    backupCount=5,               # keep 5 rotated files
    encoding="utf-8",
)
_log_handler_file.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))

_log_handler_console = logging.StreamHandler(sys.stdout)
_log_handler_console.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[_log_handler_file, _log_handler_console])
logger = logging.getLogger(__name__)

# ── User-agent pool ───────────────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5_2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 12_7_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# ── Pre-compute lowercased blocklist / priority lists ─────────────────────────
_BLOCKLIST_LOWER  = [c.lower() for c in constants.COMPANY_BLOCKLIST]
_PRIORITY_LOWER   = [c.lower() for c in constants.PRIORITY_COMPANIES]
_SKIP_TITLE_LOWER = [kw.lower() for kw in constants.SKIP_TITLE_KEYWORDS]

# Executive titles → always SKIP regardless of YOE found (overqualified / managerial)
_EXECUTIVE_LOWER  = [
    "principal", "director", "vp ", "vice president", "head of",
    "staff architect", "chief architect", "enterprise architect",
    "avp", "associate vice president",
    "module lead", "tech lead", "team lead",
    "l6", "l7",
    "distinguished",
]

# Senior titles → SKIP only when no YOE found anywhere (description is silent)
# If a senior role explicitly says "3-4 yrs", YOE filter handles it correctly.
_SENIOR_KEYWORDS  = [
    "senior", "sr.", "sr ", "lead", "staff", "architect",
    "manager", "l5",
]


# ═══════════════════════════════════════════════════════════════════════════════
# URL BUILDING
# ═══════════════════════════════════════════════════════════════════════════════

def build_search_url(keyword: str, page: int = 0) -> str:
    """
    Build a LinkedIn job search URL for a given keyword and page.
    page=0 → results 1-25, page=1 → results 26-50, etc.
    """
    start = page * 25
    encoded_keyword = urllib.parse.quote(f'"{keyword}"')
    encoded_location = urllib.parse.quote(constants.SEARCH_LOCATION)
    return (
        f"https://www.linkedin.com/jobs/search/"
        f"?f_JT=F&f_TPR={constants.DATE_POSTED_FILTER}"
        f"&keywords={encoded_keyword}"
        f"&location={encoded_location}"
        f"&sortBy=DD&start={start}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# RSS LIGHTWEIGHT FETCH (fast pre-check — falls back to Selenium if fails)
# ═══════════════════════════════════════════════════════════════════════════════

def try_rss_fetch(keyword: str) -> List[dict]:
    """
    Attempt a lightweight RSS/JSON fetch for a keyword.
    LinkedIn has deprecated public RSS, so this may return nothing —
    that's fine, the Selenium scraper takes over.

    Returns a list of partial job dicts (no description — deep scan still needed).
    Returns [] on failure.
    """
    if not constants.USE_RSS_FALLBACK or feedparser is None:
        return []

    try:
        rss_url = (
            f"https://www.linkedin.com/jobs/search/?keywords={urllib.parse.quote(keyword)}"
            f"&location={urllib.parse.quote(constants.SEARCH_LOCATION)}"
            f"&f_TPR={constants.DATE_POSTED_FILTER}&sortBy=DD&format=json"
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; RSS reader)",
            "Accept":     "application/rss+xml, application/xml, text/xml",
        }
        resp = requests.get(rss_url, headers=headers, timeout=10)
        if resp.status_code != 200 or "<rss" not in resp.text[:500]:
            return []   # LinkedIn didn't return RSS

        feed = feedparser.parse(resp.text)
        jobs = []
        for entry in feed.entries:
            raw_url = entry.get("link", "")
            job_id  = _extract_job_id(raw_url)
            title   = entry.get("title", "")
            summary = entry.get("summary", "")
            jobs.append({
                "id":          job_id,
                "title":       title,
                "company":     "",          # not in RSS
                "location":    "",
                "posted_time": "",
                "url":         raw_url.split("?")[0],
                "yoe_found":   None,
                "source":      "rss",
            })

        logger.info("RSS fetch returned %d jobs for keyword '%s'.", len(jobs), keyword)
        return jobs

    except Exception as e:
        logger.debug("RSS fetch failed for '%s': %s (Selenium will take over)", keyword, e)
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# CHROME DRIVER
# ═══════════════════════════════════════════════════════════════════════════════

def launch_chrome(headless: bool = True) -> webdriver.Chrome:
    """Launch Chrome with anti-bot settings and optional proxy."""
    options = webdriver.ChromeOptions()
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # Proxy rotation
    if constants.PROXY_LIST:
        proxy = choice(constants.PROXY_LIST)
        options.add_argument(f"--proxy-server={proxy}")
        logger.info("Using proxy: %s", proxy)

    ua = choice(USER_AGENTS)
    options.add_argument(f"user-agent={ua}")
    logger.info("User-agent: %s...", ua[:65])
    options.add_argument(f"--window-size={randint(1280,1440)},{randint(800,920)}")

    if headless:
        options.add_argument("--headless=new")

    # Prefer webdriver-manager (auto-matches ChromeDriver version), fall back to system
    try:
        if _WDM_AVAILABLE:
            service = ChromeService(ChromeDriverManager().install())
            driver = webdriver.Chrome(options=options, service=service)
        else:
            raise RuntimeError("webdriver-manager not available")
    except Exception:
        try:
            driver = webdriver.Chrome(options=options, service=ChromeService())
        except Exception:
            driver = webdriver.Chrome(options=options)

    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    driver.implicitly_wait(0)
    logger.info("Chrome launched (headless=%s).", headless)
    return driver


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE / MODAL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _dismiss_modals(driver: webdriver.Chrome) -> None:
    """Dismiss LinkedIn login/sign-up modals that cause visible pauses."""
    selectors = [
        "button[data-tracking-control-name='public_jobs_contextual-sign-in-modal_modal_dismiss']",
        "button[aria-label='Dismiss']",
        "button.modal__dismiss",
        ".modal__dismiss",
        "button[data-test-modal-close-btn]",
        "button[aria-label='Close']",
    ]
    for sel in selectors:
        try:
            btns = driver.find_elements(By.CSS_SELECTOR, sel)
            for btn in btns:
                if btn.is_displayed():
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(0.2)
                    break
        except Exception:
            continue
    try:
        webdriver.ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    except Exception:
        pass


def _is_auth_wall(driver: webdriver.Chrome) -> bool:
    return any(k in driver.current_url for k in ["authwall", "/login", "/signup", "uas/login"])


def wait_for_jobs_to_load(driver: webdriver.Chrome, timeout: int = 25) -> bool:
    for sel in ["ul.jobs-search__results-list", "div.jobs-search-results-list",
                ".base-card", ".job-search-card"]:
        try:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, sel))
            )
            logger.info("Job list loaded (%s).", sel)
            return True
        except TimeoutException:
            continue
    logger.warning("Job list did not load within %ds.", timeout)
    return False


def scroll_to_load_jobs(driver: webdriver.Chrome, scrolls: int = 5) -> None:
    for _ in range(scrolls):
        driver.execute_script("window.scrollBy(0, 800);")
        time.sleep(randint(500, 900) / 1000.0)
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(0.4)


# ═══════════════════════════════════════════════════════════════════════════════
# JOB CARD PARSING
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_text(el, css: str, default: str = "") -> str:
    try:
        found = el.find_elements(By.CSS_SELECTOR, css)
        if found:
            t = found[0].text.strip()
            if t:
                return t
        return default
    except Exception:
        return default


def _safe_attr(el, css: Optional[str], attr: str, default: str = "") -> str:
    try:
        target = el
        if css:
            found = el.find_elements(By.CSS_SELECTOR, css)
            if not found:
                return default
            target = found[0]
        val = target.get_attribute(attr)
        if val:
            return val.strip()
        return default
    except Exception:
        return default


def _extract_job_id(url: str) -> str:
    """Extract the canonical numeric LinkedIn job ID from any URL or URN variant.

    LinkedIn job identifiers come in many forms:
      - https://www.linkedin.com/jobs/view/1234567890/
      - https://www.linkedin.com/jobs/view/job-title-at-company-1234567890/
      - https://in.linkedin.com/jobs/view/devops-engineer-at-wipro-1234567890
      - /jobs/view/1234567890?trackingId=...
      - https://www.linkedin.com/jobs/search/?currentJobId=1234567890
      - urn:li:jobPosting:1234567890

    We extract ONLY the number, strip everything else → deterministic dedup.
    """
    if not url:
        return ""
    # 1. Query parameters: ?currentJobId=1234567890 or &jobId=1234567890
    m_param = re.search(r'[?&](?:currentJobId|jobId)=(\d{7,})', url)
    if m_param:
        return m_param.group(1)

    # 2. URN: urn:li:jobPosting:1234567890
    m_urn = re.search(r'urn:li:jobPosting:(\d{7,})', url)
    if m_urn:
        return m_urn.group(1)

    # 3. Path digits after hyphen or slash: e.g. -4481511083 or /4481511083
    clean = url.split("?")[0].split("#")[0].rstrip("/")
    nums = re.findall(r'(\d{7,})', clean)
    if nums:
        return nums[-1]

    # 4. Any 7+ digit number anywhere in the string
    m_any = re.search(r'(\d{7,})', url)
    if m_any:
        return m_any.group(1)

    return clean


def _parse_single_card(card) -> Optional[dict]:
    title = ""
    for sel in [".base-search-card__title", "h3.base-search-card__title", "h3"]:
        title = _safe_text(card, sel)
        if title:
            break

    company = ""
    for sel in [".base-search-card__subtitle", "h4.base-search-card__subtitle",
                ".job-search-card__company-name", "a.hidden-nested-link"]:
        company = _safe_text(card, sel)
        if company:
            break

    location = _safe_text(card, ".job-search-card__location",
               _safe_text(card, ".base-search-card__metadata"))

    posted_time = ""
    for sel in [".job-search-card__listdate", ".job-search-card__listed-status"]:
        posted_time = _safe_text(card, sel)
        if not posted_time:
            posted_time = _safe_attr(card, "time", "datetime")
        if posted_time:
            break

    # Extract Job ID directly from card DOM attributes if present
    job_id = ""
    for attr in ["data-entity-urn", "data-job-id", "data-id", "data-occludable-job-id"]:
        val = _safe_attr(card, None, attr)
        if val:
            extracted = _extract_job_id(val)
            if extracted and extracted.isdigit():
                job_id = extracted
                break

    url = ""
    for sel in ["a.base-card__full-link", "a[data-tracking-control-name]", "a"]:
        raw = _safe_attr(card, sel, "href")
        if raw and "linkedin.com/jobs" in raw:
            if not job_id:
                extracted = _extract_job_id(raw)
                if extracted and extracted.isdigit():
                    job_id = extracted
            url = raw.split("?")[0]
            break

    if not job_id and url:
        job_id = _extract_job_id(url)

    if not title or (not url and not job_id):
        return None

    # Canonicalize URL to ensure consistent, clean links
    if job_id and job_id.isdigit():
        canonical_url = f"https://www.linkedin.com/jobs/view/{job_id}/"
        if not url or "search" in url or not url.startswith("http"):
            url = canonical_url

    return {
        "id":          job_id,
        "title":       title,
        "company":     company,
        "location":    location,
        "posted_time": posted_time,
        "url":         url,
        "yoe_found":   None,
        "is_priority": False,
        "is_duplicate": False,
    }


def _looks_like_job_card(el) -> bool:
    try:
        html = el.get_attribute("outerHTML") or ""
        return any(kw in html for kw in ["base-card", "job-search-card", "base-search-card"])
    except Exception:
        return False


def parse_job_cards(driver: webdriver.Chrome) -> List[dict]:
    """Parse all visible job cards. Returns list of job dicts."""
    _dismiss_modals(driver)
    cards = []
    for sel in ["ul.jobs-search__results-list > li",
                "li.jobs-search__results-list__list-item",
                "div.base-card",
                "ul.jobs-search__results-list li"]:
        try:
            found    = driver.find_elements(By.CSS_SELECTOR, sel)
            filtered = [c for c in found if _looks_like_job_card(c)]
            if filtered:
                cards = filtered
                logger.info("Found %d cards via '%s'.", len(cards), sel)
                break
        except Exception:
            continue

    jobs = []
    for card in cards[:constants.MAX_JOBS_PER_RUN]:
        try:
            job = _parse_single_card(card)
            if job:
                jobs.append(job)
        except Exception as e:
            logger.debug("Card parse error: %s", e)

    logger.info("Parsed %d jobs from this page.", len(jobs))
    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# COMPANY CHECKS
# ═══════════════════════════════════════════════════════════════════════════════

def _company_is_blocked(company: str) -> bool:
    cl = company.lower()
    return any(b in cl for b in _BLOCKLIST_LOWER)


def _company_is_priority(company: str) -> bool:
    """Word-boundary aware priority match — 'HP' won't match 'PHPDev Corp'."""
    cl = company.lower()
    for p in _PRIORITY_LOWER:
        # Use word boundary if the keyword is purely alphabetic; else plain substring
        if re.search(r'\b' + re.escape(p) + r'\b', cl):
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# WITHIN-RUN DUPLICATE DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation and common filler words for comparison."""
    t = title.lower()
    t = re.sub(r'[^a-z0-9\s]', ' ', t)
    # Keep role-defining words like "engineer", "cloud", "devops" — they distinguish jobs
    stop = {"senior", "lead", "and", "for", "the", "at", "in",
            "of", "with", "on", "a", "an", "to", "role", "position", "job", "india",
            "ii", "iii", "iv", "i"}
    return " ".join(w for w in t.split() if w not in stop)


def tag_duplicates(jobs: List[dict]) -> None:
    """
    Mutates job dicts in-place to mark likely duplicates.
    Two jobs are considered duplicates if their normalised title + company + location
    combination has appeared before in this same run.
    """
    seen_sigs: set = set()
    for job in jobs:
        sig = (
            _normalize_title(job.get("title", "")),
            job.get("company", "").lower().strip()[:30],
            job.get("location", "").lower()[:20],
        )
        if sig in seen_sigs:
            job["is_duplicate"] = True
            logger.info("Duplicate detected: %s @ %s (%s)", job.get("title"), job.get("company"), job.get("location"))
        else:
            seen_sigs.add(sig)


# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD EXPORT
# ═══════════════════════════════════════════════════════════════════════════════

def write_dashboard_jobs(eligible_jobs: List[dict]) -> None:
    """
    Write eligible jobs to dashboard_jobs.json for the web dashboard.

    Accumulates across runs (48h rolling window):
      1. Load existing dashboard_jobs.json
      2. Merge new eligible jobs (preserve original found_at)
      3. Prune entries older than 48h
      4. Write sorted output (priority first, then by YOE desc)
    """
    import json as _json

    dashboard_file = getattr(constants, "DASHBOARD_JOBS_FILE", "data/dashboard_jobs.json")

    # Load existing dashboard data
    existing: Dict[str, dict] = {}
    try:
        with open(dashboard_file, "r", encoding="utf-8") as f:
            data = _json.load(f)
            if isinstance(data, dict) and "jobs" in data:
                existing = {j["id"]: j for j in data["jobs"] if "id" in j}
    except (FileNotFoundError, _json.JSONDecodeError, OSError):
        pass

    # Merge new jobs (newer data wins, preserve found_at)
    now_iso = datetime.now(timezone.utc).isoformat()
    for job in eligible_jobs:
        jid = job.get("id", "")
        if not jid:
            continue
        entry = {
            "id":           jid,
            "title":        job.get("title", ""),
            "company":      job.get("company", ""),
            "location":     job.get("location", ""),
            "posted_time":  job.get("posted_time", ""),
            "url":          job.get("url", ""),
            "yoe_found":    job.get("yoe_found"),
            "yoe_max":      job.get("yoe_max"),
            "is_priority":  job.get("is_priority", False),
            "is_duplicate": job.get("is_duplicate", False),
            "found_at":     existing.get(jid, {}).get("found_at", now_iso),
        }
        existing[jid] = entry

    # Prune entries older than 48h
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    pruned: Dict[str, dict] = {}
    for jid, job in existing.items():
        try:
            ts = job.get("found_at", "")
            # Handle both timezone-aware and naive ISO strings
            found = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
            if found is None:
                pruned[jid] = job
            else:
                if found.tzinfo is None:
                    found = found.replace(tzinfo=timezone.utc)
                if found >= cutoff:
                    pruned[jid] = job
        except (ValueError, TypeError):
            pruned[jid] = job  # keep if can't parse

    # Write sorted output
    # Sort: priority first; within same priority, closest YOE match first (ascending)
    # so roles asking for 3 yrs appear before 4 yrs when you have exactly 3 yrs
    output = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "config": {
            "search_keywords":       constants.SEARCH_KEYWORDS_LIST,
            "your_experience_years": constants.YOUR_EXPERIENCE_YEARS,
            "stretch_years":         getattr(constants, "STRETCH_YEARS", 1),
            "search_location":       constants.SEARCH_LOCATION,
            "enable_discord":        getattr(constants, "ENABLE_DISCORD", False),
        },
        "total_jobs": len(pruned),
        "jobs": sorted(
            pruned.values(),
            key=lambda j: (not j.get("is_priority", False), (j.get("yoe_found") or 99)),
        ),
    }

    os.makedirs(os.path.dirname(os.path.abspath(dashboard_file)), exist_ok=True)
    with open(dashboard_file, "w", encoding="utf-8") as f:
        _json.dump(output, f, indent=2)

    logger.info("Dashboard: wrote %d jobs to %s", len(pruned), dashboard_file)


# ═══════════════════════════════════════════════════════════════════════════════
# EXPERIENCE FILTERING — Smart eligibility matching
# ═══════════════════════════════════════════════════════════════════════════════
#
# DESIGN:
#   OLD (broken): Extract the min from "3-5 years" → 3 → check if 3 is in
#     [config_min, config_max]. This fails because "2-4 years" extracts 2,
#     and 2 < config_min=3, so it's SKIPPED — even though 3 yrs is IN the range!
#
#   NEW (correct): Extract BOTH sides of the requirement (min AND max),
#     then ask: "Does MY experience (3 yrs) qualify for this job?"
#
#     "1+ years"   → (1, None) → 3 ≥ 1          → ✅ I exceed their minimum
#     "2-4 years"  → (2, 4)    → 3 is in [2, 4]  → ✅ I'm in their range
#     "3-5 years"  → (3, 5)    → 3 is in [3, 5]  → ✅ At the bottom of range
#     "4+ years"   → (4, None) → 3+1 ≥ 4         → ✅ Stretch (companies flex)
#     "5+ years"   → (5, None) → 3+1 < 5          → ❌ Too senior
#     "0-1 years"  → (0, 1)    → fresher range    → ❌ Overqualified
# ═══════════════════════════════════════════════════════════════════════════════

# Patterns that indicate a year mention is about a sub-skill/tool, NOT the overall requirement.
# e.g. "8+ years experience ... with 2+ years in Kubernetes" → the 2 is secondary.
_TECH_PATTERNS = (
    r'(?:AWS|Azure|GCP|Google\s+Cloud|Kubernetes|K8s|Docker|'
    r'Terraform|Ansible|Jenkins|CI/?CD|Git|Linux|Python|Go\b|Java|Helm|ArgoCD|GitOps|'
    r'Prometheus|Grafana|Datadog|EKS|AKS|GKE|ECS|Fargate|CloudFormation|CDK|'
    r'Bash|Shell|Puppet|Chef|Vagrant|VMware|OpenStack|Istio|Vault|Consul|'
    r'Splunk|ElasticSearch|Kibana|Kafka|RabbitMQ|PostgreSQL|MySQL|MongoDB|Redis)'
)

_SECONDARY_CONTEXT_RE = re.compile(
    r'\b(?:with|including|of\s+which|out\s+of\s+which|'  # transitional words
    r'hands[\s-]?on\s+(?:experience\s+)?(?:in|with|on)|'   # "hands-on in/with"
    r'proficiency|proficient|expertise|exposure|familiarity)\s+'
    r'(?:in|with|on|of)?\s*(?:' + _TECH_PATTERNS + ')?',
    re.IGNORECASE,
)

_SECONDARY_AFTER_RE = re.compile(
    rf'^(?:\s*\+)?\s*(?:years?|yrs?)?\s*'
    rf'(?:'
        rf'(?:of\s+)?(?:hands[\s-]?on|practical|working)?\s*(?:experience|exp)?\s*(?:in|with|on)\s+{_TECH_PATTERNS}|'
        rf'(?:in|with|on)\s+{_TECH_PATTERNS}|'
        rf'(?:of\s+)?{_TECH_PATTERNS}\s+(?:experience|exp)'
    rf')',
    re.IGNORECASE,
)


def _extract_yoe_requirement(text: str) -> Optional[Tuple[float, Optional[float]]]:
    """
    Context-aware extraction of the experience requirement from job text.

    Returns:
        (min_yoe, max_yoe) where:
            min_yoe = minimum years the job requires
            max_yoe = upper bound if range was specified, or None if open-ended
        None if no YOE info found in text.

    Examples:
        "3+ years of experience"      → (3.0, None)   open-ended minimum
        "3-5 years of experience"     → (3.0, 5.0)    bounded range
        "minimum 2 years"             → (2.0, None)   open-ended minimum
        "2 to 4 years experience"     → (2.0, 4.0)    bounded range
        "Experience with CI/CD"       → None           no YOE info

    Sub-skill classification:
        "8+ yrs exp, with 2+ in K8s"  → (8.0, None)  the 2 is SECONDARY (sub-skill)
        Only if no primary is found, max(secondary) is returned as open-ended.
    """
    num = r'(\d+(?:\.\d+)?)'
    # Each entry is (min_yoe, max_yoe_or_None)
    primary: List[Tuple[float, Optional[float]]] = []
    secondary: List[Tuple[float, Optional[float]]] = []

    def _classify(m: re.Match, context_before: str, context_after: str = "") -> str:
        """Return 'primary' or 'secondary' based on surrounding context."""
        before = context_before.lower()
        if _SECONDARY_CONTEXT_RE.search(before[-80:]):
            return "secondary"
        if context_after and _SECONDARY_AFTER_RE.search(context_after[:80]):
            return "secondary"
        return "primary"

    def _add_match(val: float, m: re.Match,
                   range_max: Optional[float] = None,
                   force_secondary: bool = False):
        entry = (val, range_max)
        if force_secondary:
            secondary.append(entry)
            return
        start = max(0, m.start() - 80)
        ctx_before = text[start:m.start()]
        ctx_after = text[m.end():m.end() + 80]
        kind = _classify(m, ctx_before, ctx_after)
        if kind == "primary":
            primary.append(entry)
        else:
            secondary.append(entry)

    # ── PRIMARY pattern 1: 'minimum/min/at least X years' ────────────────────
    for m in re.finditer(
        rf'(?:minimum|min\.?|at\s+least)\s+{num}\s*\+?\s*(?:years?|yrs?)',
        text, re.IGNORECASE
    ):
        _add_match(float(m.group(1)), m)

    # ── PRIMARY pattern 2: 'X+ years of experience' (NOT sub-skill) ──────────
    for m in re.finditer(
        rf'{num}\s*\+\s*(?:years?|yrs?)\s+(?:of\s+)?(?:relevant\s+|total\s+)?(?:work\s+)?experience'
        rf'(?!\s+(?:in\s+business|old|ago|history))\b',
        text, re.IGNORECASE
    ):
        _add_match(float(m.group(1)), m)

    # ── PRIMARY pattern 3: 'experience of/: X years' ─────────────────────────
    for m in re.finditer(
        rf'(?:^|[\n.;])\s*(?:total\s+)?(?:relevant\s+)?experience[:\s]+(?:of\s+)?\s*{num}\s*\+?\s*(?:years?|yrs?)',
        text, re.IGNORECASE | re.MULTILINE
    ):
        _add_match(float(m.group(1)), m)

    # ── PRIMARY pattern 4: 'X years of experience' / 'X years experience' ────
    for m in re.finditer(
        rf'(?<![-–\d.]){num}\s*(?:years?|yrs?)\s+(?:of\s+)?(?:relevant\s+|total\s+)?(?:work\s+)?(?:experience|exp)\b',
        text, re.IGNORECASE
    ):
        _add_match(float(m.group(1)), m)

    # ── RANGE pattern: 'X-Y years' / 'X to Y years' → capture BOTH sides ────
    # These are nearly always primary (e.g. "3-5 years of experience")
    for m in re.finditer(
        rf'{num}\s*[-–]\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?)',
        text, re.IGNORECASE
    ):
        _add_match(float(m.group(1)), m, range_max=float(m.group(2)))

    for m in re.finditer(
        rf'{num}\s+to\s+(\d+(?:\.\d+)?)\s*(?:years?|yrs?)',
        text, re.IGNORECASE
    ):
        _add_match(float(m.group(1)), m, range_max=float(m.group(2)))

    # ── Explicit tech sub-skill: X(+) years of <TECH> experience / in <TECH> ──
    for m in re.finditer(
        rf'{num}\s*\+?\s*(?:years?|yrs?)\s+(?:of\s+)?{_TECH_PATTERNS}\s+(?:experience|exp)\b',
        text, re.IGNORECASE
    ):
        _add_match(float(m.group(1)), m, force_secondary=True)

    for m in re.finditer(
        rf'{num}\s*\+?\s*(?:years?|yrs?)\s+(?:in|with|on)\s+{_TECH_PATTERNS}\b',
        text, re.IGNORECASE
    ):
        _add_match(float(m.group(1)), m, force_secondary=True)

    # ── SECONDARY / ambiguous: 'X+ years' with no explicit 'of experience' ────
    for m in re.finditer(
        rf'{num}\s*\+\s*(?:years?|yrs?)(?!\s+(?:in\s+business|old|ago|history|of\s+experience))\b',
        text, re.IGNORECASE
    ):
        val = float(m.group(1))
        already = (any(abs(val - p[0]) < 0.01 for p in primary) or
                   any(abs(val - s[0]) < 0.01 for s in secondary))
        if not already:
            _add_match(val, m)

    # ── Parenthesized YOE in titles: '(2 yrs)', '(3 years)' ──────────────────
    for m in re.finditer(
        rf'\(\s*(?:min\s+)?{num}\s*\+?\s*(?:years?|yrs?)\s*\)',
        text, re.IGNORECASE
    ):
        primary.append((float(m.group(1)), None))

    # ── Decide final value ────────────────────────────────────────────────────
    if primary:
        # Pick the entry with the lowest min_yoe (the most accessible requirement)
        best = min(primary, key=lambda x: x[0])
        logger.debug("YOE req: primary=%s secondary=%s → using %s", primary, secondary, best)
        return best
    if secondary:
        # No explicit overall requirement found; the highest sub-skill YOE is
        # our best proxy for the overall seniority expected (open-ended).
        best = max(secondary, key=lambda x: x[0])
        logger.debug("YOE req: no primary, secondary=%s → using max %s", secondary, best)
        return best
    return None


def _extract_primary_yoe(text: str) -> Optional[float]:
    """Backward-compatible wrapper — returns just the min requirement as a number."""
    result = _extract_yoe_requirement(text)
    if result is None:
        return None
    val = result[0]
    return int(val) if float(val).is_integer() else round(val, 1)


# Keep old name as alias
_extract_min_years_required = _extract_primary_yoe


# ═══════════════════════════════════════════════════════════════════════════════
# SMART ELIGIBILITY CHECK
# ═══════════════════════════════════════════════════════════════════════════════

def _is_eligible_for_role(
    your_exp: float,
    req_min: float,
    req_max: Optional[float],
    stretch: float = 1.0,
) -> Tuple[bool, str]:
    """
    Determine if YOUR experience qualifies you for a job's requirement.

    This is fundamentally different from the old min/max comparison:
        OLD: "Does the extracted number fall in [config_min, config_max]?"
             → "2-4 years" extracts 2, and 2 < 3, so SKIP — WRONG!
        NEW: "Does my 3 years fit in the job's [2, 4] range?"
             → 3 is between 2 and 4 — PASS!

    Args:
        your_exp: Your actual years of experience (e.g. 3)
        req_min:  Job's minimum requirement (e.g. 2 from "2-4 years")
        req_max:  Job's max if range (e.g. 4 from "2-4"), or None for "2+ years"
        stretch:  Years above your exp you're willing to reach for (default 1)

    Returns:
        (eligible: bool, reason: str)

    Decision flow:
        1. Fresher range? (max ≤ 1)      → ❌ you're overqualified
        2. Overqualified? (you >> max)    → ❌ they want someone more junior
        3. Can you reach their minimum?   → ✅/❌ with stretch
    """
    # ── Rule 1: Fresher range exclusion ───────────────────────────────────────
    # "0-1 years" means they want a fresher. With 3 yrs you're way past that.
    if req_max is not None and req_max <= 1:
        return False, f"fresher range (0-{req_max:.0f}yr)"

    # ── Rule 2: Overqualified for explicit range ──────────────────────────────
    # "1-2 years" and you have 5 → 5 > 2 + 1 = 3 → too senior for them
    # "2-3 years" and you have 3 → 3 > 3 + 1 = 4? No → still OK
    if req_max is not None and your_exp > req_max + stretch:
        return False, f"overqualified for {req_min:.0f}-{req_max:.0f}yr (have {your_exp:.0f})"

    # ── Rule 3: Can you meet their minimum (with stretch)? ────────────────────
    # "4+ years" → need 4, you have 3, stretch 1 → 4 ≤ 3+1=4 → ✅
    # "5+ years" → need 5, you have 3, stretch 1 → 5 ≤ 4 → ❌
    if req_min <= your_exp + stretch:
        if req_max is not None:
            return True, f"fit {req_min:.0f}-{req_max:.0f}yr range (have {your_exp:.0f})"
        else:
            return True, f"meet {req_min:.0f}+ requirement (have {your_exp:.0f})"
    else:
        return False, f"need {req_min:.0f}+ yrs (have {your_exp:.0f}, stretch +{stretch:.0f} not enough)"


def _set_yoe_on_job(job: dict, req_min: float, req_max: Optional[float]) -> None:
    """Store the extracted YOE requirement on the job dict for dashboard/notifications."""
    job["yoe_found"] = int(req_min) if float(req_min).is_integer() else round(req_min, 1)
    if req_max is not None:
        job["yoe_max"] = int(req_max) if float(req_max).is_integer() else round(req_max, 1)


def _title_is_skip(title: str) -> bool:
    tl = title.lower()
    # Regex word-boundary for intern/internship catches "DevOps Intern" and "Internship"
    if re.search(r'\bintern(s|ship)?\b', tl):
        return True
    if re.search(r'\b(junior|entry[\s-]?level|fresher|trainee|apprentice)\b', tl):
        return True
    return any(kw in tl for kw in _SKIP_TITLE_LOWER)


def _title_is_executive(title: str) -> bool:
    tl = title.lower()
    return any(kw in tl for kw in _EXECUTIVE_LOWER)


def _title_implies_senior(title: str) -> bool:
    tl = title.lower()
    return any(kw in tl for kw in _SENIOR_KEYWORDS)


def meets_experience_requirement(job: dict, driver: webdriver.Chrome = None) -> bool:
    """
    Decide whether this job matches your experience level.

    Uses smart eligibility matching — not a dumb number range comparison.

    With YOUR_EXPERIENCE_YEARS = 3 and STRETCH_YEARS = 1:

        "1+ years"     → 3 ≥ 1     → ✅ (you exceed their minimum)
        "2-4 years"    → 3 in [2,4] → ✅ (you're in their range)
        "3-5 years"    → 3 in [3,5] → ✅ (you're at the bottom of their range)
        "4+ years"     → 3+1 ≥ 4   → ✅ (stretch — companies often flex ±1 yr)
        "5+ years"     → 3+1 < 5   → ❌ (too senior even with stretch)
        "0-1 years"    → fresher   → ❌ (you're overqualified)
        No YOE found   →           → title heuristics, then INCLUDE
    """
    your_exp = constants.YOUR_EXPERIENCE_YEARS
    stretch  = getattr(constants, "STRETCH_YEARS", 1)
    title    = job.get("title", "")

    if constants.DEEP_SCAN and driver is not None:
        return _deep_scan_experience(driver, job)

    # Try title for YOE
    req = _extract_yoe_requirement(title)
    if req is not None:
        req_min, req_max = req
        eligible, reason = _is_eligible_for_role(your_exp, req_min, req_max, stretch)
        if eligible:
            _set_yoe_on_job(job, req_min, req_max)
        logger.info("%s (title YOE: %s — %s): %s",
                    "PASS" if eligible else "SKIP", req, reason, title)
        return eligible

    if _title_is_skip(title):
        logger.info("SKIP (fresher/intern title): %s", title)
        return False

    if _title_is_executive(title):
        logger.info("SKIP (executive title, no YOE): %s", title)
        return False

    if _title_implies_senior(title):
        logger.info("SKIP (senior title, no YOE, no deep scan): %s", title)
        return False

    logger.info("INCLUDE (no YOE info): %s", title)
    return True


def _scrape_external_portal_yoe(
    driver: webdriver.Chrome,
    job: dict,
    linkedin_tab: str,
) -> Optional[Tuple[float, Optional[float]]]:
    """
    Click the 'Apply on company website' button on the currently-open LinkedIn
    job detail page, wait for the external portal to load in a new tab, scrape
    its text, extract YOE requirement, then close the tab and return.

    Returns (min_yoe, max_yoe_or_None) tuple, or None if no YOE found.
    """
    if not getattr(constants, "EXTERNAL_PORTAL_SCAN", False):
        return None

    title = job.get("title", "")
    timeout = getattr(constants, "EXTERNAL_PORTAL_TIMEOUT", 6)

    # ── Find the Apply / external link button ────────────────────────────────
    apply_selectors = [
        "a[data-tracking-control-name='public_jobs_apply-link-offsite_sign-up-modal']",
        "a[data-tracking-control-name='public_jobs_apply-link-offsite']",
        "button[data-tracking-control-name='public_jobs_apply-link-offsite_sign-up-modal']",
        "button[data-tracking-control-name='public_jobs_apply-link-offsite']",
        "a.apply-button--offsite",
        "button.apply-button--offsite",
        "a.top-card-layout__cta",
        "button.top-card-layout__cta",
        "a[href*='applyUrl']",
        "a.sign-up-modal__outlet-btn",
        "button.jobs-apply-button",
        "a.jobs-apply-button",
        "a[class*='apply']",
        "button[class*='apply']",
    ]

    apply_btn = None
    for sel in apply_selectors:
        try:
            found = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in found:
                if el.is_displayed():
                    apply_btn = el
                    break
            if apply_btn:
                break
        except Exception:
            continue

    if apply_btn is None:
        logger.debug("EXTERNAL_SCAN: No Apply button found for: %s", title)
        return None

    # Record the href before clicking (some portals open in same tab)
    try:
        portal_href = apply_btn.get_attribute("href") or ""
    except Exception:
        portal_href = ""

    handles_before = set(driver.window_handles)
    try:
        driver.execute_script("arguments[0].click();", apply_btn)
        time.sleep(1.5)
    except Exception as e:
        logger.debug("EXTERNAL_SCAN: Click failed for '%s': %s", title, e)
        return None

    handles_after = set(driver.window_handles)
    new_handles = handles_after - handles_before

    if new_handles:
        ext_handle = new_handles.pop()
        driver.switch_to.window(ext_handle)
    elif portal_href and portal_href.startswith("http"):
        try:
            driver.get(portal_href)
        except Exception as e:
            logger.debug("EXTERNAL_SCAN: Navigate failed for '%s': %s", title, e)
            try:
                driver.switch_to.window(linkedin_tab)
            except Exception:
                pass
            return None
    else:
        logger.debug("EXTERNAL_SCAN: No new tab and no href for: %s", title)
        return None

    # ── Scrape the external portal page ──────────────────────────────────────
    ext_req: Optional[Tuple[float, Optional[float]]] = None
    try:
        portal_url = driver.current_url
        logger.info("EXTERNAL_SCAN: Opened portal %s for: %s", portal_url[:80], title)

        try:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except TimeoutException:
            pass
        time.sleep(1.0)

        _dismiss_modals(driver)

        ext_text = ""
        ext_desc_selectors = [
            "[class*='job-description']", "[class*='jobDescription']",
            "[class*='job_description']", "[id*='job-description']",
            "[id*='jobDescription']", "[class*='description-content']",
            "[class*='job-details']", "[id*='job-details']",
            "article", "main",
        ]
        for sel in ext_desc_selectors:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                if els:
                    t = els[0].text.strip()
                    if t and len(t) > 100:
                        ext_text = t
                        break
            except Exception:
                continue

        if not ext_text:
            try:
                ext_text = driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                pass

        if ext_text:
            ext_req = _extract_yoe_requirement(ext_text)
            if ext_req is not None:
                logger.info("EXTERNAL_SCAN: Found %s on portal for: %s", ext_req, title)
            else:
                logger.info("EXTERNAL_SCAN: No YOE on portal for: %s", title)
        else:
            logger.info("EXTERNAL_SCAN: Couldn't read portal text for: %s", title)

    except Exception as e:
        logger.warning("EXTERNAL_SCAN: Error scraping portal for '%s': %s", title, e)

    # ── Close the external tab and return to LinkedIn tab ─────────────────────
    try:
        if driver.current_window_handle != linkedin_tab:
            driver.close()
        driver.switch_to.window(linkedin_tab)
    except Exception:
        try:
            driver.switch_to.window(linkedin_tab)
        except Exception:
            pass

    return ext_req


def _deep_scan_experience(driver: webdriver.Chrome, job: dict) -> bool:
    """Open job page in new tab, extract description, check YOE eligibility.
    If LinkedIn description has no YOE, follow the Apply button to the external
    portal and check there too before making an include/skip decision.
    """
    your_exp = constants.YOUR_EXPERIENCE_YEARS
    stretch  = getattr(constants, "STRETCH_YEARS", 1)
    title    = job.get("title", "")
    job_id   = job.get("id", "")
    url      = job.get("url", "")

    view_url = f"https://www.linkedin.com/jobs/view/{job_id}/" if job_id.isdigit() else url

    try:
        orig = driver.current_window_handle
        driver.execute_script("window.open(arguments[0], '_blank');", view_url)
        driver.switch_to.window(driver.window_handles[-1])
        linkedin_tab = driver.current_window_handle
        time.sleep(randint(1500, 2200) / 1000.0)

        if _is_auth_wall(driver):
            driver.close()
            driver.switch_to.window(orig)
            if _title_is_skip(title) or _title_is_executive(title):
                logger.info("DEEP_SCAN: Auth wall — SKIP (title heuristic): %s", title)
                return False
            if _title_implies_senior(title):
                logger.info("DEEP_SCAN: Auth wall — SKIP (senior title, no desc): %s", title)
                return False
            logger.info("DEEP_SCAN: Auth wall — INCLUDE (title OK): %s", title)
            return True

        _dismiss_modals(driver)
        time.sleep(0.3)

        # Expand description
        for sel in [
            "button[data-tracking-control-name='public_jobs_show-more-html-btn']",
            ".show-more-less-html__button--more",
            "button.show-more-less-html__button",
        ]:
            try:
                btns = driver.find_elements(By.CSS_SELECTOR, sel)
                if btns and btns[0].is_displayed():
                    driver.execute_script("arguments[0].click();", btns[0])
                    time.sleep(0.3)
                    break
            except Exception:
                continue

        # Extract description text
        desc = ""
        for sel in [".description__text", ".show-more-less-html__markup",
                    "#job-details", ".jobs-description-content__text",
                    ".jobs-description__content", "div.description"]:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                if els:
                    t = els[0].text.strip()
                    if t and len(t) > 50:
                        desc = t
                        break
            except Exception:
                continue

        if not desc:
            try:
                desc = driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                pass

        # ── Evaluate LinkedIn description ─────────────────────────────────────
        if desc:
            req = _extract_yoe_requirement(desc)
            if req is not None:
                req_min, req_max = req
                driver.close()
                driver.switch_to.window(orig)
                eligible, reason = _is_eligible_for_role(your_exp, req_min, req_max, stretch)
                if eligible:
                    _set_yoe_on_job(job, req_min, req_max)
                logger.info("DEEP_SCAN %s — %s (%s): %s",
                            "PASS" if eligible else "SKIP", req, reason, title)
                return eligible

            # No YOE in LinkedIn desc — try the external portal
            logger.debug("DEEP_SCAN: No YOE in LinkedIn desc, trying external portal: %s", title)
            ext_req = _scrape_external_portal_yoe(driver, job, linkedin_tab)

            driver.close()
            driver.switch_to.window(orig)

            if ext_req is not None:
                req_min, req_max = ext_req
                eligible, reason = _is_eligible_for_role(your_exp, req_min, req_max, stretch)
                if eligible:
                    _set_yoe_on_job(job, req_min, req_max)
                logger.info("EXTERNAL_SCAN %s — %s (%s): %s",
                            "PASS" if eligible else "SKIP", ext_req, reason, title)
                return eligible

            # Still no YOE anywhere — apply title-based heuristics
            if _title_is_skip(title) or _title_is_executive(title):
                logger.info("DEEP_SCAN SKIP (fresher/executive title, no YOE in desc or portal): %s", title)
                return False
            if _title_implies_senior(title):
                logger.info("DEEP_SCAN SKIP (senior title + no YOE anywhere): %s", title)
                return False
            logger.info("DEEP_SCAN INCLUDE (no YOE in desc or portal): %s", title)
            return True

        else:
            # Couldn't read the LinkedIn description at all — still try external portal
            logger.debug("DEEP_SCAN: Empty LinkedIn desc, trying external portal: %s", title)
            ext_req = _scrape_external_portal_yoe(driver, job, linkedin_tab)

            driver.close()
            driver.switch_to.window(orig)

            if ext_req is not None:
                req_min, req_max = ext_req
                eligible, reason = _is_eligible_for_role(your_exp, req_min, req_max, stretch)
                if eligible:
                    _set_yoe_on_job(job, req_min, req_max)
                logger.info("EXTERNAL_SCAN %s — %s (%s) [li-desc empty]: %s",
                            "PASS" if eligible else "SKIP", ext_req, reason, title)
                return eligible

            if _title_is_skip(title) or _title_is_executive(title):
                logger.info("DEEP_SCAN SKIP (fresher/executive title, couldn't read desc): %s", title)
                return False
            if _title_implies_senior(title):
                logger.info("DEEP_SCAN SKIP (senior title + no YOE anywhere): %s", title)
                return False
            logger.warning("DEEP_SCAN: Couldn't read desc or portal — INCLUDE: %s", title)
            return True

    except Exception as e:
        logger.warning("Deep scan error for '%s': %s", title, e)
        try:
            if len(driver.window_handles) > 1:
                driver.close()
            driver.switch_to.window(driver.window_handles[0])
        except Exception:
            pass
        return not (_title_is_skip(title) or _title_is_executive(title) or _title_implies_senior(title))



# ═══════════════════════════════════════════════════════════════════════════════
# MAIN SCRAPE ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def scrape_keyword(
    driver: webdriver.Chrome,
    keyword: str,
    all_jobs: Dict[str, dict],
) -> int:
    """
    Scrape all pages for a single keyword. Merges results into all_jobs by job ID.
    Returns number of new unique job cards added.
    """
    added = 0
    for page in range(constants.MAX_PAGES):
        url = build_search_url(keyword, page)
        logger.info("Keyword='%s' page=%d → %s", keyword, page + 1, url)
        driver.get(url)
        time.sleep(randint(2000, 3000) / 1000.0)

        if not wait_for_jobs_to_load(driver, timeout=20):
            logger.warning("Page %d didn't load for keyword '%s' — stopping pagination.", page + 1, keyword)
            break

        scroll_to_load_jobs(driver, scrolls=4)
        _dismiss_modals(driver)

        cards = parse_job_cards(driver)
        if not cards:
            logger.info("No cards on page %d — reached end for '%s'.", page + 1, keyword)
            break

        for job in cards:
            jid = job["id"]
            if jid and jid not in all_jobs:
                all_jobs[jid] = job
                added += 1

        # LinkedIn shows 25 results per page; fewer means we hit the last page
        if len(cards) < 22:
            logger.info("Only %d cards on page %d — reached last page for '%s'.",
                        len(cards), page + 1, keyword)
            break

        time.sleep(randint(1500, 2500) / 1000.0)

    logger.info("Keyword '%s': %d unique new cards added.", keyword, added)
    return added


# ═══════════════════════════════════════════════════════════════════════════════
# RUN ONCE
# ═══════════════════════════════════════════════════════════════════════════════

def run_once(headless: bool = True) -> int:
    """
    Single full scrape-and-notify run.

    Flow:
      1. For each keyword → try RSS (fast) → if fails, use Selenium pages
      2. Company blocklist → skip blocked companies
      3. Tag priority companies
      4. Tag within-run duplicates
      5. Deep scan descriptions for YOE
      6. Deduplicate against seen_jobs.json
      7. Notify on Discord (per new job)
      8. Mark seen, cleanup store
    """
    keywords = constants.SEARCH_KEYWORDS_LIST
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("Run Start | Keywords: %s | Location: %s | YOE: %s yrs (stretch +%s)",
                keywords, constants.SEARCH_LOCATION, constants.YOUR_EXPERIENCE_YEARS,
                getattr(constants, "STRETCH_YEARS", 1))

    driver         = None
    new_jobs_count = 0
    all_jobs: Dict[str, dict] = {}   # job_id → job_dict (merged across all keywords)

    try:
        # ── Step 1: RSS pre-check (no Chrome needed) ─────────────────────────
        for keyword in keywords:
            rss_jobs = try_rss_fetch(keyword)
            for job in rss_jobs:
                if job["id"] not in all_jobs:
                    all_jobs[job["id"]] = job

        # ── Step 2: Selenium scraping (multi-keyword + pagination) ────────────
        driver = launch_chrome(headless=headless)

        for keyword in keywords:
            scrape_keyword(driver, keyword, all_jobs)
            time.sleep(randint(1000, 2000) / 1000.0)

        logger.info("Total unique job cards across all keywords: %d", len(all_jobs))

        if not all_jobs:
            logger.warning("No jobs found at all — LinkedIn may have changed markup.")
            notifier.send_status(
                "**LinkedIn Jobs Monitor:** No job cards found this run. "
                "LinkedIn may have updated their page. Check the log.",
                warn=True,
            )
            return 0

        # ── Step 3: Company blocklist ─────────────────────────────────────────
        before = len(all_jobs)
        all_jobs = {
            jid: job for jid, job in all_jobs.items()
            if not _company_is_blocked(job.get("company", ""))
        }
        blocked = before - len(all_jobs)
        if blocked:
            logger.info("Blocked %d jobs from blocklisted companies.", blocked)

        # ── Step 4: Tag priority companies ────────────────────────────────────
        for job in all_jobs.values():
            if _company_is_priority(job.get("company", "")):
                job["is_priority"] = True

        # ── Step 5: Within-run duplicate detection ────────────────────────────
        tag_duplicates(list(all_jobs.values()))

        # ── Step 6: Deduplicate against seen_jobs store FIRST ─────────────────
        # This MUST happen before the experience filter to avoid:
        #   a) Wasting deep-scan time on already-seen jobs
        #   b) Re-notifying about the same job every run
        all_job_list = [j for j in all_jobs.values() if j.get("id")]
        all_ids      = [j["id"] for j in all_job_list]
        new_ids      = set(job_store.filter_new(all_ids, jobs=all_job_list))
        unseen       = {jid: job for jid, job in all_jobs.items() if jid in new_ids}

        logger.info("After dedup: %d / %d are new (not seen before).",
                    len(unseen), len(all_jobs))

        if not unseen:
            logger.info("No new jobs this run — all were previously seen.")
            # Refresh dashboard (prunes expired entries without adding unfiltered jobs)
            try:
                write_dashboard_jobs([])
            except Exception as e:
                logger.warning("Dashboard export failed: %s", e)
            job_store.cleanup()
            logger.info("Store size after cleanup: %d IDs.", job_store.count())
            return 0

        # ── Step 7: Experience filter (only on new/unseen jobs) ──────────────
        eligible = []
        for job in unseen.values():
            if meets_experience_requirement(job, driver if constants.DEEP_SCAN else None):
                eligible.append(job)

        logger.info("After experience filter: %d / %d eligible (of %d new).",
                    len(eligible), len(unseen), len(all_jobs))

        # ── Step 7b: Write dashboard JSON ─────────────────────────────────────
        # Dashboard gets ALL eligible (new + already-seen eligible from prior runs)
        try:
            write_dashboard_jobs(eligible)
        except Exception as e:
            logger.warning("Dashboard export failed: %s", e)

        # ── Step 8: Notify eligible new jobs ──────────────────────────────────
        if eligible:
            # Sort: priority companies first, then by YOE descending
            eligible.sort(key=lambda j: (not j["is_priority"], -(j["yoe_found"] or 0)))
            notifier.send_new_jobs(eligible)

            if constants.ENABLE_MACOS_NOTIFICATION:
                notifier.notify_macos(
                    "LinkedIn Jobs Monitor",
                    f"{len(eligible)} new DevOps job(s) found!",
                )

        # ── Step 9: Mark ALL scraped job IDs as seen ──────────────────────────
        # Mark ALL new IDs (not just eligible ones) so that:
        #   - Ineligible jobs (too senior, fresher, etc.) are never re-processed
        #   - Eligible jobs are never re-notified
        # This prevents "same job repeating every day" for jobs that keep
        # appearing in the 24h search window (re-posted or still active).
        # Pass job dicts for fingerprint-based dedup on future runs.
        new_id_list  = list(new_ids)
        new_job_list = [all_jobs[jid] for jid in new_id_list if jid in all_jobs]
        job_store.mark_seen_batch(new_id_list, jobs=new_job_list)
        new_jobs_count = len(eligible)

        if not eligible:
            logger.info("No eligible new jobs this run.")

        # ── Cleanup ───────────────────────────────────────────────────────────
        job_store.cleanup()
        logger.info("Store size after cleanup: %d IDs.", job_store.count())

    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        try:
            notifier.send_status(f"**LinkedIn Jobs Monitor error:** {e}", warn=True)
        except Exception:
            pass

    finally:
        if driver is not None:
            try:
                driver.quit()
                logger.info("Chrome closed.")
            except Exception:
                pass

    logger.info("Run End — %d new job(s). ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n",
                new_jobs_count)
    return new_jobs_count


# ═══════════════════════════════════════════════════════════════════════════════
# SCHEDULER / TEST / ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def run_scheduler():
    if schedule_lib is None:
        logger.error("'schedule' package not installed. Run: pip install schedule")
        sys.exit(1)
    interval = constants.REFRESH_INTERVAL_MINUTES
    logger.info("Scheduler started — every %d min. Ctrl+C to stop.", interval)
    run_once(headless=constants.HEADLESS)
    schedule_lib.every(interval).minutes.do(run_once, headless=constants.HEADLESS)
    while True:
        schedule_lib.run_pending()
        time.sleep(30)


def run_test():
    """Send 2 test embeds to Discord to verify format and links."""
    logger.info("Sending test Discord messages...")
    notifier.send_new_jobs([
        {
            "title":        "Senior DevOps Engineer (TEST — click title)",
            "company":      "Wipro",
            "location":     "Chennai, Tamil Nadu, India",
            "posted_time":  "19 hours ago",
            "url":          "https://in.linkedin.com/jobs/view/devops-engineer-l3-at-wipro-4481511083",
            "yoe_found":    3,
            "is_priority":  True,
            "is_duplicate": False,
        },
        {
            "title":        "DevOps Lead — AWS/Kubernetes (TEST)",
            "company":      "Zepto",
            "location":     "Bengaluru, Karnataka, India",
            "posted_time":  "5 hours ago",
            "url":          "https://in.linkedin.com/jobs/view/gcp-devops-lead-at-infosys-4471395223",
            "yoe_found":    None,
            "is_priority":  False,
            "is_duplicate": False,
        },
    ])
    print("\n✅ Test sent! Check Discord.")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--test" in args:
        run_test()
    elif "--schedule" in args:
        run_scheduler()
    else:
        run_once(headless="--manual" not in args)
