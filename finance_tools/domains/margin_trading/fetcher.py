"""
個股融資融券（上市＋上櫃），來源 FinMind `TaiwanStockMarginPurchaseShortSale`。

**不再直接抓證交所與櫃買網站**：2026-08-26 證交所來函後移除。原本走
`www.twse.com.tw/exchangeReport/MI_MARGN` 與
`www.tpex.org.tw/.../margin_bal_result.php` 兩支網頁端點，屬其網站使用條款明文
禁止爬取的範圍（與 openapi.twse.com.tw 的開放資料不同）。移除前的實作見 git history。

FinMind 一支資料集同時涵蓋上市與上櫃，所以原本 fetch_twse／fetch_tpex 兩條路
收斂成一次請求；權證等非個股代號一併回傳，由呼叫端的 company_codes 過濾。

對帳紀錄（切換前逐欄位比對，零差異）：
- 2026-08-27：378 檔 × 6 欄位（marginBuy/Sell/Balance、shortBuy/Sell/Balance）
  與既有 company-financials 完全相同，含上櫃（6488、5483、3324）
"""

import logging
from typing import Optional

import pandas as pd

from finance_tools.utils.finmind import fetch_finmind

logger = logging.getLogger(__name__)


class MarginTradingFetcher:
    """Fetcher for margin trading data (上市＋上櫃 一次取得)。"""

    DATASET = "TaiwanStockMarginPurchaseShortSale"

    # 輸出欄位 → FinMind 欄位。
    # 融券的 buy/sell 不可對調：buy 是回補、sell 是放空，換位置會讓多空整個顛倒。
    # 2026-08-27 以 2317（shortBuy 17／shortSell 45）等不對稱樣本驗證過方向正確。
    FIELD_MAP = {
        "margin_buy": "MarginPurchaseBuy",
        "margin_sell": "MarginPurchaseSell",
        "margin_repay": "MarginPurchaseCashRepayment",
        "margin_balance": "MarginPurchaseTodayBalance",
        "short_buy": "ShortSaleBuy",
        "short_sell": "ShortSaleSell",
        "short_repay": "ShortSaleCashRepayment",
        "short_balance": "ShortSaleTodayBalance",
    }

    def fetch_all(self, date_str: str) -> Optional[pd.DataFrame]:
        """
        Args:
            date_str: YYYYMMDD

        Returns:
            DataFrame[stock_id, margin_*, short_*]；當日無資料（假日或尚未公布）回 None。
            單位為「張」，與既有 company-financials 一致。
        """
        d = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        rows = fetch_finmind(self.DATASET, d, d, label="融資融券")
        if not rows:
            return None

        df = pd.DataFrame(rows)
        missing = {"stock_id", *self.FIELD_MAP.values()} - set(df.columns)
        if missing:
            logger.error("FinMind 融資融券缺欄位 %s: %s", date_str, sorted(missing))
            return None

        out = pd.DataFrame({"stock_id": df["stock_id"].astype(str)})
        for col, src in self.FIELD_MAP.items():
            out[col] = pd.to_numeric(df[src], errors="coerce")
        return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    fetcher = MarginTradingFetcher()
    test_date = "20260827"

    print(f"Testing fetch_all for {test_date}...")
    combined_df = fetcher.fetch_all(test_date)
    if combined_df is not None:
        print(f"Total rows fetched: {len(combined_df)}")
        print(combined_df.head())
        otc_stock = combined_df[combined_df["stock_id"] == "6488"]
        if not otc_stock.empty:
            print("\nOTC Stock (6488) Found:")
            print(otc_stock.to_dict("records")[0])
    else:
        print("Fetch failed")
