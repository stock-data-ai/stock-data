"""台股交易日判斷 — 以 TWSE 休市日 OpenAPI 為準。

用於「交易日卻抓不到當日資料 → 視為 STALE 失敗」的檢查：
週六日必為非交易日；平日再對照 TWSE 公告的休市日清單。
API 失敗時保守視為交易日（寧可誤報紅燈，也不要漏報假成功）。
"""
import logging
from datetime import date, datetime
from typing import Optional, Set

import requests

logger = logging.getLogger(__name__)

HOLIDAY_API = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"

_closed_dates_cache: Optional[Set[str]] = None


def _roc_to_iso(roc: str) -> str:
    """'1150101' → '2026-01-01'"""
    year = int(roc[:3]) + 1911
    return f"{year}-{roc[3:5]}-{roc[5:7]}"


def _fetch_closed_dates() -> Optional[Set[str]]:
    """抓取 TWSE 休市日清單（ISO 日期集合）。清單混有「開始交易日」等說明性條目，需過濾。"""
    resp = requests.get(HOLIDAY_API, timeout=20)
    resp.raise_for_status()
    closed: Set[str] = set()
    for entry in resp.json():
        name = entry.get("Name", "")
        desc = entry.get("Description", "")
        if "市場無交易" in name or "放假" in desc or "補假" in desc:
            closed.add(_roc_to_iso(entry["Date"]))
    return closed


def is_tw_trading_day(d: date) -> bool:
    global _closed_dates_cache
    if d.weekday() >= 5:
        return False
    if _closed_dates_cache is None:
        try:
            _closed_dates_cache = _fetch_closed_dates()
        except Exception as e:
            logger.warning(f"無法取得 TWSE 休市日清單（{e}），保守視為交易日")
            return True
    # 休市日 API 只涵蓋當年度；跨年度日期若不在清單中，平日一律視為交易日
    return d.isoformat() not in _closed_dates_cache


def parse_yyyymmdd(date_str: str) -> date:
    """'20260712' 或 '2026-07-12' → date"""
    return datetime.strptime(date_str.replace("-", ""), "%Y%m%d").date()
