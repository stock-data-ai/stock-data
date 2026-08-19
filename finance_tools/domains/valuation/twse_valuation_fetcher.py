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
from finance_tools.utils.twse_url import bust as _bust

logger = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.twse.com.tw/",
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

    # 兩支都吃 date 參數：補跑指定日期會拿到那一天，而非「最新一筆」。
    # （舊的 STOCK_DAY_ALL/BWIBBU_ALL 沒有 date，補跑 8/19 會寫進 8/20 的價格；
    #   且 STOCK_DAY_ALL 自 2026-08-18 起持續逾時，上市市值連兩天靜默停更。）
    BWIBBU_D_URL = "https://www.twse.com.tw/exchangeReport/BWIBBU_d?response=json&date={date}&selectType=ALL"
    MI_INDEX_URL = "https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={date}&type=ALLBUT0999"
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
        """BWIBBU_d 一次給齊收盤價/本益比/淨值比/殖利率；MI_INDEX 補上它沒有的 ETF 與 F 股。"""
        try:
            # BWIBBU_d: [0]代號 [1]名稱 [2]收盤價 [3]殖利率(%) [4]股利年度 [5]本益比 [6]股價淨值比
            ratios: Dict[str, dict] = {}
            ratio_resp = requests.get(
                _bust(self.BWIBBU_D_URL.format(date=date_str)), timeout=60, headers=_BROWSER_HEADERS
            )
            ratio_resp.raise_for_status()
            ratio_data = ratio_resp.json()
            if ratio_data.get("stat") == "OK" and ratio_data.get("data"):
                for row in ratio_data["data"]:
                    ratios[row[0].strip()] = {
                        "close": _parse_float(row[2]),
                        "dividend_yield": _parse_float(row[3]),
                        "pe": _parse_float(row[5]),
                        "pb": _parse_float(row[6]),
                    }
            else:
                logger.warning(f"TWSE BWIBBU_d: 無 {date_str} 資料（非交易日或尚未公布）")

            # TWSE 對同一連線的連續請求有速率限制，緊接著打第二個 API 會被卡住 timeout
            time.sleep(3)

            # MI_INDEX 的「每日收盤行情」表比 BWIBBU_d 多約 235 檔（ETF、F 股／KY 無本益比者）
            closes: Dict[str, float] = {}
            close_resp = requests.get(
                _bust(self.MI_INDEX_URL.format(date=date_str)), timeout=60, headers=_BROWSER_HEADERS
            )
            close_resp.raise_for_status()
            close_data = close_resp.json()
            if close_data.get("stat") == "OK":
                for table in close_data.get("tables") or []:
                    if "每日收盤行情" not in (table.get("title") or ""):
                        continue
                    # [0]代號 [1]名稱 [2]成交股數 [3]成交筆數 [4]成交金額 [5]開 [6]高 [7]低 [8]收盤價
                    for row in table.get("data") or []:
                        close = _parse_float(row[8]) if len(row) > 8 else None
                        if close is not None:
                            closes[row[0].strip()] = close
            if not closes:
                logger.warning(f"TWSE MI_INDEX: 無 {date_str} 收盤行情，僅用 BWIBBU_d 的收盤價")

            result: Dict[str, ValuationRecord] = {}
            for code in set(closes) | set(ratios):
                r = ratios.get(code, {})
                close = closes.get(code)
                if close is None:
                    close = r.get("close")
                if not code or close is None:
                    continue
                result[code] = ValuationRecord(
                    close=close,
                    pe=r.get("pe"),
                    pb=r.get("pb"),
                    dividend_yield=r.get("dividend_yield"),
                )
            return result or None
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
        req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
