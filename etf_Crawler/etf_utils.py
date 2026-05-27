"""
etf_utils.py — 主動式 ETF 爬蟲共用工具

提供：
  create_session()           建立帶重試機制的 Session
  clean_snapshot()           提取持股乾淨欄位
  write_holdings_update()    統一寫入 topHoldings / holdingsHistory
  record_unchanged_snapshot() 無更新時仍記錄當日快照
"""

import json
from datetime import date
from pathlib import Path
from typing import List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HISTORY_KEEP_DAYS = 30


def create_session() -> requests.Session:
    """建立帶重試機制的 requests Session（GET + POST，retries=3）。"""
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.5,
        status_forcelist=[408, 429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def clean_snapshot(h: dict, *, has_foreign_code: bool = False) -> dict:
    """
    從持股 dict 提取乾淨欄位（不含 previousWeight 等比較性欄位）。
    has_foreign_code: 保留 foreignCode 欄位（US/JP 個股用）。
    """
    result: dict = {"name": h["name"], "weight": h["weight"]}
    if h.get("shares") is not None:
        result["shares"] = h["shares"]
    if h.get("code"):
        result["code"] = h["code"]
    if has_foreign_code and h.get("foreignCode"):
        result["foreignCode"] = h["foreignCode"]
    return result


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


def write_holdings_update(
    json_path: Path,
    etf_code: str,
    holdings: list,
    tran_date: Optional[str],
    *,
    has_foreign_code: bool = False,
    history_only: bool = False,
) -> bool:
    """
    更新 ETF JSON 的 topHoldings 與 holdingsHistory。

    has_foreign_code: 持股含 foreignCode 欄位（US/JP 個股）。
    history_only:     只補 holdingsHistory，不動 topHoldings / lastUpdated（backfill 用）。
    """
    if not json_path.exists():
        print(f"  [WARN] 找不到 {json_path.name}")
        return False

    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        if not tran_date:
            print(f"  [SKIP] 無法取得資料日期，跳過寫入")
            return False

        if "holdingsHistory" not in data:
            data["holdingsHistory"] = {}

        def _snap(h: dict) -> dict:
            return clean_snapshot(h, has_foreign_code=has_foreign_code)

        def _key(h: dict) -> str:
            if has_foreign_code:
                return h.get("code") or h.get("foreignCode") or h.get("name", "")
            return h.get("code") or h.get("name", "")

        if history_only:
            if tran_date in data["holdingsHistory"]:
                print(f"  [SKIP] {tran_date} 已存在")
                return True
            data["holdingsHistory"][tran_date] = [_snap(h) for h in holdings]
        else:
            prev_holdings = data.get("topHoldings", [])
            prev_date = data.get("lastUpdated")

            if tran_date == prev_date and prev_holdings:
                if sorted([_snap(h) for h in holdings], key=_key) == \
                        sorted([_snap(h) for h in prev_holdings], key=_key):
                    return record_unchanged_snapshot(
                        json_path, data, etf_code, [_snap(h) for h in holdings], tran_date
                    )

            if prev_date and prev_holdings and prev_date not in data["holdingsHistory"]:
                data["holdingsHistory"][prev_date] = [_snap(h) for h in prev_holdings]

            prev_map = {_key(h): h for h in prev_holdings}
            for h in holdings:
                prev = prev_map.get(_key(h))
                if prev:
                    prev_w = prev.get("weight", 0)
                    h["previousWeight"] = prev_w
                    h["weightChange"] = round(h["weight"] - prev_w, 2)
                    prev_s = prev.get("shares") or 0
                    h["previousShares"] = prev_s
                    h["sharesChange"] = (
                        (h["shares"] - prev_s) if h.get("shares") is not None and prev_s else 0
                    )
                else:
                    h["previousWeight"] = 0
                    h["weightChange"] = h["weight"]
                    h["previousShares"] = 0
                    h["sharesChange"] = h.get("shares") or 0

            data["topHoldings"] = holdings
            data["holdingsHistory"][tran_date] = [_snap(h) for h in holdings]
            data["lastUpdated"] = tran_date

        for old_d in sorted(data["holdingsHistory"].keys(), reverse=True)[HISTORY_KEEP_DAYS:]:
            del data["holdingsHistory"][old_d]

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"  [OK] {etf_code} — {len(holdings)} 筆持股，{tran_date}")
        return True

    except Exception as e:
        print(f"  [ERROR] 寫入失敗: {e}")
        import traceback
        traceback.print_exc()
        return False
