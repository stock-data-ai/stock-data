"""
TWSE / TPEx 外資持股比例 — 一次全市場批次撈取。

上市 (TWSE): MI_QFIIS，selectType=ALLBUT0999
上櫃 (TPEx): tpex_3insti_qfii OpenAPI

回傳 {code: float} — 外資持股比率百分比，e.g. {"2330": 70.12}
"""
import json
import logging
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


class TWSEShareholdingFetcher:
    """
    一次取回全市場外資持股比例（上市 + 上櫃）。
    取代 FinMind TaiwanStockShareholding per-stock 請求。
    """

    TWSE_URL = "https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS?response=json&selectType=ALLBUT0999"
    TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_qfii"

    def fetch_all(self) -> Dict[str, float]:
        """
        Returns:
            {code: foreign_ratio_pct}  e.g. {"2330": 70.12, "3711": 45.3}
        """
        result: Dict[str, float] = {}

        listed = _retry(self._fetch_listed, "TWSE MI_QFIIS")
        if listed is not None:
            result.update(listed)
            logger.info(f"TWSE MI_QFIIS: {len(listed)} 支上市股票外資持股比例")
        else:
            logger.warning("TWSE MI_QFIIS: 無法取得資料")

        otc = _retry(self._fetch_otc, "TPEx tpex_3insti_qfii")
        if otc is not None:
            result.update(otc)
            logger.info(f"TPEx tpex_3insti_qfii: {len(otc)} 支上櫃股票外資持股比例")
        else:
            logger.warning("TPEx tpex_3insti_qfii: 無法取得資料")

        logger.info(f"外資持股比例合計: {len(result)} 支股票")
        return result

    def _fetch_listed(self) -> Optional[Dict[str, float]]:
        try:
            resp = requests.get(_bust(self.TWSE_URL), timeout=20, headers=_BROWSER_HEADERS)
            resp.raise_for_status()
            data = resp.json()
            if data.get("stat") != "OK":
                return None
            # 欄位 [0]=code, [7]=全體外資及陸資持股比率（已為 float，e.g. 70.12）
            return {
                row[0].strip(): float(row[7])
                for row in data.get("data", [])
                if row[0].strip() and row[7] is not None
            }
        except Exception as e:
            logger.error(f"TWSE MI_QFIIS error: {e}")
            return None

    def _fetch_otc(self) -> Optional[Dict[str, float]]:
        try:
            req = urllib.request.Request(self.TPEX_URL, headers=_BROWSER_HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            # 欄位: SecuritiesCompanyCode, PercentageOfSharesOC/FMIHeld = "87.8%"
            return {
                rec["SecuritiesCompanyCode"].strip(): float(rec["PercentageOfSharesOC/FMIHeld"].replace("%", ""))
                for rec in data
                if rec.get("SecuritiesCompanyCode") and rec.get("PercentageOfSharesOC/FMIHeld")
            }
        except Exception as e:
            logger.error(f"TPEx tpex_3insti_qfii error: {e}")
            return None
