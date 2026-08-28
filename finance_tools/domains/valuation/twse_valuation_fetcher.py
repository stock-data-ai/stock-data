"""
TWSE / TPEx 市值 + 估值指標 — 一次全市場批次撈取。

上市 (TWSE): STOCK_DAY_ALL（收盤價）+ BWIBBU_ALL（本益比/殖利率/股價淨值比）
上櫃 (TPEx): tpex_mainboard_quotes（收盤價）+ tpex_mainboard_peratio_analysis（本益比/殖利率/股價淨值比）

四個 API 都是「當日全市場一次回傳」，取代 yfinance 逐股呼叫。
市值 = 收盤價 × issuedCommonShares（由呼叫端帶入 companies-all.json 的股數資料計算）。
"""
import json
import logging
import time
import urllib.request
from typing import Dict, Optional

import requests

from finance_tools.utils.retry import retry as _retry
from finance_tools.utils.finmind import fetch_finmind

logger = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.tpex.org.tw/",
}


class ValuationRecord:
    __slots__ = ("close", "pe", "pb", "dividend_yield")

    def __init__(self, close: float, pe: Optional[float] = None,
                 pb: Optional[float] = None, dividend_yield: Optional[float] = None):
        self.close = close
        self.pe = pe
        self.pb = pb
        self.dividend_yield = dividend_yield


def _parse_float(val) -> Optional[float]:
    try:
        f = float(str(val).replace(",", "").strip())
        return f
    except (ValueError, AttributeError):
        return None


class TWSEValuationFetcher:
    """
    一次取回全市場收盤價 + 本益比/股價淨值比/殖利率（上市 + 上櫃）。
    fetch_all() 最多 4 次 HTTP 呼叫，取代數千次 yfinance per-stock 請求。
    """

    # FinMind 兩支都吃日期區間，補跑指定日期會拿到那一天而非「最新一筆」——
    # 這是原本不能改用 openapi BWIBBU_ALL／STOCK_DAY_ALL 的原因（那兩支沒有 date）。
    FINMIND_PER_DATASET = "TaiwanStockPER"      # 本益比／股價淨值比／殖利率
    FINMIND_PRICE_DATASET = "TaiwanStockPrice"  # 收盤價（含 ETF、F 股、權證）
    TPEX_QUOTES_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
    TPEX_PERATIO_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"

    def fetch_all(self, date_str: str) -> Dict[str, ValuationRecord]:
        """
        Args:
            date_str: YYYYMMDD，e.g. '20260819'
        Returns:
            {code: ValuationRecord} — 可能含上市 + 上櫃股票
        """
        result: Dict[str, ValuationRecord] = {}

        listed = _retry(lambda: self._fetch_listed(date_str), "TWSE BWIBBU_d/MI_INDEX")
        self.listed_ok = listed is not None
        if listed is not None:
            result.update(listed)
            logger.info(f"TWSE 市值估值: {len(listed)} 支上市股票")
        else:
            logger.warning("TWSE BWIBBU_d/MI_INDEX: 無法取得資料")

        otc = _retry(self._fetch_otc, "TPEx tpex_mainboard_quotes/peratio_analysis")
        if otc is not None:
            result.update(otc)
            logger.info(f"TPEx 市值估值: {len(otc)} 支上櫃股票")
        else:
            logger.warning("TPEx tpex_mainboard_quotes/peratio_analysis: 無法取得資料")

        logger.info(f"市值估值合計: {len(result)} 支股票")
        return result

    # ──────────────────────────────────────────────────────────────
    # TWSE  (上市)
    # ──────────────────────────────────────────────────────────────
    def _fetch_listed(self, date_str: str) -> Optional[Dict[str, ValuationRecord]]:
        """
        全市場收盤價 + 本益比／淨值比／殖利率（FinMind）。

        **不再抓 www.twse.com.tw 的 BWIBBU_d／MI_INDEX**：2026-08-26 證交所來函後移除，
        那兩支是其網站條款明文禁止爬取的網頁端點（與 openapi.twse.com.tw 的開放資料不同）。

        `TaiwanStockPER` 同時涵蓋上市與上櫃，所以這裡也會回上櫃代號；`fetch_all` 之後
        再用 `_fetch_otc`（櫃買 OpenAPI，開放資料）覆蓋上櫃，兩者不衝突。

        對帳紀錄：2026-08-27 與開放資料 BWIBBU_d 逐檔比對，1,081 檔 × PER／PBR／殖利率
        三欄全部相同。
        """
        d = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        per_rows = fetch_finmind(self.FINMIND_PER_DATASET, d, d, label="本益比")
        price_rows = fetch_finmind(self.FINMIND_PRICE_DATASET, d, d, label="收盤價")

        if not per_rows and not price_rows:
            logger.warning("FinMind 估值：%s 無資料（非交易日或尚未公布）", date_str)
            return None

        ratios: Dict[str, dict] = {}
        for row in per_rows or []:
            code = str(row.get("stock_id", "")).strip()
            if code:
                ratios[code] = {
                    "dividend_yield": _parse_float(row.get("dividend_yield")),
                    "pe": _parse_float(row.get("PER")),
                    "pb": _parse_float(row.get("PBR")),
                }

        # 收盤價要另外抓：PER 那支沒有價格。價格涵蓋面比 PER 廣（ETF、F 股／KY 無本益比者）
        closes: Dict[str, float] = {}
        for row in price_rows or []:
            code = str(row.get("stock_id", "")).strip()
            close = _parse_float(row.get("close"))
            if code and close is not None and close > 0:
                closes[code] = close
        if not closes:
            logger.warning("FinMind 收盤價：無 %s 資料，僅能用有 PER 的部分", date_str)

        result: Dict[str, ValuationRecord] = {}
        for code in set(closes) | set(ratios):
            r = ratios.get(code, {})
            close = closes.get(code)
            if not code or close is None:
                continue
            result[code] = ValuationRecord(
                close=close,
                pe=r.get("pe"),
                pb=r.get("pb"),
                dividend_yield=r.get("dividend_yield"),
            )
        return result or None

    # ──────────────────────────────────────────────────────────────
    # TPEx  (上櫃)
    # ──────────────────────────────────────────────────────────────
    def _fetch_otc(self) -> Optional[Dict[str, ValuationRecord]]:
        try:
            quotes = self._fetch_tpex_json(self.TPEX_QUOTES_URL)
            if not quotes:
                return None

            time.sleep(3)

            ratios = {}
            peratio_data = self._fetch_tpex_json(self.TPEX_PERATIO_URL) or []
            for rec in peratio_data:
                code = rec.get("SecuritiesCompanyCode", "").strip()
                if not code:
                    continue
                ratios[code] = {
                    "pe": _parse_float(rec.get("PriceEarningRatio")),
                    "dividend_yield": _parse_float(rec.get("YieldRatio")),
                    "pb": _parse_float(rec.get("PriceBookRatio")),
                }

            result = {}
            for rec in quotes:
                code = rec.get("SecuritiesCompanyCode", "").strip()
                close = _parse_float(rec.get("Close"))
                if not code or close is None:
                    continue
                r = ratios.get(code, {})
                result[code] = ValuationRecord(
                    close=close,
                    pe=r.get("pe"),
                    pb=r.get("pb"),
                    dividend_yield=r.get("dividend_yield"),
                )
            return result
        except Exception as e:
            logger.error(f"TPEx 市值估值 error: {e}")
            return None

    @staticmethod
    def _fetch_tpex_json(url: str):
        req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
