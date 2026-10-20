#! python3
# -*- coding: utf-8 -*-
"""
linkedin_jobs.py — LinkedIn DevOps Jobs Monitor

Scrapes LinkedIn's public job search for DevOps (and related) jobs posted
in the last 24 hours, filters by experience level, deduplicates, and sends
Discord notifications for every new match.

New in this version:
  - Multi-keyword search (DevOps, SRE, Platform Engineer, ...)
  - Pagination — scrapes multiple result pages per keyword
  - RSS lightweight pre-check (falls back to Selenium if fails)
  - Company blocklist — silently skip unwanted companies
  - Priority company badge — ★ for Indian IT MNCs
  - Within-run duplicate detection
  - Log rotation (5 MB max, 5 backups)
  - Proxy rotation support

YOE filter fix:
  - "1-3 years" now extracts 1 (the MINIMUM of the range), not 3.
    Jobs requiring min < 2 years are correctly skipped.

Usage:
    python linkedin_jobs.py            # headless run (used by launchd / GitHub Actions)
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
from typing import Dict, List, Optional, Tuple

try:
    import feedparser          # RSS fallback
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
    import schedule as schedule_lib
except ImportError:
    schedule_lib = None

import constants
import job_store
import notifier

# ── Logging (with rotation) ───────────────────────────────────────────────────
_log_handler_file = logging.handlers.RotatingFileHandler(
    "linkedin_jobs.log",
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
_EXECUTIVE_LOWER  = [
    "principal", "director", "vp ", "vice president", "head of",
    "staff architect", "chief architect", "enterprise architect",
]
_SENIOR_KEYWORDS  = [
    "senior", "sr.", "sr ", "lead", "staff", "architect",
    "manager", "l3", "l4", "l5",
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


def _safe_attr(el, css: str, attr: str, default: str = "") -> str:
    try:
        found = el.find_elements(By.CSS_SELECTOR, css)
        if found:
            val = found[0].get_attribute(attr)
            if val:
                return val.strip()
        return default
    except Exception:
        return default


def _extract_job_id(url: str) -> str:
    m = re.search(r'/(\d{7,})', url)
    if m:
        return m.group(1)
    return url.split("?")[0].rstrip("/")


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

    url = ""
    for sel in ["a.base-card__full-link", "a[data-tracking-control-name]", "a"]:
        raw = _safe_attr(card, sel, "href")
        if raw and "linkedin.com/jobs" in raw:
            url = raw.split("?")[0]
            break

    if not title or not url:
        return None

    return {
        "id":          _extract_job_id(url),
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
    cl = company.lower()
    return any(p in cl for p in _PRIORITY_LOWER)


# ═══════════════════════════════════════════════════════════════════════════════
# WITHIN-RUN DUPLICATE DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation and common filler words for comparison."""
    t = title.lower()
    t = re.sub(r'[^a-z0-9\s]', ' ', t)
    stop = {"engineer", "senior", "lead", "and", "for", "the", "at", "in",
            "of", "with", "on", "a", "an", "to", "role", "position", "job", "india"}
    return " ".join(w for w in t.split() if w not in stop)


def tag_duplicates(jobs: List[dict]) -> None:
    """
    Mutates job dicts in-place to mark likely duplicates.
    Two jobs are considered duplicates if their normalised title + location
    combination has appeared before in this same run.
    """
    seen_sigs: set = set()
    for job in jobs:
        sig = (_normalize_title(job.get("title", "")), job.get("location", "").lower()[:20])
        if sig in seen_sigs:
            job["is_duplicate"] = True
            logger.info("Duplicate detected: %s @ %s", job.get("title"), job.get("location"))
        else:
            seen_sigs.add(sig)


