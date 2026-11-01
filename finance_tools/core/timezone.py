"""台灣時區工具 — 確保 CI (UTC) 與本地開發產出一致的日期"""

from datetime import datetime
from zoneinfo import ZoneInfo

TW_TZ = ZoneInfo("Asia/Taipei")


def now_tw() -> datetime:
    """回傳台灣時區的當前時間"""
    return datetime.now(TW_TZ)


def today_str() -> str:
    """回傳台灣時區的今日日期字串 (YYYY-MM-DD)"""
    return now_tw().strftime("%Y-%m-%d")