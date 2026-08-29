"""
台股代號 → 名稱／市場別（FinMind `TaiwanStockInfo`）。

**為什麼需要這層**：2026-08-26 證交所來函後，行情與處置名單陸續改走 FinMind。
FinMind 的行情資料集只有 `stock_id`，沒有公司名稱；處置資料集有名稱但沒有市場別。
原本這兩個欄位是證交所／櫃買端點各自附帶的，端點拆掉之後要有地方補回來。

不吃日期時 `TaiwanStockInfo` 回全量（2026-08-29 實測 4,312 筆：twse 2,395、tpex 1,372、
emerging 545）。內容近乎靜態，所以在行程內快取一份就夠；跨行程用 `cache_path` 落地，
排程每天新起行程時不必重打。

**權證不在 `TaiwanStockInfo` 裡**（實測 039038 查無）。需要權證的呼叫端要傳
`include_warrant=True`，改查 `TaiwanStockInfoWithWarrant`——那支大得多，只在真的需要
時才用（處置名單含權證，行情計算則已用 `len(code)==4` 濾掉）。
"""

import json
import logging
import os
import time
from typing import Dict, Optional

from finance_tools.utils.finmind import fetch_finmind

logger = logging.getLogger(__name__)

# market 值沿用既有程式碼的中文單字（p1_reconcile 的 punish rows 用「市」「櫃」）
_TYPE_TO_MARKET = {"twse": "市", "tpex": "櫃", "emerging": "興"}

_CACHE_TTL_SEC = 24 * 3600
_mem: Dict[str, Dict[str, Dict[str, str]]] = {}


def _load(include_warrant: bool, cache_path: Optional[str]) -> Dict[str, Dict[str, str]]:
    dataset = "TaiwanStockInfoWithWarrant" if include_warrant else "TaiwanStockInfo"

    if cache_path and os.path.exists(cache_path):
        if time.time() - os.path.getmtime(cache_path) < _CACHE_TTL_SEC:
            try:
                return json.load(open(cache_path, encoding="utf-8"))
            except Exception:
                logger.warning("stock_info 快取讀取失敗，改重抓：%s", cache_path)

    # 不帶日期 = 全量；帶日期只會回「當天有異動」的少數幾筆（實測 5 筆），是常見誤用
    rows = fetch_finmind(dataset, "", "", label=dataset)
    if not rows:
        return {}

    # 同一代號可能有多列（轉板：興櫃→上櫃 會各留一筆，2026-08-29 實測 4,312 列／
    # 3,141 個代號）。**必須取 date 最新的那筆**，否則市場別會隨回傳順序飄，
    # 已轉上櫃的股票可能被標成「興」。
    latest: Dict[str, str] = {}
    out: Dict[str, Dict[str, str]] = {}
    for r in rows:
        code = str(r.get("stock_id", "")).strip()
        if not code:
            continue
        d = str(r.get("date", ""))
        if code in latest and d <= latest[code]:
            continue
        latest[code] = d
        out[code] = {
            "name": str(r.get("stock_name", "")).strip(),
            "market": _TYPE_TO_MARKET.get(str(r.get("type", "")).strip(), ""),
            "industry": str(r.get("industry_category", "")).strip(),
        }
    if cache_path and out:
        try:
            json.dump(out, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            logger.warning("stock_info 快取寫入失敗：%s", cache_path)
    return out


def stock_info(
    include_warrant: bool = False, cache_path: Optional[str] = None
) -> Dict[str, Dict[str, str]]:
    """
    Returns:
        {code: {"name", "market"（市／櫃／興）, "industry"}}；取不到回空 dict。

    呼叫端務必把「查不到」當成正常情形處理（權證、新上市、已下市都可能查不到），
    不要因為缺一筆就整批放棄。
    """
    key = f"warrant={include_warrant}"
    if key not in _mem:
        _mem[key] = _load(include_warrant, cache_path)
    return _mem[key]
