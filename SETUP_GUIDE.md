# LinkedIn DevOps Jobs Monitor — Setup & Usage Guide

> **What this does:** Automatically monitors LinkedIn for new DevOps jobs posted in the last 24 hours, filters for roles requiring 3+ years of experience, and sends you a Telegram notification with the job title, company, and direct link — every 30 minutes.

---

## Table of Contents
1. [How It Works](#1-how-it-works)
2. [Project Structure](#2-project-structure)
3. [Configuration (constants.py)](#3-configuration-constantspy)
4. [First-Time Setup](#4-first-time-setup)
5. [Scheduler — Daily Commands](#5-scheduler--daily-commands)
6. [Checking Logs](#6-checking-logs)
7. [Changing What to Search For](#7-changing-what-to-search-for)
8. [Troubleshooting](#8-troubleshooting)
9. [Running Manually](#9-running-manually)
10. [Security Reminders](#10-security-reminders)

---

## 1. How It Works

```
launchd (every 30 min)
    │
    └──► linkedin_jobs.py
              │
              ├── Launch headless Chrome (no login needed)
              ├── Open: linkedin.com/jobs/search/?keywords=DevOps&f_TPR=r86400
              ├── Scroll page to load all job cards
              ├── Parse job cards (title, company, location, link)
              ├── Filter: keep only jobs with 3+ yrs experience in title
              ├── Check against data/seen_jobs.json (deduplication)
              │
              ├── [New jobs found?] ──► Send Telegram notification
              │                           with job title + clickable link
              │
              ├── Update data/seen_jobs.json (mark as seen)
              └── Cleanup entries older than 48 hours
```

**Key facts:**
- ✅ No LinkedIn account or login required (uses public job search)
- ✅ No risk of LinkedIn account ban
- ✅ Telegram notification has the direct LinkedIn job link (tap to open)
- ✅ Won't notify you twice for the same job (JSON deduplication)

---

## 2. Project Structure

```
/Users/rvaranas/PROJECTS/naukri-A/LinkedInJobs/
│
├── linkedin_jobs.py               ← Main automation script
├── constants.py                   ← ✏️  Your config (search terms, Telegram, etc.)
├── notifier.py                    ← Telegram notification helper
├── job_store.py                   ← Seen-jobs JSON tracker
├── requirements.txt               ← Python dependencies
├── com.linkedin.jobmonitor.plist  ← macOS launchd scheduler config
├── SETUP_GUIDE.md                 ← This file
│
├── data/
│   └── seen_jobs.json             ← Auto-managed state (don't edit manually)
│
├── linkedin_jobs.log              ← Main log (appended every run)
├── linkedin_jobs_launchd.log      ← stdout from launchd
└── linkedin_jobs_launchd_error.log← stderr from launchd
```

---

## 3. Configuration (`constants.py`)

| Setting | Default | What it does |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | your bot token | Telegram bot authentication |
| `TELEGRAM_CHAT_ID` | your chat ID | Where notifications are sent |
| `SEARCH_KEYWORDS` | `"DevOps Engineer"` | LinkedIn search query |
| `SEARCH_LOCATION` | `"India"` | Location filter |
| `MIN_EXPERIENCE_YEARS` | `3` | Minimum years to alert on |
| `DATE_POSTED_FILTER` | `"r86400"` | Last 24 hours (`r604800` = week) |
| `DEEP_SCAN` | `False` | If True, reads full job description (slower but more accurate) |
| `REFRESH_INTERVAL_MINUTES` | `30` | Used in `--schedule` daemon mode |
| `SEEN_JOBS_TTL_HOURS` | `48` | How long to remember seen jobs |
| `HEADLESS` | `True` | Chrome runs invisibly |
| `MAX_JOBS_PER_RUN` | `50` | Max jobs to process per run |
| `ENABLE_MACOS_NOTIFICATION` | `False` | Also show macOS desktop popup |

> ⚠️ **After editing constants.py**, no need to reload launchd — changes take effect on the next run automatically.

---

## 4. First-Time Setup

> ✅ Already done during installation. Keeping this for reference if you set up on a new Mac.

```bash
# 1. Enter project directory
cd /Users/rvaranas/PROJECTS/naukri-A/LinkedInJobs

# 2. Create virtual environment and install dependencies
python3 -m venv .venv
.venv/bin/pip install selenium requests schedule

# 3. Initialize seen-jobs store
mkdir -p data && echo '{}' > data/seen_jobs.json

# 4. Test Telegram connection
.venv/bin/python linkedin_jobs.py --test

# 5. Copy launchd plist to LaunchAgents
cp com.linkedin.jobmonitor.plist ~/Library/LaunchAgents/

# 6. Load the scheduler
launchctl load ~/Library/LaunchAgents/com.linkedin.jobmonitor.plist

# 7. Verify it's active
launchctl list | grep linkedin
# Expected: -   0   com.linkedin.jobmonitor
```

---

## 5. Scheduler — Daily Commands

### Check if the scheduler is active
```bash
launchctl list | grep linkedin
```
Output: `- 0 com.linkedin.jobmonitor` → ✅ Running every 30 minutes
Output: nothing → ❌ Not loaded

### Stop the scheduler (pause monitoring)
```bash
launchctl unload ~/Library/LaunchAgents/com.linkedin.jobmonitor.plist
```

### Start / restart the scheduler
```bash
launchctl load ~/Library/LaunchAgents/com.linkedin.jobmonitor.plist
```

### Permanently disable
```bash
launchctl unload ~/Library/LaunchAgents/com.linkedin.jobmonitor.plist
rm ~/Library/LaunchAgents/com.linkedin.jobmonitor.plist
```

### Change interval (e.g., to hourly)
Edit `com.linkedin.jobmonitor.plist` → change `<integer>1800</integer>` to `<integer>3600</integer>`
Then re-copy and reload:
```bash
cp com.linkedin.jobmonitor.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.linkedin.jobmonitor.plist
launchctl load ~/Library/LaunchAgents/com.linkedin.jobmonitor.plist
```

---

## 6. Checking Logs

### Quick check — last 20 lines
```bash
tail -20 /Users/rvaranas/PROJECTS/naukri-A/LinkedInJobs/linkedin_jobs.log
```

### Watch live (while running manually)
```bash
tail -f /Users/rvaranas/PROJECTS/naukri-A/LinkedInJobs/linkedin_jobs.log
```

### Check for errors
```bash
cat /Users/rvaranas/PROJECTS/naukri-A/LinkedInJobs/linkedin_jobs_launchd_error.log
```

### What a successful run looks like
```
2026-10-21 09:30:01  INFO  ━━━ LinkedIn Jobs Monitor — Run Start ━━━
2026-10-21 09:30:01  INFO  Keywords: DevOps Engineer | Location: India | Filter: last 24h
2026-10-21 09:30:02  INFO  User-agent: Mozilla/5.0 (Macintosh...
2026-10-21 09:30:02  INFO  Chrome launched (headless=True)
2026-10-21 09:30:05  INFO  Job list loaded (selector: ul.jobs-search__results-list)
2026-10-21 09:30:08  INFO  Found 24 job cards via selector '...'
2026-10-21 09:30:08  INFO  Parsed 24 jobs from page.
2026-10-21 09:30:08  INFO  After experience filter: 11 / 24 jobs eligible
2026-10-21 09:30:08  INFO  New (not seen before): 3 jobs
2026-10-21 09:30:09  INFO  Telegram notification sent OK.
2026-10-21 09:30:09  INFO  Store now contains 47 job IDs.
2026-10-21 09:30:09  INFO  Chrome closed.
2026-10-21 09:30:09  INFO  ━━━ Run End — 3 new job(s) found ━━━
```

---

## 7. Changing What to Search For

Edit `constants.py`:

```python
# Add SRE roles too
SEARCH_KEYWORDS = "DevOps OR SRE OR Platform Engineer"

# Widen to 2+ years
MIN_EXPERIENCE_YEARS = 2

# Check past week instead of 24 hours
DATE_POSTED_FILTER = "r604800"

# Check every hour instead of every 30 min (also update the plist)
REFRESH_INTERVAL_MINUTES = 60
```

---

## 8. Troubleshooting

### Problem: No jobs found / "No job cards found" in log
LinkedIn may have updated their page structure (CSS selectors change occasionally).
```bash
# Run with visible Chrome to see what's happening
cd /Users/rvaranas/PROJECTS/naukri-A/LinkedInJobs
.venv/bin/python linkedin_jobs.py --manual
```

### Problem: Telegram notification not received
```bash
# Test the Telegram connection
.venv/bin/python linkedin_jobs.py --test
```
If it fails:
- Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `constants.py`
- Make sure you sent `/start` or any message to the bot first

### Problem: Script ran but Chrome driver error
```bash
.venv/bin/pip install --upgrade selenium
```

### Problem: Scheduler not running
```bash
# Reload
launchctl unload ~/Library/LaunchAgents/com.linkedin.jobmonitor.plist
launchctl load ~/Library/LaunchAgents/com.linkedin.jobmonitor.plist

# Check for errors
cat linkedin_jobs_launchd_error.log
```

### Problem: Getting too many notifications (every run notifying old jobs)
The `data/seen_jobs.json` file may have been deleted. The TTL cleanup will settle down within 2 runs.

---

## 9. Running Manually

```bash
cd /Users/rvaranas/PROJECTS/naukri-A/LinkedInJobs
source .venv/bin/activate

# One-off silent run (headless)
python linkedin_jobs.py

# One-off with visible Chrome window (for debugging)
python linkedin_jobs.py --manual

# Test Telegram only (no scraping)
python linkedin_jobs.py --test

# Daemon mode (runs every 30 min, keeps running until Ctrl+C)
python linkedin_jobs.py --schedule
```

---

## 10. Security Reminders

> ⚠️ `constants.py` contains your **Telegram bot token** — treat it like a password.

- **Never commit `constants.py` to GitHub** — it's already in `.gitignore`
- If you accidentally expose the token: go to [@BotFather](https://t.me/BotFather) → `/revoke` → generate a new token → update `constants.py`
- The bot can **only send messages to your chat ID** — even if someone gets the token, they can't read your messages

---

*Created: October 2026*
