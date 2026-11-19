import json
import os

# Try to load local secrets file if present (untracked by Git)
try:
    from constants_local import DISCORD_WEBHOOK_URL as _LOCAL_WEBHOOK
except ImportError:
    _LOCAL_WEBHOOK = ""

# ── User Config (overrides from user_config.json — editable via dashboard) ─────
_user_config = {}
try:
    _config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_config.json")
    with open(_config_path, "r", encoding="utf-8") as _f:
        _user_config = json.load(_f)
except (FileNotFoundError, json.JSONDecodeError, OSError):
    pass  # No user config — use defaults below

# ── Discord ────────────────────────────────────────────────────────────────────
# Disabled — using web dashboard instead. Set to True to re-enable.
ENABLE_DISCORD = _user_config.get("enable_discord", False)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL") or _LOCAL_WEBHOOK or ""


# ── LinkedIn Search ────────────────────────────────────────────────────────────
# Multiple keywords — each is searched separately, results merged by job ID.
# This catches roles posted as "SRE", "Platform Engineer" etc. that don't
# appear under a plain "DevOps" search.
SEARCH_KEYWORDS_LIST = _user_config.get("search_keywords", [
    "DevOps Engineer",
    "SRE",
    "Platform Engineer",
    "Cloud Engineer",
    "Infrastructure Engineer",
])

SEARCH_LOCATION   = _user_config.get("search_location", "India")
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
# Smart eligibility matching for YOUR experience level.
#
# Instead of a dumb min/max number comparison, the algorithm asks:
# "Does MY experience fit what THIS JOB is looking for?"
#
# With YOUR_EXPERIENCE_YEARS = 3 and STRETCH_YEARS = 1:
#
#   Job says "1+ years"     → I have 3 ≥ 1     → ✅ I exceed their minimum
#   Job says "2-4 years"    → 3 is in [2, 4]   → ✅ I'm right in the range
#   Job says "3-5 years"    → 3 is in [3, 5]   → ✅ I'm at the bottom of range
#   Job says "4+ years"     → I have 3+1 ≥ 4   → ✅ Within stretch — companies flex
#   Job says "5+ years"     → I have 3+1 < 5   → ❌ Too senior for me
#   Job says "0-1 years"    → max ≤ 1 = fresher→ ❌ Fresher role, I'm overqualified
#   Job says "5-8 years"    → 3+1 < 5          → ❌ I can't reach their minimum
#   No YOE found            →                  → ✅ INCLUDE (unless title says intern/senior/director)
#
YOUR_EXPERIENCE_YEARS = _user_config.get("your_experience_years", 3)
STRETCH_YEARS         = _user_config.get("stretch_years", 1)

# ── Deep Scan ─────────────────────────────────────────────────────────────────
DEEP_SCAN = True

# ── External Portal Scan ──────────────────────────────────────────────────────
# When LinkedIn description has no YOE, click Apply and scan the external
# company job portal for experience requirements.
EXTERNAL_PORTAL_SCAN = True
EXTERNAL_PORTAL_TIMEOUT = 6   # seconds to wait for external portal page to load

# ── Skip Keywords (title only — very obvious fresher-only roles) ───────────────
# ONLY skips if title contains these AND description also has no 3+ YOE mention.
SKIP_TITLE_KEYWORDS = [
    "fresher", "freshers", "fresh graduate",
    "0-1 year", "0 to 1 year",
    "internship", "intern",
    "trainee", "apprentice",
    "junior", "entry level", "entry-level",
]

# ── Company Blocklist ──────────────────────────────────────────────────────────
# Jobs from these companies are silently skipped — no deep scan, no notification.
# Add full or partial company name (case-insensitive match).
COMPANY_BLOCKLIST = [
    # Add companies you'd never join. Examples:
    # "Some Company", "ABC Consultants",
]

# ── Priority Companies ─────────────────────────────────────────────────────────
# Jobs from these companies get a ★ PRIORITY badge in the dashboard.
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
    # ─── Indian Startups / Tech Companies ────────────────────────────────────
    "Juspay", "Razorpay", "PhonePe", "Zepto", "Swiggy", "Zomato",
    "Flipkart", "Meesho", "Paytm", "CRED", "Groww", "Ola",
    # ─── Global Tech MNCs (strong India presence, genuine hiring) ─────────────
    "Microsoft", "Google", "Amazon", "Apple", "Meta",
    "IBM", "Oracle", "SAP", "Salesforce", "Adobe",
    "VMware", "Broadcom", "Cisco", "Intel", "Qualcomm",
    "NVIDIA", "Dell", "HP", "HPE", "Hewlett Packard",
    "NetApp", "Juniper", "Akamai", "Palo Alto",
    # ─── Cloud / SaaS / DevOps-centric companies ─────────────────────────────
    "Red Hat", "Atlassian", "GitLab", "GitHub", "HashiCorp",
    "Datadog", "Elastic", "Confluent", "MongoDB", "Cloudflare",
    "Grafana", "New Relic", "PagerDuty", "Splunk",
    "Snowflake", "Databricks", "Palantir", "Twilio",
    "Stripe", "Square", "Block",
    # ─── Financial / Banking MNCs (large India tech teams) ────────────────────
    "Goldman Sachs", "Morgan Stanley", "JP Morgan", "JPMorgan",
    "Deutsche Bank", "Barclays", "HSBC", "Citi", "Citibank",
    "BNY Mellon", "BNY", "UBS", "Credit Suisse",
    "Wells Fargo", "Bank of America", "Nomura",
    "Visa", "Mastercard", "PayPal", "American Express",
    # ─── Consulting / Professional Services ───────────────────────────────────
    "Deloitte", "Accenture", "Capgemini", "Cognizant", "CGI",
    "DXC", "Atos", "NTT Data", "Fujitsu",
    # ─── Product / Other MNCs with India engineering ──────────────────────────
    "Walmart", "Target", "Intuit", "ServiceNow", "Workday",
    "Uber", "Grab", "Booking", "Expedia",
    "Samsung", "Sony", "Siemens", "Bosch",
    "ThoughtWorks", "Thoughtworks",
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
USE_RSS_FALLBACK = False   # LinkedIn deprecated RSS — this never returns data, skip it

# ── Data / State ───────────────────────────────────────────────────────────────
SEEN_JOBS_FILE         = "data/seen_jobs.json"
SEEN_JOBS_TTL_HOURS    = 720  # 30 days retention (prevents reposted/promoted active jobs from repeating every 2 days)
DASHBOARD_JOBS_FILE    = "data/dashboard_jobs.json"

# ── Browser ────────────────────────────────────────────────────────────────────
HEADLESS = True
MAX_JOBS_PER_RUN = 100   # max cards to process per keyword per page

# ── Scheduler (daemon mode) ────────────────────────────────────────────────────
REFRESH_INTERVAL_MINUTES = 30

# ── macOS Desktop Notifications (optional) ────────────────────────────────────
ENABLE_MACOS_NOTIFICATION = False
