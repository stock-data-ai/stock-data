"""
Fetches market-wide sentiment data:
1. Institutional investors aggregate from TWSE BFI82U
2. Margin trading aggregate from TWSE MI_MARGN + TPEx (per-stock summed)

Data sources:
- Institutional: https://www.twse.com.tw/fund/BFI82U (units: 千元 → converted to 元)
- Margin TWSE:   https://www.twse.com.tw/exchangeReport/MI_MARGN (units: 張)
- Margin TPEx:   https://www.tpex.org.tw/.../margin_bal_result.php  (units: 張)
"""

import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)


class MarketSentimentFetcher:
    INSTITUTIONAL_URL = (
        "https://www.twse.com.tw/fund/BFI82U"
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

    def __init__(self) -> None:
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
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

    def fetch_twse_institutional(self, date_str: str) -> Optional[Dict]:
        """
        Fetch TWSE (上市) institutional investors aggregate from BFI82U.
        Both BFI82U and TPEx OpenAPI return amounts in 元 directly.
        BFI82U ignores date when market is closed; returns latest trading day.
        """
        url = self.INSTITUTIONAL_URL.format(date=date_str)
        try:
            resp = requests.get(url, headers=self.headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if data.get("stat") != "OK":
                logger.warning("BFI82U stat=%s for %s", data.get("stat"), date_str)
                return None

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
            logger.exception("Error fetching BFI82U for %s", date_str)
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
    # Margin trading (融資融券) — TWSE + TPEx per-stock summed
    # ------------------------------------------------------------------

    def _fetch_twse_margin_raw(self, date_str: str) -> Optional[pd.DataFrame]:
        """
        Fetch TWSE per-stock margin data with prev-balance columns included.

        Expected 融資融券彙總 table column indices:
          0: code, 1: name
          2: margin_buy(張), 3: margin_sell(張), 4: margin_repay(張)
          5: prev_margin_balance(張), 6: margin_balance(張), 7: margin_limit
          8: short_sell(張), 9: short_buy(張), 10: short_repay(張)
          11: prev_short_balance(張), 12: short_balance(張), 13: short_limit
          14: offset(資券相抵)
        """
        url = self.TWSE_MARGIN_URL.format(date=date_str)
        try:
            resp = requests.get(url, headers=self.headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if data.get("stat") != "OK":
                logger.warning(
                    "TWSE MI_MARGN returned stat=%s for %s", data.get("stat"), date_str
                )
                return None

            target_table = None
            for table in data.get("tables", []):
                if "融資融券" in table.get("title", ""):
                    target_table = table
                    break

            if not target_table or not target_table.get("data"):
                logger.warning("TWSE MI_MARGN: 融資融券 table not found for %s", date_str)
                return None

            df = pd.DataFrame(
                target_table["data"], columns=target_table["fields"]
            )
            # Select: code, name, margin_buy, margin_sell,
            #         prev_margin_balance, margin_balance,
            #         short_sell, short_buy,
            #         prev_short_balance, short_balance
            result = df.iloc[:, [0, 1, 2, 3, 5, 6, 8, 9, 11, 12]].copy()
            result.columns = [
                "stock_id", "stock_name",
                "margin_buy", "margin_sell",
                "prev_margin_balance", "margin_balance",
                "short_sell", "short_buy",
                "prev_short_balance", "short_balance",
            ]
            for col in result.columns[2:]:
                result[col] = pd.to_numeric(
                    result[col].apply(self._clean_number), errors="coerce"
                ).fillna(0)
            return result
        except Exception:
            logger.exception("Error fetching TWSE MI_MARGN for %s", date_str)
            return None

    def _fetch_tpex_margin_raw(self, date_str: str) -> Optional[pd.DataFrame]:
        """
        Fetch TPEx per-stock margin data with prev-balance columns included.

        Expected tables[0] column indices (TPEx API):
          0: code, 1: name
          2: prev_margin_balance(張), 3: margin_buy(張), 4: margin_sell(張)
          5: margin_repay(張), 6: margin_balance(張)
          ...
          10: prev_short_balance(張), 11: short_sell(張), 12: short_buy(張)
          13: short_repay(張), 14: short_balance(張)
        """
        try:
            dt = datetime.strptime(date_str, "%Y%m%d")
            roc_date = f"{dt.year - 1911}/{dt.month:02}/{dt.day:02}"
        except Exception:
            logger.error("Invalid date_str for TPEx: %s", date_str)
            return None

        timestamp = int(time.time() * 1000)
        url = self.TPEX_MARGIN_URL.format(roc_date=roc_date, timestamp=timestamp)

        try:
            resp = requests.get(url, headers=self.headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            tables = data.get("tables", [])
            if not tables or not tables[0].get("data"):
                logger.warning("TPEx MI_MARGN returned no data for %s", date_str)
                return None

            table = tables[0]
            df = pd.DataFrame(table["data"], columns=table["fields"])
            # Select: code, name,
            #         prev_margin_balance(2), margin_buy(3), margin_sell(4),
            #         margin_balance(6),
            #         prev_short_balance(10), short_sell(11), short_buy(12),
            #         short_balance(14)
            result = df.iloc[:, [0, 1, 2, 3, 4, 6, 10, 11, 12, 14]].copy()
            result.columns = [
                "stock_id", "stock_name",
                "prev_margin_balance", "margin_buy", "margin_sell",
                "margin_balance",
                "prev_short_balance", "short_sell", "short_buy",
                "short_balance",
            ]
            for col in result.columns[2:]:
                result[col] = pd.to_numeric(
                    result[col].apply(self._clean_number), errors="coerce"
                ).fillna(0)
            return result
        except Exception:
            logger.exception("Error fetching TPEx MI_MARGN for %s", date_str)
            return None

    def fetch_margin_aggregate(self, date_str: str) -> Optional[Dict]:
        """
        Compute market-wide margin trading totals by summing per-stock data
        from TWSE + TPEx.

        Returns:
            Dict with:
              longBalance:  { change, buy, sell, balance }  — all in 張
              shortBalance: { change, buy, sell, balance }  — all in 張
            Returns None if both exchanges fail.
        """
        twse_df = self._fetch_twse_margin_raw(date_str)
        tpex_df = self._fetch_tpex_margin_raw(date_str)

        dfs = [df for df in [twse_df, tpex_df] if df is not None]
        if not dfs:
            return None

        combined = pd.concat(dfs, ignore_index=True)

        def isum(col: str) -> int:
            return int(combined[col].sum()) if col in combined.columns else 0

        margin_buy = isum("margin_buy")
        margin_sell = isum("margin_sell")
        margin_balance = isum("margin_balance")
        prev_margin_balance = isum("prev_margin_balance")

        short_buy = isum("short_buy")
        short_sell = isum("short_sell")
        short_balance = isum("short_balance")
        prev_short_balance = isum("prev_short_balance")

        return {
            "longBalance": {
                "change": margin_balance - prev_margin_balance,
                "buy": margin_buy,
                "sell": margin_sell,
                "balance": margin_balance,
            },
            "shortBalance": {
                "change": short_balance - prev_short_balance,
                "buy": short_buy,
                "sell": short_sell,
                "balance": short_balance,
            },
        }

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
        tpex_inst = self.fetch_tpex_institutional()

        if twse_inst or tpex_inst:
            inst: Dict = {"date": formatted_date}
            if twse_inst:
                inst["twse"] = twse_inst
            else:
                logger.warning("TWSE institutional data unavailable for %s", date_str)
            if tpex_inst:
                inst["tpex"] = tpex_inst
            else:
                logger.warning("TPEx institutional data unavailable")
            result["institutional"] = inst
        else:
            logger.warning("All institutional data unavailable for %s", date_str)

        margin = self.fetch_margin_aggregate(date_str)
        if margin:
            result["margin"] = {"date": formatted_date, **margin}
        else:
            logger.warning("Margin aggregate unavailable for %s", date_str)

        return result
