"""
TWSE / TPEx 外資持股比例 — 一次全市場批次撈取。

上市: FinMind TaiwanStockShareholding
上櫃 (TPEx): tpex_3insti_qfii OpenAPI

**上市不再抓 www.twse.com.tw/rwd/zh/fund/MI_QFIIS**：2026-08-26 證交所來函後移除，
那是其網站條款明文禁止爬取的網頁端點（與 openapi.twse.com.tw 的開放資料不同）。
對帳紀錄：2026-08-27 上市 1,075 檔外資持股比率與既有資料完全相同。

回傳 {code: {"ratio": float, "shares": int}} — 比率是百分比、shares 是持股張數，
e.g. {"2330": {"ratio": 70.12, "shares": 17945329}}

**張數是官方揭露值，不是用比率乘發行股數推的。** 兩邊來源都直接給股數
（FinMind ForeignInvestmentShares、TPEx CurrentlySharesOC/FIHeld），這裡只做
股→張的換算。用發行股數回推會踩到 CLAUDE.md 記過的那個坑：發行股數來源不一致
會讓下游（處置預警等）跟著錯，而且錯得很安靜。
"""
import json
import logging
from datetime import datetime, timedelta, timezone
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


def _to_lots(raw) -> Optional[int]:
    """外資持股股數 → 張數。兩邊來源都是股，且可能是帶千分位的字串。"""
    if raw is None:
        return None
    try:
        return int(round(float(str(raw).replace(",", "")) / 1000))
    except (TypeError, ValueError):
        return None


class TWSEShareholdingFetcher:
    """
    一次取回全市場外資持股比例（上市 + 上櫃）。
    取代 FinMind TaiwanStockShareholding per-stock 請求。
    """

    FINMIND_DATASET = "TaiwanStockShareholding"
    # 往回找幾天才放棄。原本的 MI_QFIIS 沒有 date 參數、永遠回「最新一筆」，
    # FinMind 則要指定日期，所以自己往回走來還原同樣的語意。
    LOOKBACK_DAYS = 7
    TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_qfii"

    def fetch_all(self) -> Dict[str, Dict[str, float]]:
        """
        Returns:
            {code: {"ratio": pct, "shares": lots}}
            e.g. {"2330": {"ratio": 70.12, "shares": 17945329}}
        """
        result: Dict[str, Dict[str, float]] = {}

        listed = _retry(self._fetch_listed, "FinMind 外資持股")
        if listed is not None:
            result.update(listed)
            logger.info(f"FinMind 外資持股: {len(listed)} 支上市股票")
        else:
            logger.warning("FinMind 外資持股: 無法取得資料")

        otc = _retry(self._fetch_otc, "TPEx tpex_3insti_qfii")
        if otc is not None:
            result.update(otc)
            logger.info(f"TPEx tpex_3insti_qfii: {len(otc)} 支上櫃股票外資持股比例")
        else:
            logger.warning("TPEx tpex_3insti_qfii: 無法取得資料")

        logger.info(f"外資持股合計: {len(result)} 支股票（比率＋張數）")
        return result

    def _fetch_listed(self) -> Optional[Dict[str, Dict[str, float]]]:
        """
        最新一日的全體外資及陸資持股比率（FinMind）。

        **逐日往回找、不用區間查詢**：TaiwanStockShareholding 帶 start≠end 會回空
        （2026-08-28 實測 08-22~08-28 回 0 筆，但同區間內每一天單獨查都有 2,369 筆），
        改成從今天往回逐日試，拿到第一個有資料的日期就停——這也還原了 MI_QFIIS
        「永遠給最新一筆」的語意。
        """
        today = datetime.now(timezone(timedelta(hours=8))).date()
        for back in range(self.LOOKBACK_DAYS):
            d = (today - timedelta(days=back)).isoformat()
            rows = fetch_finmind(
                self.FINMIND_DATASET, d, d, label="外資持股", retries=1, retry_delay=0, quiet=True
            )
            if not rows:
                continue
            out = {}
            for row in rows:
                code = str(row.get("stock_id", "")).strip()
                ratio = row.get("ForeignInvestmentSharesRatio")
                if code and ratio is not None:
                    try:
                        entry = {"ratio": float(ratio)}
                    except (TypeError, ValueError):
                        continue
                    shares = _to_lots(row.get("ForeignInvestmentShares"))
                    if shares is not None:
                        entry["shares"] = shares
                    out[code] = entry
            if out:
                logger.info("FinMind 外資持股：採用 %s 的資料（%d 檔）", d, len(out))
                return out
        logger.error("FinMind 外資持股：往回 %d 天都沒有資料", self.LOOKBACK_DAYS)
        return None

    def _fetch_otc(self) -> Optional[Dict[str, Dict[str, float]]]:
        try:
            req = urllib.request.Request(self.TPEX_URL, headers=_BROWSER_HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            # 欄位: SecuritiesCompanyCode、PercentageOfSharesOC/FMIHeld = "87.8%"、
            #       CurrentlySharesOC/FIHeld = 外資持有股數（字串，可能帶千分位）
            out: Dict[str, Dict[str, float]] = {}
            for rec in data:
                code = (rec.get("SecuritiesCompanyCode") or "").strip()
                pct = rec.get("PercentageOfSharesOC/FMIHeld")
                if not code or not pct:
                    continue
                try:
                    entry = {"ratio": float(str(pct).replace("%", "").replace(",", ""))}
                except (TypeError, ValueError):
                    continue
                shares = _to_lots(rec.get("CurrentlySharesOC/FIHeld"))
                if shares is not None:
                    entry["shares"] = shares
                out[code] = entry
            return out
        except Exception as e:
            logger.error(f"TPEx tpex_3insti_qfii error: {e}")
            return None
