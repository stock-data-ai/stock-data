import os
from pathlib import Path

# --- Paths ---
# Use absolute path relative to the project root if running from root,
# or handle relative paths carefully. 
# Based on current usage: "src/data/layer3"
BASE_DIR = Path("src/data/layer3")
MARKET_DATA_DIR = Path("src/data/market")

# Subdirectories
COMPANY_FINANCIALS_DIR = BASE_DIR / "company-financials"
COMPANIES_DIR = BASE_DIR / "companies"
COMPANY_TOPICS_DIR = BASE_DIR / "company-topics"
JP_COMPANY_DIR = BASE_DIR / "company-jp"
US_COMPANY_DIR = BASE_DIR / "company-us"

# Files
COMPANIES_ALL_FILE = COMPANIES_DIR / "companies-all.json"
COMPANY_TOPICS_INDEX_FILE = COMPANY_TOPICS_DIR / "index.json"

# --- API Settings ---
FINMIND_API_URL = "https://api.web.finmindtrade.com/v2/user_info"
API_EXHAUSTION_THRESHOLD = 0.9
TOKEN_RESET_INTERVAL_SECONDS = 3600
# Connection retry settings (ADR-007 preparation)
MAX_RETRIES = 3
RETRY_BACKOFF_FACTOR = 1

# --- Data Fetching ---
DEFAULT_SLEEP_RANGE = (1, 3)
FULL_UPDATE_DAYS = 365  # 抓近1年；歷史資料已存 JSON，MERGE 邏輯保留舊記錄
REVENUE_DAYS = 365
DEFAULT_FETCH_DAYS = 90

# --- Rerun / Batch Settings ---
MAX_RERUN_ROUNDS = 4
RERUN_DELAY_MINUTES = 65
RERUN_DIR = Path("finance_tools")
