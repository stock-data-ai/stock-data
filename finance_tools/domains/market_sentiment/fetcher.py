"""
Fetches market-wide sentiment data:
1. Institutional investors aggregate from TWSE BFI82U
2. Margin trading aggregate from TWSE MI_MARGN + TPEx (per-stock summed)

Data sources:
- Institutional: https://www.twse.com.tw/fund/BFI82U (units: 千元 → converted to 元)
- Margin TWSE:   https://www.twse.com.tw/exchangeReport/MI_MARGN (units: 張)
- Margin TPEx:   https://www.tpex.org.tw/.../margin_bal_result.php  (units: 張)
- Margin 長序列: FinMind TaiwanStockTotalMarginPurchaseShortSale（整段區間一次抓）
"""

import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)


class MarketSentimentFetcher:
    INSTITUTIONAL_URL = (
        "https://www.twse.com.tw/zh/fund/BFI82U"
        "?response=json&dayDate={date}&type=day"
    )
    TPEX_INSTITUTIONAL_URL = (
        "https://www.tpex.org.tw/openapi/v1/tpex_3insti_summary"
    )
    TWSE_MARGIN_URL = (
        "https://www.twse.com.tw/exchangeReport/MI_MARGN"
        "?response=json&date={date}&selectType=ALL"
    )
    TPEX_MARGIN_URL = (
        "https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/"
        "margin_bal_result.php?l=zh-tw&d={roc_date}&_={timestamp}"
    )
    FINMIND_DATA_URL = "https://api.finmindtrade.com/api/v4/data"
    FINMIND_MARGIN_DATASET = "TaiwanStockTotalMarginPurchaseShortSale"
    # FinMind 的 name 欄位 → 我們的欄位名（都取 TodayBalance = 當日餘額）
    FINMIND_MARGIN_FIELDS = {
        "MarginPurchaseMoney": "longAmount",   # 融資餘額金額（元）
        "MarginPurchase": "longBalance",       # 融資餘額（張）
        "ShortSale": "shortBalance",           # 融券餘額（張）
    }
    FINMIND_INST_DATASET = "TaiwanStockTotalInstitutionalInvestors"
    # 與 BFI82U 逐類對帳一致（2026-08-07 五類買賣金額完全相同），取 buy - sell = 買賣超
    FINMIND_INST_FIELDS = {
        "Foreign_Investor": "foreign",
        "Investment_Trust": "trust",
        "Dealer_self": "dealer",
        "Dealer_Hedging": "dealerHedge",
        "Foreign_Dealer_Self": "foreignDealer",
    }

    def __init__(self) -> None:
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.twse.com.tw/",
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _clean_number(self, val: Any) -> Any:
        if isinstance(val, str):
            return val.replace(",", "")
        return val

    def _parse_yuan(self, val: str) -> int:
        """Parse a NT$ integer string (may have commas). BFI82U returns 元 directly."""
        try:
            return int(str(val).replace(",", "").replace(" ", ""))
        except (ValueError, AttributeError):
            return 0

    # ------------------------------------------------------------------
    # Institutional investors (三大法人)
    # ------------------------------------------------------------------

    def _map_inst_rows(self, inst_map: Dict, name_foreign: str) -> Dict:
        """Map a {name: {buy,sell,net}} dict to our canonical keys."""
        zero = {"buy": 0, "sell": 0, "net": 0}
        return {
            "foreign": inst_map.get(name_foreign, zero),
            "trust": inst_map.get("投信", zero),
            "dealer": inst_map.get("自營商(自行買賣)", zero),
            "dealerHedge": inst_map.get("自營商(避險)", zero),
            "foreignDealer": inst_map.get("外資自營商", zero),
        }

    def fetch_twse_institutional(self, date_str: str, retries: int = 3, retry_delay: int = 30) -> Optional[Dict]:
        """
        Fetch TWSE (上市) institutional investors aggregate from BFI82U.
        Retries up to `retries` times with `retry_delay` seconds between attempts.
        """
        url = self.INSTITUTIONAL_URL.format(date=date_str)
        for attempt in range(retries):
            try:
                resp = requests.get(url, headers=self.headers, timeout=15)
                resp.raise_for_status()
                data = resp.json()

                if data.get("stat") != "OK":
                    logger.warning("BFI82U stat=%s for %s (attempt %d/%d)", data.get("stat"), date_str, attempt + 1, retries)
                else:
                    inst_map = {
                        row[0]: {
                            "buy": self._parse_yuan(row[1]),
                            "sell": self._parse_yuan(row[2]),
                            "net": self._parse_yuan(row[3]),
                        }
                        for row in data.get("data", [])
                    }
                    return self._map_inst_rows(inst_map, "外資及陸資(不含外資自營商)")
            except Exception:
                logger.warning("Error fetching BFI82U for %s (attempt %d/%d)", date_str, attempt + 1, retries, exc_info=True)

            if attempt < retries - 1:
                time.sleep(retry_delay)

        logger.error("BFI82U fetch failed after %d attempts for %s", retries, date_str)
        return None

    def fetch_tpex_institutional(self) -> Optional[Dict]:
        """
        Fetch TPEx (上櫃) institutional investors aggregate from OpenAPI.
        Note: this endpoint always returns the latest trading day (no date param).
        """
        try:
            resp = requests.get(
                self.TPEX_INSTITUTIONAL_URL, headers=self.headers, timeout=15
            )
            resp.raise_for_status()
            rows = resp.json()

            # Investor names have leading full-width spaces for sub-items; strip them.
            # TPEx uses "外資及陸資(不含自營商)" (not "不含外資自營商")
            inst_map = {
                row["Investor"].strip(): {
                    "buy": self._parse_yuan(row["PurchaseAmount"]),
                    "sell": self._parse_yuan(row["SaleAmount"]),
                    "net": self._parse_yuan(row["Net"]),
                }
                for row in rows
            }
            return self._map_inst_rows(inst_map, "外資及陸資(不含自營商)")
        except Exception:
            logger.exception("Error fetching TPEx 三大法人 OpenAPI")
            return None

    # ------------------------------------------------------------------
    # Margin trading (融資融券)
    # ------------------------------------------------------------------

    def _parse_margin_balance_row(self, row: list) -> Dict:
        """
        Parse one row from TWSE 信用交易統計 (table[0]).
        Fields: 項目, 買進, 賣出, 現金(券)償還, 前日餘額, 今日餘額
        """
        p = self._parse_yuan  # reuse int parser (no ×1000 needed)
        today = p(row[5])
        prev = p(row[4])
        return {
            "change": today - prev,
            "buy": p(row[1]),
            "sell": p(row[2]),
            "balance": today,
        }

    def fetch_twse_margin(self, date_str: str, retries: int = 3, retry_delay: int = 30) -> Optional[Dict]:
        """
        Fetch TWSE (上市) margin aggregate from MI_MARGN table[0] (信用交易統計).
        Only accepts data for the exact requested date — no cross-date fallback,
        which would silently mislabel a previous day's data as `date_str` when
        the data is not yet published or a request is rate-limited.
        Retries the same date up to `retries` times with `retry_delay` seconds between attempts.
        (Unlike BFI82U, MI_MARGN does not auto-return the latest trading day.)

        table[0] rows (欄位: 項目, 買進, 賣出, 現金(券)償還, 前日餘額, 今日餘額):
          0: 融資(交易單位)  — 張
          1: 融券(交易單位)  — 張
          2: 融資金額(仟元)  — 千元

        Returns:
          longBalance:  { change, buy, sell, balance }  — 張
          shortBalance: { change, buy, sell, balance }  — 張
          longAmount:   { change, buy, sell, balance }  — 元 (千元 × 1000)
        """
        for attempt in range(retries):
            result = self._fetch_twse_margin_single(date_str)
            if result is not None:
                return result
            if attempt < retries - 1:
                time.sleep(retry_delay)
        logger.warning("TWSE margin: no data for %s (尚未公布或休市)", date_str)
        return None

    def _fetch_twse_margin_single(self, date_str: str) -> Optional[Dict]:
        url = self.TWSE_MARGIN_URL.format(date=date_str)
        try:
            resp = requests.get(url, headers=self.headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if data.get("stat") != "OK":
                return None

            summary_table = data.get("tables", [None])[0]
            if not summary_table or not summary_table.get("data"):
                return None

            rows = summary_table["data"]
            if len(rows) < 3:
                logger.warning("TWSE 信用交易統計 unexpected row count: %d for %s", len(rows), date_str)
                return None

            long_balance = self._parse_margin_balance_row(rows[0])   # 融資(交易單位)
            short_balance = self._parse_margin_balance_row(rows[1])  # 融券(交易單位)

            # 融資金額(仟元) → convert to 元
            raw = self._parse_margin_balance_row(rows[2])
            long_amount = {k: v * 1000 for k, v in raw.items()}

            return {
                "longBalance": long_balance,
                "shortBalance": short_balance,
                "longAmount": long_amount,
            }
        except Exception:
            logger.exception("Error fetching TWSE margin for %s", date_str)
            return None

    def fetch_tpex_margin(self, date_str: str, retries: int = 3, retry_delay: int = 30) -> Optional[Dict]:
        """
        Fetch TPEx (上櫃) margin aggregate by summing per-stock data.
        Only accepts data for the exact requested date — no cross-date fallback,
        which would silently mislabel a previous day's data as `date_str`.
        Retries the same date up to `retries` times with `retry_delay` seconds between attempts.
        TPEx has no server-side aggregate table; per-stock summation is required.

        TPEx table[0] fields (confirmed 2026):
          0:代號  1:名稱  2:前資餘額(張)  3:資買  4:資賣  5:現償  6:資餘額
          10:前券餘額(張)  11:券賣  12:券買  13:券償  14:券餘額
        """
        try:
            dt = datetime.strptime(date_str, "%Y%m%d")
        except Exception:
            logger.error("Invalid date_str for TPEx: %s", date_str)
            return None

        for attempt in range(retries):
            result = self._fetch_tpex_margin_single(date_str, dt)
            if result is not None:
                return result
            if attempt < retries - 1:
                time.sleep(retry_delay)
        logger.warning("TPEx margin: no data for %s (尚未公布或休市)", date_str)
        return None

    def _fetch_tpex_margin_single(self, date_str: str, dt: datetime) -> Optional[Dict]:
        roc_date = f"{dt.year - 1911}/{dt.month:02}/{dt.day:02}"
        timestamp = int(time.time() * 1000)
        url = self.TPEX_MARGIN_URL.format(roc_date=roc_date, timestamp=timestamp)

        try:
            resp = requests.get(url, headers=self.headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            tables = data.get("tables", [])
            if not tables or not tables[0].get("data"):
                return None

            table = tables[0]
            df = pd.DataFrame(table["data"], columns=table["fields"])
            # cols: prev_margin(2), margin_buy(3), margin_sell(4), margin_balance(6)
            #       prev_short(10), short_sell(11), short_buy(12), short_balance(14)
            cols = df.columns[[2, 3, 4, 6, 10, 11, 12, 14]]
            names = [
                "prev_margin", "margin_buy", "margin_sell", "margin_balance",
                "prev_short", "short_sell", "short_buy", "short_balance",
            ]
            num = df[cols].copy()
            num.columns = names
            for c in names:
                num[c] = pd.to_numeric(
                    num[c].apply(self._clean_number), errors="coerce"
                ).fillna(0)

            s = num.sum()
            return {
                "longBalance": {
                    "change": int(s["margin_balance"] - s["prev_margin"]),
                    "buy": int(s["margin_buy"]),
                    "sell": int(s["margin_sell"]),
                    "balance": int(s["margin_balance"]),
                },
                "shortBalance": {
                    "change": int(s["short_balance"] - s["prev_short"]),
                    "buy": int(s["short_sell"]),   # 券賣 = 融券建倉
                    "sell": int(s["short_buy"]),   # 券買 = 融券回補
                    "balance": int(s["short_balance"]),
                },
            }
        except Exception:
            logger.exception("Error fetching TPEx margin for %s", date_str)
            return None

    def fetch_twse_margin_history(
        self, start_date: str, end_date: str, retries: int = 3, retry_delay: int = 10
    ) -> Optional[List[Dict]]:
        """
        Fetch 上市整體融資融券「餘額水位」長序列（FinMind，整段區間一次抓）。

        MI_MARGN 只能逐日查，補兩年水位要 480+ 次請求；FinMind 同一口徑一次給完。
        已對帳 2026-08-05~08-07：融資張數／融資金額／融券張數與 MI_MARGN 逐日相同。
        免 token 可用；有 token 時帶上以吃較高的額度。

        Args:
            start_date / end_date: YYYY-MM-DD

        Returns:
            [{date, longAmount(元), longBalance(張), shortBalance(張)}, ...] 舊→新
            全區間都拿不到時回 None（呼叫端據此保留既有檔案）。
        """
        rows = self._fetch_finmind_range(
            self.FINMIND_MARGIN_DATASET, start_date, end_date, "融資水位", retries, retry_delay
        )
        if rows is None:
            return None

        by_date: Dict[str, Dict[str, int]] = {}
        for row in rows:
            field = self.FINMIND_MARGIN_FIELDS.get(row.get("name"))
            date = row.get("date")
            if not field or not date:
                continue
            by_date.setdefault(date, {})[field] = int(row.get("TodayBalance") or 0)

        fields = ("longAmount", "longBalance", "shortBalance")
        series = [
            {"date": date, **{f: vals[f] for f in fields}}
            for date, vals in sorted(by_date.items())
            if set(fields) <= vals.keys()
        ]
        if not series:
            logger.error("FinMind 融資水位回傳資料不完整 %s~%s", start_date, end_date)
            return None
        return series

    def fetch_twse_institutional_history(
        self, start_date: str, end_date: str, retries: int = 3, retry_delay: int = 10
    ) -> Optional[List[Dict]]:
        """
        Fetch 上市三大法人每日買賣超長序列（FinMind，整段區間一次抓）。

        BFI82U 只能逐日查；FinMind 同一口徑一次給完。
        已對帳 2026-08-07：五類的買進／賣出金額與 BFI82U 完全相同。

        Returns:
            [{date, foreign, trust, dealer, dealerHedge, foreignDealer}, ...] 舊→新
            單位：元，值為買賣超（buy - sell）。
        """
        rows = self._fetch_finmind_range(
            self.FINMIND_INST_DATASET, start_date, end_date, "三大法人", retries, retry_delay
        )
        if rows is None:
            return None

        by_date: Dict[str, Dict[str, int]] = {}
        for row in rows:
            field = self.FINMIND_INST_FIELDS.get(row.get("name"))
            date = row.get("date")
            if not field or not date:
                continue
            by_date.setdefault(date, {})[field] = int(row.get("buy") or 0) - int(row.get("sell") or 0)

        fields = tuple(self.FINMIND_INST_FIELDS.values())
        series = [
            # 外資自營商常年為 0，FinMind 偶爾整天不給該列，補 0 而非整天丟掉
            {"date": date, **{f: vals.get(f, 0) for f in fields}}
            for date, vals in sorted(by_date.items())
            if "foreign" in vals
        ]
        if not series:
            logger.error("FinMind 三大法人回傳資料不完整 %s~%s", start_date, end_date)
            return None
        return series

    def _fetch_finmind_range(
        self, dataset: str, start_date: str, end_date: str,
        label: str, retries: int, retry_delay: int,
    ) -> Optional[List[Dict]]:
        """FinMind v4 區間查詢。免 token 可用；有 token 時帶上以吃較高的額度。"""
        params = {"dataset": dataset, "start_date": start_date, "end_date": end_date}
        headers = {"Accept": "application/json"}
        raw_token = (
            os.environ.get("FINMIND_API_TOKENS")
            or os.environ.get("FINMIND_API_TOKEN_local")
            or ""
        )
        token = raw_token.split(",")[0].strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        for attempt in range(retries):
            try:
                resp = requests.get(
                    self.FINMIND_DATA_URL, params=params, headers=headers, timeout=30
                )
                resp.raise_for_status()
                payload = resp.json()
                rows = payload.get("data") or []
                if rows:
                    return rows
                logger.warning(
                    "FinMind %s 無資料 %s~%s: msg=%s (attempt %d/%d)",
                    label, start_date, end_date, payload.get("msg"), attempt + 1, retries,
                )
            except Exception:
                logger.warning(
                    "Error fetching FinMind %s %s~%s (attempt %d/%d)",
                    label, start_date, end_date, attempt + 1, retries, exc_info=True,
                )

            if attempt < retries - 1:
                time.sleep(retry_delay)

        logger.error("FinMind %s 長序列取得失敗 %s~%s", label, start_date, end_date)
        return None

    # ------------------------------------------------------------------
    # Combined
    # ------------------------------------------------------------------

    def fetch_all(self, date_str: str) -> Dict:
        """
        Fetch full market sentiment for a given date.

        Args:
            date_str: YYYYMMDD

        Returns:
            Dict with optional keys 'institutional' and 'margin'.
            institutional contains 'twse' (上市) and 'tpex' (上櫃) sub-objects.
            Both BFI82U and TPEx OpenAPI return the latest trading day; the date
            param is used for TWSE BFI82U and margin only.
        """
        formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        result: Dict = {}

        twse_inst = self.fetch_twse_institutional(date_str)
        # tpex_inst = self.fetch_tpex_institutional()  # 上櫃暫停，只顯示上市

        if twse_inst:
            inst: Dict = {"date": formatted_date}
            inst["twse"] = twse_inst
            # if tpex_inst:
            #     inst["tpex"] = tpex_inst
            result["institutional"] = inst
        else:
            logger.warning("TWSE institutional data unavailable for %s", date_str)

        twse_margin = self.fetch_twse_margin(date_str)
        tpex_margin = self.fetch_tpex_margin(date_str)

        if twse_margin or tpex_margin:
            margin: Dict = {"date": formatted_date}
            if twse_margin:
                margin["twse"] = twse_margin
            else:
                logger.warning("TWSE margin unavailable for %s", date_str)
            if tpex_margin:
                margin["tpex"] = tpex_margin
            else:
                logger.warning("TPEx margin unavailable for %s", date_str)
            result["margin"] = margin
        else:
            logger.warning("All margin data unavailable for %s", date_str)

        return result