# ═══════════════════════════════════════════════════════════════════════════════
# EXPERIENCE FILTERING  ← BUG FIX: now extracts MINIMUM of range, not maximum
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_min_years_required(text: str) -> Optional[float]:
    """
    Extract the MINIMUM years of experience the job REQUIRES.

    Supports decimals (e.g. 2.5 yrs, 1.5 yrs) and ranges (1-3 yrs -> 1).
    Excludes company age phrases (e.g. '15+ years in business', 'founded 20 years ago').

    Returns the minimum of all year values found (as int if whole number, else float).
    Returns None if no year mentions found.
    """
    num = r'(\d+(?:\.\d+)?)'
    found = []

    # 'X+ years' (excluding company age phrases like '15+ years in business')
    for m in re.finditer(
        rf'{num}\s*\+\s*(?:years?|yrs?)(?!\s+(?:in\s+business|old|ago|history))\b',
        text, re.IGNORECASE
    ):
        found.append(float(m.group(1)))

    # 'X-Y years' or 'X–Y years' -> take X (lower bound = minimum required)
    for m in re.finditer(rf'{num}\s*[-–]\s*\d+(?:\.\d+)?\s*(?:years?|yrs?)', text, re.IGNORECASE):
        found.append(float(m.group(1)))

    # 'X to Y years' -> take X
    for m in re.finditer(rf'{num}\s+to\s+\d+(?:\.\d+)?\s*(?:years?|yrs?)', text, re.IGNORECASE):
        found.append(float(m.group(1)))

    # 'minimum/min/at least X years'
    for m in re.finditer(
        rf'(?:minimum|min\.?|at\s+least)\s+{num}\s*(?:years?|yrs?)',
        text, re.IGNORECASE
    ):
        found.append(float(m.group(1)))

    # 'X years [of] experience' — negative lookbehind prevents matching upper bound of range
    for m in re.finditer(
        rf'(?<![-–\d.]){num}\s*(?:years?|yrs?)\s*(?:of\s+)?(?:relevant\s+)?(?:experience|exp)\b',
        text, re.IGNORECASE
    ):
        found.append(float(m.group(1)))

    # 'experience of X years' / 'experience: X years'
    for m in re.finditer(
        rf'(?:experience|exp)[:\s]+(?:of\s+)?{num}\s*\+?\s*(?:years?|yrs?)',
        text, re.IGNORECASE
    ):
        found.append(float(m.group(1)))

    # Parenthesized YOE in titles: '(2 yrs)', '(2.5 yrs)', '(3 years)', '(0 yrs)'
    for m in re.finditer(
        rf'\(\s*(?:min\s+)?{num}\s*\+?\s*(?:years?|yrs?)\s*\)',
        text, re.IGNORECASE
    ):
        found.append(float(m.group(1)))

    if not found:
        return None
    val = min(found)
    return int(val) if val.is_integer() else round(val, 1)


