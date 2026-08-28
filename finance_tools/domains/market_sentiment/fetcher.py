"""
Fetches market-wide sentiment data:
1. Institutional investors aggregate from TWSE BFI82U
2. Margin trading aggregate from TWSE MI_MARGN + TPEx (per-stock summed)

Data sources:
- Institutional 上市: FinMind TaiwanStockTotalInstitutionalInvestors（單位：元）
- Institutional 上櫃: https://www.tpex.org.tw/openapi/v1/tpex_3insti_summary
- Margin 上市:       FinMind TaiwanStockTotalMarginPurchaseShortSale（張／元）
- Margin 上櫃:       https://www.tpex.org.tw/.../margin_bal_result.php（張）

**上市部分不再直接抓證交所網站**：2026-08-26 證交所來函後移除。`www.twse.com.tw`
的 BFI82U／MI_MARGN 屬其網站使用條款明文禁止爬取的範圍，與 openapi.twse.com.tw
的開放資料性質不同。改用 FinMind 同口徑資料，日／區間共用同一來源。

對帳紀錄（換過去前逐欄位比對，零差異）：
- 2026-08-07：五類三大法人的買進／賣出金額與 BFI82U 完全相同
- 2026-08-27：三大法人 buy/sell/net（5 類 × 3 欄）與融資融券 change/buy/sell/balance
  （3 組 × 4 欄）共 27 個數字，與 BFI82U／MI_MARGN 全部相同
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
    TPEX_INSTITUTIONAL_URL = (
        "https://www.tpex.org.tw/openapi/v1/tpex_3insti_summary"
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
            "Referer": "https://www.tpex.org.tw/",
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
        Fetch TWSE (上市) institutional investors aggregate（FinMind，單位：元）。

        回傳形狀與上櫃路徑一致：{foreign|trust|dealer|dealerHedge|foreignDealer: {buy, sell, net}}。
        FinMind 另有一列 name="total"，不在 FINMIND_INST_FIELDS 內，自動略過。
        """
        rows = self._finmind_rows_for_date(
            self.FINMIND_INST_DATASET, date_str, "三大法人", retries, retry_delay
        )
        if not rows:
            return None

        out: Dict[str, Dict[str, int]] = {}
        for row in rows:
            field = self.FINMIND_INST_FIELDS.get(row.get("name"))
            if not field:
                continue
            buy = int(row.get("buy") or 0)
            sell = int(row.get("sell") or 0)
            out[field] = {"buy": buy, "sell": sell, "net": buy - sell}

        if "foreign" not in out:
            logger.error("FinMind 三大法人缺外資欄位 %s", date_str)
            return None

        # 外資自營商常年為 0，FinMind 偶爾整天不給該列 → 補零而非整天丟掉
        zero = {"buy": 0, "sell": 0, "net": 0}
        return {f: out.get(f, zero) for f in self.FINMIND_INST_FIELDS.values()}

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

    def fetch_twse_margin(self, date_str: str, retries: int = 3, retry_delay: int = 30) -> Optional[Dict]:
        """
        Fetch TWSE (上市) margin aggregate（FinMind，單位：張／元）。

        只接受「剛好是所請求日期」的資料——`_finmind_rows_for_date` 以 start=end
        查詢，非交易日或尚未公布時回空，不會把前一日的數字誤標成 date_str。

        Returns:
          longBalance:  { change, buy, sell, balance }  — 張
          shortBalance: { change, buy, sell, balance }  — 張
          longAmount:   { change, buy, sell, balance }  — 元
        """
        rows = self._finmind_rows_for_date(
            self.FINMIND_MARGIN_DATASET, date_str, "融資融券", retries, retry_delay
        )
        if not rows:
            return None

        out: Dict[str, Dict[str, int]] = {}
        for row in rows:
            field = self.FINMIND_MARGIN_FIELDS.get(row.get("name"))
            if not field:
                continue
            today = int(row.get("TodayBalance") or 0)
            prev = int(row.get("YesBalance") or 0)
            out[field] = {
                "change": today - prev,
                "buy": int(row.get("buy") or 0),
                "sell": int(row.get("sell") or 0),
                "balance": today,
            }

        missing = set(self.FINMIND_MARGIN_FIELDS.values()) - out.keys()
        if missing:
            logger.error("FinMind 融資融券缺欄位 %s: %s", date_str, sorted(missing))
            return None
        return out

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

    def _finmind_rows_for_date(
        self, dataset: str, date_str: str, label: str, retries: int, retry_delay: int,
    ) -> Optional[List[Dict]]:
        """
        單日取數：以 start=end 查 FinMind，回傳該日的原始 rows。

        用 start=end 而非「抓一段再挑」是刻意的——FinMind 只會回這一天，
        非交易日或尚未公布時回空，呼叫端不會拿到前一日的數字卻標成 date_str
        （舊的 MI_MARGN 實作就是為了防這件事才禁止跨日 fallback）。

        Args:
            date_str: YYYYMMDD
        """
        d = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        return self._fetch_finmind_range(dataset, d, d, label, retries, retry_delay)

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
