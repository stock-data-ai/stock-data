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


class TWSEInstitutionalFetcher:
    """
    一次取回全市場三大法人買賣超（上市 + 上櫃）。
    fetch_all(date_str) 最多 2 次 HTTP 呼叫，取代數千次 FinMind per-stock 請求。
    """

    TWSE_URL = "https://www.twse.com.tw/fund/T86?response=json&date={date}&selectType=ALL"
    TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"

    def fetch_all(self, date_str: str) -> Dict[str, InstitutionalRecord]:
        """
        Args:
            date_str: YYYYMMDD，e.g. '20260605'
        Returns:
            {code: InstitutionalRecord}  — 可能含上市 + 上櫃股票
        """
        result: Dict[str, InstitutionalRecord] = {}

        listed = self._fetch_listed(date_str)
        if listed is not None:
            result.update(listed)
            logger.info(f"TWSE T86: {len(listed)} 支上市股票 ({date_str})")
        else:
            logger.warning(f"TWSE T86: 無 {date_str} 資料（非交易日或尚未公布）")

        otc = self._fetch_otc()
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
    def _fetch_listed(self, date_str: str) -> Optional[Dict[str, InstitutionalRecord]]:
        url = self.TWSE_URL.format(date=date_str)
        try:
            resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            data = resp.json()

            if data.get("stat") != "OK" or not data.get("data"):
                return None

            # T86 欄位索引:
            # [0] code  [1] name
            # [2-4]  外陸資（不含外資自營商）: buy, sell, net
            # [5-7]  外資自營商: buy, sell, net
            # [8-10] 投信: buy, sell, net
            # [11]   自營商合計 net
            # [12-14] 自營商（自行買賣）: buy, sell, net
            # [15-17] 自營商（避險）: buy, sell, net
            # [18]   三大法人合計 net
            date_iso = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
            return {
                row[0].strip(): InstitutionalRecord(
                    date=date_iso,
                    Foreign_Investor_buy  = _parse_int(row[2]),
                    Foreign_Investor_sell = _parse_int(row[3]),
                    Foreign_Dealer_buy    = _parse_int(row[5]),
                    Foreign_Dealer_sell   = _parse_int(row[6]),
                    Investment_Trust_buy  = _parse_int(row[8]),
                    Investment_Trust_sell = _parse_int(row[9]),
                    Dealer_self_buy       = _parse_int(row[12]),
                    Dealer_self_sell      = _parse_int(row[13]),
                    Dealer_hedging_buy    = _parse_int(row[15]),
                    Dealer_hedging_sell   = _parse_int(row[16]),
                )
                for row in data["data"]
                if row[0].strip()
            }
        except Exception as e:
            logger.error(f"TWSE T86 error: {e}")
            return None

    # ──────────────────────────────────────────────────────────────
    # TPEx tpex_3insti_daily_trading  (上櫃)
    # ──────────────────────────────────────────────────────────────
    def _fetch_otc(self) -> Optional[Dict[str, InstitutionalRecord]]:
        try:
            req = urllib.request.Request(self.TPEX_URL, headers={"User-Agent": "Mozilla/5.0"})
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
