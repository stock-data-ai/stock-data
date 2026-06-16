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

logger = logging.getLogger(__name__)


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

    STOCK_DAY_ALL_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=json"
    BWIBBU_ALL_URL = "https://www.twse.com.tw/exchangeReport/BWIBBU_ALL?response=json"
    TPEX_QUOTES_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
    TPEX_PERATIO_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"

    def fetch_all(self) -> Dict[str, ValuationRecord]:
        """
        Returns:
            {code: ValuationRecord} — 可能含上市 + 上櫃股票
        """
        result: Dict[str, ValuationRecord] = {}

        listed = _retry(self._fetch_listed, "TWSE STOCK_DAY_ALL/BWIBBU_ALL")
        if listed is not None:
            result.update(listed)
            logger.info(f"TWSE 市值估值: {len(listed)} 支上市股票")
        else:
            logger.warning("TWSE STOCK_DAY_ALL/BWIBBU_ALL: 無法取得資料")

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
    def _fetch_listed(self) -> Optional[Dict[str, ValuationRecord]]:
        try:
            close_resp = requests.get(self.STOCK_DAY_ALL_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            close_resp.raise_for_status()
            close_data = close_resp.json()
            if close_data.get("stat") != "OK" or not close_data.get("data"):
                return None

            # TWSE 對同一連線的連續請求有速率限制，緊接著打第二個 API 會被卡住 timeout
            time.sleep(3)

            ratio_resp = requests.get(self.BWIBBU_ALL_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            ratio_resp.raise_for_status()
            ratio_data = ratio_resp.json()
            ratios = {}
            if ratio_data.get("stat") == "OK" and ratio_data.get("data"):
                # BWIBBU_ALL: [0]代號 [1]名稱 [2]本益比 [3]殖利率(%) [4]股價淨值比
                for row in ratio_data["data"]:
                    code = row[0].strip()
                    ratios[code] = {
                        "pe": _parse_float(row[2]),
                        "dividend_yield": _parse_float(row[3]),
                        "pb": _parse_float(row[4]),
                    }
            else:
                logger.warning("TWSE BWIBBU_ALL: 無資料，僅使用收盤價")

            # STOCK_DAY_ALL: [0]代號 [1]名稱 ... [7]收盤價
            result = {}
            for row in close_data["data"]:
                code = row[0].strip()
                close = _parse_float(row[7]) if len(row) > 7 else None
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
            logger.error(f"TWSE 市值估值 error: {e}")
            return None

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
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
