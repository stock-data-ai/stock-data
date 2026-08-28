"""
TWSE / TPEx 三大法人買賣超 — 一次全市場批次撈取。

上市 (TWSE): T86 API，selectType=ALL
上櫃 (TPEx): tpex_3insti_daily_trading OpenAPI

兩者都是「當日資料，一次返回全市場」，不需要 per-stock API 呼叫。
回傳 {code: InstitutionalRecord} 供 company_processor._build_ratios 使用。
"""
import json
import logging
import urllib.request
from typing import Dict, Optional

import requests

from finance_tools.utils.retry import retry as _retry
from finance_tools.utils.finmind import fetch_finmind

logger = logging.getLogger(__name__)

# Keys used by company_processor to build FinMind-compatible DataFrame
class InstitutionalRecord:
    __slots__ = (
        "date",
        "Foreign_Investor_buy", "Foreign_Investor_sell",
        "Foreign_Dealer_buy",   "Foreign_Dealer_sell",
        "Investment_Trust_buy", "Investment_Trust_sell",
        "Dealer_self_buy",      "Dealer_self_sell",
        "Dealer_hedging_buy",   "Dealer_hedging_sell",
    )

    def __init__(self, date: str, **kw):
        self.date = date
        for k in self.__slots__[1:]:
            setattr(self, k, kw.get(k, 0))

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


def _parse_int(val) -> int:
    try:
        return int(str(val).replace(",", "").strip())
    except (ValueError, AttributeError):
        return 0


def _roc_date_to_iso(roc: str) -> str:
    """'1150609' → '2026-06-09'"""
    try:
        year = int(roc[:3]) + 1911
        return f"{year}-{roc[3:5]}-{roc[5:7]}"
    except Exception:
        return ""


_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.tpex.org.tw/",
}


