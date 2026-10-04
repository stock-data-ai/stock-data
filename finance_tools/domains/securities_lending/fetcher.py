"""
個股借券賣出餘額（上市＋上櫃），來源 FinMind `TaiwanDailyShortSaleBalances`。

**為什麼需要這支**：現有的 `margin_trading` 只有融資融券，那是散戶的信用交易。
法人放空多半走借券（融券有券源與平盤下限制），所以只看融資融券會在高價股上
得到「幾乎沒有空單」的錯覺——3324 雙鴻 2026-09-03 融券餘額 226 張，但借券賣出
餘額 3,211 張，差 14 倍，而後者在 App 裡原本完全看不到。

> [!CAUTION]
> **單位陷阱：這支資料集是「股」，`margin_trading` 那支是「張」。**
> 同一天的 3324：`TaiwanStockMarginPurchaseShortSale.ShortSaleTodayBalance` = 226（張），
> `TaiwanDailyShortSaleBalances.MarginShortSalesCurrentDayBalance` = 226000（股），
> 兩支資料集講的是同一個數字。本 fetcher **一律換算成張**輸出，與 company-financials
> 既有欄位一致；漏掉這一步會畫出 1000 倍的線，而且因為圖形長得一樣只是刻度不同，
> 目視很難發現。
>
> 換算後不保證是整數：借券賣出餘額因除權配股會有畸零股（2330 為 16,030,514 股
> ＝ 16,030.514 張），落檔採四捨五入，殘差 < 1 張。因此
> `sblBalance ≠ 前日餘額 + sblShortSales − sblReturns + sblAdjustments` 可能差 1 張，
> 這是捨入造成的，不是抓錯。

**授權**：與 `margin_trading` 同一條路（FinMind Sponsor Pro），且是盤後才公布的
日結資料，不在 08:30–14:30 的即時遮蔽區間內。證交所開放資料**沒有**借券賣出餘額
（`openapi.twse.com.tw/v1/SBL/TWT96U` 是「當日可借券賣出股數」＝剩餘可借量，不是餘額），
所以這支沒有「改走 OpenAPI」的遷移路徑；上櫃那半才有 TPEx 開放資料可對帳。

對帳紀錄（與櫃買開放資料 `www.tpex.org.tw/openapi/v1/tpex_margin_sbl` 逐欄位比對）：
- 2026-09-03：931 檔（全體上櫃）× 6 欄位（融券餘額、借券賣出餘額、當日借券賣出、
  還券、調整、可用額度）**零差異**，且 TPEx 側無任何 FinMind 缺漏的代號。
- 同日順帶確認 `SBLShortSalesQuota` 的語意是「**當日剩餘可借券賣出量**」而不是餘額上限：
  931 檔裡有 768 檔的借券賣出餘額大於它。欄位取名 `sblAvailable` 以免再被讀成上限。
"""

import logging
from typing import Optional

import pandas as pd

from finance_tools.utils.finmind import fetch_finmind

logger = logging.getLogger(__name__)


class SecuritiesLendingFetcher:
    """Fetcher for securities lending (借券賣出) balances（上市＋上櫃 一次取得）。"""

    DATASET = "TaiwanDailyShortSaleBalances"

    # 輸出欄位 → FinMind 欄位。只取借券（SBL）那一組；融券已由 margin_trading 落檔，
    # 兩邊各自維護同一個數字必然會漂移，所以這裡不重複輸出。
    FIELD_MAP = {
        "sbl_balance": "SBLShortSalesCurrentDayBalance",
        "sbl_short_sales": "SBLShortSalesShortSales",
        "sbl_returns": "SBLShortSalesReturns",
        "sbl_adjustments": "SBLShortSalesAdjustments",
        "sbl_available": "SBLShortSalesQuota",
    }

    SHARES_PER_LOT = 1000

    def fetch_all(self, date_str: str) -> Optional[pd.DataFrame]:
        """
        Args:
            date_str: YYYYMMDD

        Returns:
            DataFrame[stock_id, sbl_*]；當日無資料（假日或尚未公布）回 None。
            單位為「張」（原始為股，已除以 1000），與既有 company-financials 一致。
        """
        d = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        rows = fetch_finmind(self.DATASET, d, d, label="借券賣出餘額")
        if not rows:
            return None

        df = pd.DataFrame(rows)
        missing = {"stock_id", *self.FIELD_MAP.values()} - set(df.columns)
        if missing:
            logger.error("FinMind 借券賣出餘額缺欄位 %s: %s", date_str, sorted(missing))
            return None

        out = pd.DataFrame({"stock_id": df["stock_id"].astype(str)})
        for col, src in self.FIELD_MAP.items():
            out[col] = pd.to_numeric(df[src], errors="coerce") / self.SHARES_PER_LOT
        return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    fetcher = SecuritiesLendingFetcher()
    test_date = "20260903"

    print(f"Testing fetch_all for {test_date}...")
    combined_df = fetcher.fetch_all(test_date)
    if combined_df is not None:
        print(f"Total rows fetched: {len(combined_df)}")
        print(combined_df[combined_df["stock_id"].isin(["2330", "3324", "6488"])])
    else:
        print("No data.")
