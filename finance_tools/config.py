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
FINMIND_DATA_URL = "https://api.finmindtrade.com/api/v4/data"
API_EXHAUSTION_THRESHOLD = 0.9
TOKEN_RESET_INTERVAL_SECONDS = 3600
# Connection retry settings (ADR-007 preparation)
MAX_RETRIES = 3
RETRY_BACKOFF_FACTOR = 1

# --- Data Fetching ---
# 財報類資料一律「每一期抓一次全市場」（見 FinMindClient.fetch_market_day），每次都抓完整視窗，
# 不再分「平常一年／歷史不夠時十年」兩套。FinMind 配額按**請求次數**計
# （`user_count`／`api_request_limit`，Sponsor Pro 每小時 20,000 次），完整視窗一次約兩百多次請求。
# 舊版 `history_is_short` 那套放寬視窗的判準（季度 < 8、月營收 < 24、缺稅率欄位）一併移除：
# 每次都抓完整視窗，新公司、壞檔、新加的欄位都在下一輪自然補齊，不需要另外判斷。
#
# 年數都從「起始年的 1 月」算起，視窗前緣一定是 Q1／1 月：現金流量表是累計制，
# 單季要靠「本季累計－上一季累計」，前緣若落在 Q2 就拆不出來，會寫出 None 蓋掉已存好的值。
INCOME_STATEMENT_YEARS = 10   # 損益表：今年＋前 10 年
BALANCE_SHEET_YEARS = 7       # 資產負債表＋現金流量表：今年＋前 7 年（ROE／ROA／OCF 年度表格）
MONTHLY_REVENUE_YEARS = 7     # 月營收：今年＋前 7 年；保留 72 筆（MONTHLY_REVENUE_LIMIT），多一年給年增率
MARKET_FETCH_WORKERS = 4      # 同時發出的全市場請求數
MARKET_FETCH_TIMEOUT_SECONDS = 120  # 資產負債表一季約 28 MB
DEFAULT_FETCH_DAYS = 90

# --- Rerun / Batch Settings ---
MAX_RERUN_ROUNDS = 4
RERUN_DELAY_MINUTES = 65
RERUN_DIR = Path("finance_tools")
