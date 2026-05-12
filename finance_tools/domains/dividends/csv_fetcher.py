"""
CSV Dividend Fetcher - 從 finance_tools/assets/dividend_csv/historical/ 讀取歷史股利資料
每年一個 CSV，每家公司一筆（年度合計），取代 FinMind 抓取邏輯。
"""

import csv
import logging
import os
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

_CSV_DIR = os.path.join(os.path.dirname(__file__), "../../assets/dividend_csv/historical")
_YEARS = [2021, 2022, 2023, 2024, 2025]

_FREQ_MAP = {"年": "年", "半年": "半年", "季": "季"}


def _parse_tw_date(raw: str) -> Optional[str]:
    """'22/07/15 → 2022-07-15"""
    raw = raw.strip().lstrip("'")
    if not raw:
        return None
    parts = raw.split("/")
    if len(parts) != 3:
        return None
    yy, mm, dd = parts
    try:
        return f"{2000 + int(yy)}-{mm}-{dd}"
    except ValueError:
        return None


def _load_year(year: int) -> Dict[str, Dict[str, Any]]:
    """讀取單一年份 CSV，回傳 {code: record}。"""
    path = os.path.join(_CSV_DIR, f"股利所屬年度{year}.csv")
    if not os.path.exists(path):
        logger.warning(f"找不到 {path}")
        return {}

    result: Dict[str, Dict[str, Any]] = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            code = row.get("代號", "").strip().strip("=").strip('"')
            total_raw = row.get("合計股利", "").strip()
            if not code or not total_raw:
                continue

            try:
                cash = float(row.get("現金股利", "0") or 0)
                stock = float(row.get("股票股利", "0") or 0)
                total = float(total_raw)
            except ValueError:
                continue

            if total == 0:
                continue

            freq_raw = row.get("股利發放頻率", "").strip()
            freq = _FREQ_MAP.get(freq_raw, "年")

            payout_date = _parse_tw_date(row.get("現金股利發放日", ""))
            ex_dividend_date = _parse_tw_date(row.get("除息交易日", ""))

            result[code] = {
                "year": year,
                "period": "年度",
                "sequence": 1,
                "cashDividend": round(cash, 4),
                "stockDividend": round(stock, 4),
                "totalDividend": round(total, 4),
                "payoutDate": payout_date,
                "exDividendDate": ex_dividend_date,
                "_frequency": freq,
            }
    return result


def load_all() -> Dict[str, List[Dict[str, Any]]]:
    """
    讀取所有年份 CSV，回傳 {code: [records sorted desc by year]}。
    每家公司每年一筆（年度合計）。
    """
    by_code: Dict[str, List[Dict[str, Any]]] = {}
    freq_by_code: Dict[str, str] = {}

    for year in _YEARS:
        year_data = _load_year(year)
        for code, record in year_data.items():
            freq = record.pop("_frequency", None)
            if freq:
                freq_by_code[code] = freq
            by_code.setdefault(code, []).append(record)

    # sort desc
    for code in by_code:
        by_code[code].sort(key=lambda r: r["year"], reverse=True)

    return by_code, freq_by_code


def load_for_code(code: str):
    """讀取單一公司歷史股利，回傳 (records, frequency)。"""
    all_data, freq_map = load_all()
    return all_data.get(code, []), freq_map.get(code)