def _title_is_skip(title: str) -> bool:
    tl = title.lower()
    if re.search(r'\bintern(s|ship)?\b', tl):
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
    Decide whether this job should be notified.

    Tailored for ~2.5 years of experience:
      - Jobs requiring min 1+, 2+, 3+ years  → INCLUDE
      - Jobs requiring min 0 (fresher/intern) → SKIP (< min_yrs)
      - Jobs requiring min >= 4+ years       → SKIP (> max_yrs)
      - No YOE in description                → INCLUDE (unless intern/fresher or executive)
    """
    min_yrs = constants.MIN_EXPERIENCE_YEARS
    max_yrs = getattr(constants, "MAX_EXPERIENCE_YEARS", 3)
    title   = job.get("title", "")

    if constants.DEEP_SCAN and driver is not None:
        return _deep_scan_experience(driver, job, min_yrs, max_yrs)

    yrs_in_title = _extract_min_years_required(title)
    if yrs_in_title is not None:
        result = min_yrs <= yrs_in_title <= max_yrs
        if result:
            job["yoe_found"] = yrs_in_title
        logger.info("%s (title: %s yrs, range %s-%s): %s",
                    "PASS" if result else "SKIP", yrs_in_title, min_yrs, max_yrs, title)
        return result

    if _title_is_skip(title):
        logger.info("SKIP (fresher/intern title): %s", title)
        return False

    if _title_is_executive(title):
        logger.info("SKIP (executive title, no YOE): %s", title)
        return False

    logger.info("INCLUDE (no YOE info): %s", title)
    return True


def _deep_scan_experience(driver: webdriver.Chrome, job: dict, min_yrs: float, max_yrs: float) -> bool:
    """Open job page in new tab, extract description, check YOE."""
    title  = job.get("title", "")
    job_id = job.get("id", "")
    url    = job.get("url", "")

    view_url = f"https://www.linkedin.com/jobs/view/{job_id}/" if job_id.isdigit() else url

    try:
        orig = driver.current_window_handle
        driver.execute_script("window.open(arguments[0], '_blank');", view_url)
        driver.switch_to.window(driver.window_handles[-1])
        time.sleep(randint(1500, 2200) / 1000.0)

        if _is_auth_wall(driver):
            logger.info("DEEP_SCAN: Auth wall — INCLUDE: %s", title)
            driver.close()
            driver.switch_to.window(orig)
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

        # Extract description
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

        driver.close()
        driver.switch_to.window(orig)

        # Evaluate — minimum YOE must be within eligible range [min_yrs, max_yrs]
        if desc:
            yrs = _extract_min_years_required(desc)
            if yrs is not None:
                result = min_yrs <= yrs <= max_yrs
                if result:
                    job["yoe_found"] = yrs
                logger.info("DEEP_SCAN %s — %s yrs min (eligible range: %s-%s yrs): %s",
                            "PASS" if result else "SKIP", yrs, min_yrs, max_yrs, title)
                return result
            else:
                if _title_is_skip(title) or _title_is_executive(title):
                    logger.info("DEEP_SCAN SKIP (fresher/executive title, no YOE in desc): %s", title)
                    return False
                logger.info("DEEP_SCAN INCLUDE (no YOE in desc): %s", title)
                return True
        else:
            if _title_is_skip(title) or _title_is_executive(title):
                logger.info("DEEP_SCAN SKIP (fresher/executive title, couldn't read desc): %s", title)
                return False
            logger.warning("DEEP_SCAN: Couldn't read desc — INCLUDE: %s", title)
            return True

    except Exception as e:
        logger.warning("Deep scan error for '%s': %s", title, e)
        try:
            if len(driver.window_handles) > 1:
                driver.close()
            driver.switch_to.window(driver.window_handles[0])
        except Exception:
            pass
        return not (_title_is_skip(title) or _title_is_executive(title))


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
    logger.info("Run Start | Keywords: %s | Location: %s | YOE: %s-%s yrs",
                keywords, constants.SEARCH_LOCATION, constants.MIN_EXPERIENCE_YEARS, getattr(constants, "MAX_EXPERIENCE_YEARS", 3))

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

        # ── Step 6: Experience filter (deep scan) ─────────────────────────────
        eligible = []
        for job in all_jobs.values():
            if meets_experience_requirement(job, driver if constants.DEEP_SCAN else None):
                eligible.append(job)

        logger.info("After experience filter: %d / %d eligible.",
                    len(eligible), len(all_jobs))

        # ── Step 7: Deduplicate against store ─────────────────────────────────
        eligible_ids = [j["id"] for j in eligible]
        new_ids      = set(job_store.filter_new(eligible_ids))
        new_jobs     = [j for j in eligible if j["id"] in new_ids]

        # ── Step 8: Notify FIRST, then mark seen ─────────────────────────────
        if new_jobs:
            # Sort: priority companies first, then by YOE descending
            new_jobs.sort(key=lambda j: (not j["is_priority"], -(j["yoe_found"] or 0)))
            notifier.send_new_jobs(new_jobs)

            if constants.ENABLE_MACOS_NOTIFICATION:
                notifier.notify_macos(
                    "LinkedIn Jobs Monitor",
                    f"{len(new_jobs)} new DevOps job(s) found!",
                )

            job_store.mark_seen_batch(list(new_ids))
            new_jobs_count = len(new_jobs)
        else:
            logger.info("No new jobs this run.")

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
