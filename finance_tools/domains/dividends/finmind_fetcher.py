"""
FinMind Dividend Fetcher - 從 FinMind TaiwanStockDividend 抓取完整股利歷史
year 欄位直接是「股利所屬年度」（民國年），無發放年偏移問題。
"""

import logging
import os
import time
import re
from FinMind.data import DataLoader
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

_PERIOD_SEQUENCE = {
    "第1季": 1, "第2季": 2, "第3季": 3, "第4季": 4,
    "上半年": 1, "下半年": 2,
    "年度": 1,
}

# FinMind 半年配實際格式是「前半年度」/「後半年度」，對應標準 period label
_HALFYEAR_MAP = {
    "前半年度": ("上半年", 1),
    "後半年度": ("下半年", 2),
}


def _parse_year_period(raw: str):
    """
    "113年"          → (2024, "年度",  1)
    "113年第2季"     → (2024, "第2季", 2)
    "113年前半年度"  → (2024, "上半年", 1)
    "113年後半年度"  → (2024, "下半年", 2)
    "不適用" / ""    → None
    """
    raw = raw.strip()
    if not raw or raw == "不適用":
        return None

    m = re.match(r"^(\d+)年(.*)$", raw)
    if not m:
        return None

    roc = int(m.group(1))
    suffix = m.group(2).strip()

    gregorian = roc + 1911
    if not suffix:
        return gregorian, "年度", 1

    if suffix in _HALFYEAR_MAP:
        period, sequence = _HALFYEAR_MAP[suffix]
        return gregorian, period, sequence

    return gregorian, suffix, _PERIOD_SEQUENCE.get(suffix, 1)


class FinMindDividendFetcher:
    """從 FinMind TaiwanStockDividend 抓取每家公司完整股利歷史，支援多 token 輪替。"""

    YEARS_BACK = 5

    def __init__(self, loader=None):
        raw = os.environ.get("FINMIND_API_TOKENS") or \
              os.environ.get("FINMIND_API_TOKEN_local", "")
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        if not tokens:
            raise ValueError("未設定 FINMIND_API_TOKENS")
        self.loaders = [DataLoader(token=t) for t in tokens]
        self._idx = 0
        # 每個 token 600 req/hr → N tokens 共 N×600 req/hr → 間隔 3600/(N×600) = 6/N 秒
        self.REQUEST_DELAY = round(6.0 / len(self.loaders), 2)
        logger.info(f"FinMindDividendFetcher: {len(self.loaders)} 個 token，間隔 {self.REQUEST_DELAY}s")

    def _next_loader(self):
        loader = self.loaders[self._idx % len(self.loaders)]
        self._idx += 1
        return loader

    def fetch_one(self, code: str) -> Optional[List[Dict[str, Any]]]:
        """
        抓單一公司股利，回傳 records list 或 None（失敗）。
        """
        from datetime import date
        start_date = date(date.today().year - self.YEARS_BACK, 1, 1).strftime("%Y-%m-%d")
        try:
            df = self._next_loader().taiwan_stock_dividend(
                stock_id=code,
                start_date=start_date,
            )
        except Exception as e:
            logger.warning(f"  [{code}] FinMind 抓取失敗: {e}")
            return None

        if df is None or df.empty:
            return []

        records = []
        for _, row in df.iterrows():
            parsed = _parse_year_period(str(row.get("year", "") or ""))
            if parsed is None:
                continue

            gregorian, period, sequence = parsed

            cash = float(row.get("CashEarningsDistribution") or 0) + \
                   float(row.get("CashStatutorySurplus") or 0)
            stock = float(row.get("StockEarningsDistribution") or 0) + \
                    float(row.get("StockStatutorySurplus") or 0)

            if cash == 0 and stock == 0:
                continue

            payout_date = str(row.get("CashDividendPaymentDate") or "").strip() or None
            if payout_date and len(payout_date) < 8:
                payout_date = None

            records.append({
                "year": gregorian,
                "period": period,
                "sequence": sequence,
                "cashDividend": round(cash, 4),
                "stockDividend": round(stock, 4),
                "totalDividend": round(cash + stock, 4),
                "payoutDate": payout_date,
            })

        # 合併同 (year, sequence) 的現金+股票股利（FinMind 有時分兩筆）
        merged: Dict[tuple, Dict[str, Any]] = {}
        for r in records:
            key = (r["year"], r["sequence"])
            if key not in merged:
                merged[key] = r.copy()
            else:
                merged[key]["cashDividend"] = round(merged[key]["cashDividend"] + r["cashDividend"], 4)
                merged[key]["stockDividend"] = round(merged[key]["stockDividend"] + r["stockDividend"], 4)
                merged[key]["totalDividend"] = round(merged[key]["cashDividend"] + merged[key]["stockDividend"], 4)
                if not merged[key]["payoutDate"] and r["payoutDate"]:
                    merged[key]["payoutDate"] = r["payoutDate"]

        result = sorted(merged.values(), key=lambda r: (r["year"], r["sequence"]))
        return result

    def fetch_all(self, codes: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """批次抓取，回傳 {code: records}。"""
        result = {}
        total = len(codes)
        for i, code in enumerate(codes, 1):
            if i % 50 == 0:
                logger.info(f"  FinMind 進度: {i}/{total}")
            records = self.fetch_one(code)
            if records is not None:
                result[code] = records
            time.sleep(self.REQUEST_DELAY)
        return result
