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
# 檔裡的歷史「不夠」時改用這個視窗，讓歷史一次長回來（見下方兩個門檻）。
# FinMind 配額按**請求次數**計（`user_count`／`api_request_limit`），抓十年跟抓一年
# 都是一次請求；平常只抓一年，是因為舊的已經存在檔裡，不必每週重抓。
FULL_HISTORY_DAYS = 3650
# 「歷史不夠」的判準：有營收的季度少於 8 季，或月營收少於 24 個月。
# 一年視窗最多只帶回 5 季／13 個月，所以門檻必須高於這個數字——
# 否則「只抓過一年」的檔永遠被當成夠了（日更先建的空殼、壞檔重建失敗後的空殼都是這樣）。
MIN_QUARTERS_FOR_NORMAL_WINDOW = 8
MIN_MONTHS_FOR_NORMAL_WINDOW = 24
REVENUE_DAYS = 365
DEFAULT_FETCH_DAYS = 90

# --- Rerun / Batch Settings ---
MAX_RERUN_ROUNDS = 4
RERUN_DELAY_MINUTES = 65
RERUN_DIR = Path("finance_tools")