class TWSEInstitutionalFetcher:
    """
    一次取回全市場三大法人買賣超（上市 + 上櫃）。
    fetch_all(date_str) 最多 2 次 HTTP 呼叫，取代數千次 FinMind per-stock 請求。
    """

    FINMIND_DATASET = "TaiwanStockInstitutionalInvestorsBuySellWide"
    TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"

    def fetch_all(self, date_str: str) -> Dict[str, InstitutionalRecord]:
        """
        Args:
            date_str: YYYYMMDD，e.g. '20260605'
        Returns:
            {code: InstitutionalRecord}  — 可能含上市 + 上櫃股票
        """
        result: Dict[str, InstitutionalRecord] = {}

        listed = _retry(lambda: self._fetch_listed(date_str), f"TWSE T86 {date_str}")
        self.listed_ok = listed is not None
        if listed is not None:
            result.update(listed)
            logger.info(f"TWSE T86: {len(listed)} 支上市股票 ({date_str})")
        else:
            logger.warning(f"TWSE T86: 無 {date_str} 資料（非交易日或尚未公布）")

        otc = _retry(self._fetch_otc, "TPEx tpex_3insti_daily_trading")
        if otc is not None:
            result.update(otc)
            logger.info(f"TPEx: {len(otc)} 支上櫃股票")
        else:
            logger.warning("TPEx tpex_3insti_daily_trading: 無法取得資料")

        logger.info(f"三大法人合計: {len(result)} 支股票")
        return result

    # ──────────────────────────────────────────────────────────────
    # TWSE T86  (上市)
    # ──────────────────────────────────────────────────────────────
    # FinMind Wide → InstitutionalRecord。Wide 版一檔一列（24K 筆），long 版一檔五列
    # （121K 筆、10.4MB），內容相同但體積差一倍，沒有理由用 long。
    FINMIND_FIELDS = {
        "Foreign_Investor_buy":  "Foreign_Investor_buy",
        "Foreign_Investor_sell": "Foreign_Investor_sell",
        "Foreign_Dealer_buy":    "Foreign_Dealer_Self_buy",
        "Foreign_Dealer_sell":   "Foreign_Dealer_Self_sell",
        "Investment_Trust_buy":  "Investment_Trust_buy",
        "Investment_Trust_sell": "Investment_Trust_sell",
        "Dealer_self_buy":       "Dealer_self_buy",
        "Dealer_self_sell":      "Dealer_self_sell",
        "Dealer_hedging_buy":    "Dealer_Hedging_buy",
        "Dealer_hedging_sell":   "Dealer_Hedging_sell",
    }

    def _fetch_listed(self, date_str: str) -> Optional[Dict[str, InstitutionalRecord]]:
        """
        全市場個股三大法人買賣超（FinMind，單位：股）。

        **不再抓 www.twse.com.tw/fund/T86**：2026-08-26 證交所來函後移除，那是其網站條款
        明文禁止爬取的網頁端點（與 openapi.twse.com.tw 的開放資料不同）。

        > [!WARNING]
        > FinMind 這支同時涵蓋上市與上櫃，但**上櫃的 dealer_self／dealer_hedging 與櫃買
        > 自己的 OpenAPI 對不起來**（2026-08-27 實測 531 檔上櫃有此差異，兩邊的自營商
        > 自行買賣與避險像是互換）。所以 `_fetch_otc` 不可省略——`fetch_all` 讓它在後面
        > 覆蓋上櫃，上櫃一律以櫃買官方為準。看到「FinMind 已含上櫃」就把 `_fetch_otc`
        > 拿掉的話，531 檔上櫃的自營商資料會靜默變成另一個數字。

        對帳紀錄：2026-08-27 以開放資料的上市名單限縮後，1,075 檔 × 10 欄位全部相同。
        """
        d = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        rows = fetch_finmind(self.FINMIND_DATASET, d, d, label="個股三大法人")
        if not rows:
            return None

        date_iso = d
        result: Dict[str, InstitutionalRecord] = {}
        for row in rows:
            code = str(row.get("stock_id", "")).strip()
            if not code:
                continue
            result[code] = InstitutionalRecord(
                date=date_iso,
                **{ours: _parse_int(row.get(theirs)) for ours, theirs in self.FINMIND_FIELDS.items()},
            )
        return result or None

    # ──────────────────────────────────────────────────────────────
    # TPEx tpex_3insti_daily_trading  (上櫃)
    # ──────────────────────────────────────────────────────────────
    def _fetch_otc(self) -> Optional[Dict[str, InstitutionalRecord]]:
        try:
            req = urllib.request.Request(self.TPEX_URL, headers=_BROWSER_HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not data:
                return None

            date_iso = _roc_date_to_iso(data[0].get("Date", ""))
            result: Dict[str, InstitutionalRecord] = {}
            for rec in data:
                code = rec.get("SecuritiesCompanyCode", "").strip()
                if not code:
                    continue
                # TPEx 欄位名稱有空白/拼字問題，使用精確 key
                # 外資合計（含外資自營商）= ForeignInvestorsIncludeMainlandAreaInvestors-TotalBuy
                # TPEx 無自行買賣/避險拆分 → 全部放入 Dealer_self
                result[code] = InstitutionalRecord(
                    date=date_iso,
                    Foreign_Investor_buy  = _parse_int(rec.get("ForeignInvestorsIncludeMainlandAreaInvestors-TotalBuy", 0)),
                    Foreign_Investor_sell = _parse_int(rec.get("ForeignInvestorsIncludeMainlandAreaInvestors-TotalSell", 0)),
                    Foreign_Dealer_buy    = _parse_int(rec.get("Foreign Dealers-Total Buy", 0)),
                    Foreign_Dealer_sell   = _parse_int(rec.get("Foreign Dealers-TotalSell", 0)),
                    Investment_Trust_buy  = _parse_int(rec.get("SecuritiesInvestmentTrustCompanies-TotalBuy", 0)),
                    Investment_Trust_sell = _parse_int(rec.get("SecuritiesInvestmentTrustCompanies-TotalSell", 0)),
                    Dealer_self_buy       = _parse_int(rec.get("Dealers-TotalBuy", 0)),
                    Dealer_self_sell      = _parse_int(rec.get("Dealers-TotalSell", 0)),
                    Dealer_hedging_buy    = 0,
                    Dealer_hedging_sell   = 0,
                )
            return result
        except Exception as e:
            logger.error(f"TPEx tpex_3insti_daily_trading error: {e}")
            return None
