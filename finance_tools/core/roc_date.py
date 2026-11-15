"""民國年 ↔ 西元——stock-data 的唯一實作。

stock_map 對應 scripts/lib/roc_date.py 與 src/shared/lib/roc-date.mjs；三份由 stock_map 的
test_methods/market-events/roc-date.test.mjs 用同一組案例對帳，改規則要三份一起改。
守門：finance_tools/tests/test_roc_date.py。

專案內部一律西元 YYYY-MM-DD。民國年只出現在邊界：證交所／櫃買／觀測站的資料讀進來時用 roc_to_iso 轉掉，
要用民國參數查詢時才用 iso_to_roc／ad_year_to_roc 轉回去。不要再寫 ± 1911。

解析一律檢查日期真的存在（1151340、114/02/29 回 None），格式不對回 None；要拋錯的呼叫端自己拋。
"""

import re
from datetime import date

ROC_OFFSET = 1911

# 年份後面不可再接數字（防止 8 碼被當成 7 碼），日期後面可以接說明文字（「115年12月21日（星期一）」）
_ROC_DATE_PATTERNS = (
    re.compile(r"^(\d{3})(\d{2})(\d{2})(?!\d)"),                                # 1150629
    re.compile(r"^(\d{2,3})[/.-](\d{1,2})[/.-](\d{1,2})(?!\d)"),                # 115/06/29、99.1.5
    re.compile(r"^(?:民國)?\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),  # 民國115年6月29日
)
_ROC_MONTH_PATTERNS = (
    re.compile(r"^(\d{3})(\d{2})$"),                                            # 11507
    re.compile(r"^(\d{2,3})\s*[/.\-年]\s*(\d{1,2})\s*月?$"),                     # 115/07、115年7月
)


def _iso_of(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def roc_to_iso(value) -> str | None:
    """民國日期 → 西元 YYYY-MM-DD。認得 1150629、115/06/29、115.6.29、115-06-29、115年6月29日，
    年份可為 2～3 位數（民國 99 年以前）。格式不對或日期不存在回 None。"""
    s = str(value if value is not None else "").strip()
    for pattern in _ROC_DATE_PATTERNS:
        m = pattern.match(s)
        if m:
            return _iso_of(int(m[1]) + ROC_OFFSET, int(m[2]), int(m[3]))
    return None


def roc_month_to_iso(value) -> str | None:
    """民國年月 → 西元 YYYY-MM。認得 11507、115/07、115年7月。格式不對回 None。"""
    s = str(value if value is not None else "").strip()
    for pattern in _ROC_MONTH_PATTERNS:
        m = pattern.match(s)
        if m:
            month = int(m[2])
            return f"{int(m[1]) + ROC_OFFSET}-{month:02d}" if 1 <= month <= 12 else None
    return None


def roc_year_to_ad(value) -> int | None:
    """民國年 → 西元年（年度、股利年度、季別這類只有年的欄位）。不是正整數回 None。"""
    s = str(value if value is not None else "").strip()
    return int(s) + ROC_OFFSET if re.fullmatch(r"\d{1,3}", s) and int(s) > 0 else None


def to_ad_year(year: int) -> int:
    """不確定是民國還是西元的年份（公司自己寫的公告，兩種都有）→ 西元年。小於 1911 視為民國。
    來源確定是民國的用 roc_year_to_ad，不要用這個。"""
    return year + ROC_OFFSET if year < ROC_OFFSET else year


def ad_year_to_roc(year: int) -> int:
    """西元年 → 民國年（觀測站網址、表頭這類要民國年的查詢參數）。"""
    return year - ROC_OFFSET


def iso_to_roc(iso: str, sep: str = "") -> str | None:
    """西元 YYYY-MM-DD → 民國日期。sep 為空字串時回 7 碼 1150902，否則如 115/09/02。格式不對回 None。"""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(iso or ""))
    if not m or not _iso_of(int(m[1]), int(m[2]), int(m[3])):
        return None
    return sep.join([f"{int(m[1]) - ROC_OFFSET:03d}", m[2], m[3]])
