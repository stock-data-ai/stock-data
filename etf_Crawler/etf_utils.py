"""
etf_utils.py — 主動式 ETF 爬蟲共用工具
"""

import json
from datetime import date
from pathlib import Path
from typing import List

HISTORY_KEEP_DAYS = 30


def record_unchanged_snapshot(
    json_path: Path,
    data: dict,
    etf_code: str,
    holdings_clean: List[dict],
    source_date: str,
) -> bool:
    """
    當來源網站尚未發佈新資料（source_date == lastUpdated），
    仍以今天的執行日期寫一筆相同快照到 holdingsHistory，
    讓歷史記錄連續、不留空白交易日。
    topHoldings 與 lastUpdated 不更動。
    """
    today = date.today().isoformat()
    if "holdingsHistory" not in data:
        data["holdingsHistory"] = {}

    if today != source_date and today not in data["holdingsHistory"]:
        data["holdingsHistory"][today] = holdings_clean
        for old_d in sorted(data["holdingsHistory"].keys(), reverse=True)[HISTORY_KEEP_DAYS:]:
            del data["holdingsHistory"][old_d]
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"  [OK] {etf_code} 來源未更新，記錄 {today} 快照（持股同 {source_date}）")
    else:
        print(f"  [SKIP] {etf_code} 數據無變化（{source_date}），今日已有記錄")
    return True
