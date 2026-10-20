import os

# Try to load local secrets file if present (untracked by Git)
try:
    from constants_local import DISCORD_WEBHOOK_URL as _LOCAL_WEBHOOK
except ImportError:
    _LOCAL_WEBHOOK = ""

# ── Discord ────────────────────────────────────────────────────────────────────
# Reads from environment variable when running in GitHub Actions (CI/CD).
# Falls back to local secrets file for local runs.
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL") or _LOCAL_WEBHOOK or ""


# ── LinkedIn Search ────────────────────────────────────────────────────────────
# Multiple keywords — each is searched separately, results merged by job ID.
# This catches roles posted as "SRE", "Platform Engineer" etc. that don't
# appear under a plain "DevOps" search.
SEARCH_KEYWORDS_LIST = [
    "DevOps Engineer",
    "SRE",
    "Platform Engineer",
    "Cloud Engineer",
    "Infrastructure Engineer",
]

SEARCH_LOCATION   = "India"
DATE_POSTED_FILTER = "r86400"   # last 24 hours (matches browser extension)

# Max result pages to scrape per keyword (LinkedIn shows ~25 jobs/page).
# 4 pages × 5 keywords = up to 500 job cards checked per run.
MAX_PAGES = 4

# LinkedIn URL template — {KEYWORD} and {START} are filled at runtime.
# f_JT=F → Full-time only | sortBy=DD → newest first
LINKEDIN_URL_TEMPLATE = (
    "https://www.linkedin.com/jobs/search/"
    "?f_JT=F&f_TPR={DATE_FILTER}&keywords=%22{KEYWORD}%22"
    "&location={LOCATION}&sortBy=DD&start={START}"
)

# ── Experience Filtering ───────────────────────────────────────────────────────
# A job is sent if:
#   - Description explicitly mentions >= MIN_EXPERIENCE_YEARS  → NOTIFY
#   - Description mentions no YOE at all                       → NOTIFY
#   - Description explicitly mentions < MIN_EXPERIENCE_YEARS   → SKIP
MIN_EXPERIENCE_YEARS = 2

# ── Deep Scan ─────────────────────────────────────────────────────────────────
DEEP_SCAN = True

# ── Skip Keywords (title only — very obvious fresher-only roles) ───────────────
# ONLY skips if title contains these AND description also has no 2+ YOE mention.
SKIP_TITLE_KEYWORDS = [
    "fresher", "freshers", "fresh graduate",
    "0-1 year", "0 to 1 year",
    "internship", "intern ",
    "trainee", "apprentice",
]

# ── Company Blocklist ──────────────────────────────────────────────────────────
# Jobs from these companies are silently skipped — no deep scan, no notification.
# Add full or partial company name (case-insensitive match).
COMPANY_BLOCKLIST = [
    # Add companies you'd never join. Examples:
    # "Some Company", "ABC Consultants",
]

# ── Priority Companies (Indian IT MNCs) ────────────────────────────────────────
# Jobs from these companies get a ★ PRIORITY badge in Discord.
# Partial match — "TCS" will match "TCS Limited", "Tata Consultancy Services", etc.
PRIORITY_COMPANIES = [
    # ─── Tier 1 Indian IT Giants ──────────────────────────────────────────────
    "TCS", "Tata Consultancy", "Infosys", "Wipro", "HCL",
    "Tech Mahindra", "LTIMindtree", "L&T Infotech", "Mindtree",
    # ─── Tier 2 Indian IT MNCs ────────────────────────────────────────────────
    "Mphasis", "Hexaware", "Persistent", "Coforge", "Birlasoft",
    "Zensar", "Cyient", "Sonata", "KPIT", "Happiest Minds",
    "Sasken", "Tata Elxsi", "Mastech", "NIIT Tech",
    "L&T Technology", "LTTS", "Tata Technologies",
    # ─── Other large Indian tech companies ───────────────────────────────────
    "Juspay", "Razorpay", "PhonePe", "Zepto", "Swiggy", "Zomato",
    "Flipkart", "Meesho", "Paytm", "CRED", "Groww", "Ola",
]

# ── Proxy Rotation (optional) ─────────────────────────────────────────────────
# Leave empty for no proxy (default). Add proxy strings to rotate IPs and
# avoid LinkedIn rate-limiting. Each Chrome launch picks a random proxy.
# Format: "http://ip:port" or "http://user:password@ip:port"
# Note: You need your own proxies (e.g., Webshare.io free tier = 10 proxies).
PROXY_LIST = [
    # "http://123.45.67.89:8080",
    # "http://user:pass@proxy2.example.com:3128",
]

# ── RSS Fallback ───────────────────────────────────────────────────────────────
# Attempt a lightweight RSS/JSON fetch before launching Chrome.
# If LinkedIn returns valid data, Selenium is skipped for that keyword (faster).
# LinkedIn has deprecated public RSS, so this often fails — that's fine,
# Selenium kicks in as the real scraper.
USE_RSS_FALLBACK = True

# ── Data / State ───────────────────────────────────────────────────────────────
SEEN_JOBS_FILE    = "data/seen_jobs.json"
SEEN_JOBS_TTL_HOURS = 48

# ── Browser ────────────────────────────────────────────────────────────────────
HEADLESS = True
MAX_JOBS_PER_RUN = 100   # max cards to process per keyword per page

# ── Scheduler (daemon mode) ────────────────────────────────────────────────────
REFRESH_INTERVAL_MINUTES = 30

# ── macOS Desktop Notifications (optional) ────────────────────────────────────
ENABLE_MACOS_NOTIFICATION = False
