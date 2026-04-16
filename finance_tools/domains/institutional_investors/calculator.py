# finance_tools/processing/inst_ratio_calculator.py
"""
持股比例計算器。

- 外資比例：直接從 TaiwanStockShareholding（ForeignInvestmentSharesRatio）取得
- 投信/自營商比例：種子值 + 累積買賣超推估
  持股張數(t) = 持股張數(種子日) + Σ 買賣超(種子日 → t)
  持股比例(t) = 持股張數(t) / (issuedCommonShares / 1000) * 100
"""

import logging
from typing import Dict, Optional
import pandas as pd

logger = logging.getLogger(__name__)


class InstRatioCalculator:
    def __init__(self, seeds: Dict, companies_data: Dict):
        """
        Args:
            seeds: inst_ratio_seeds.json 的內容，key = stock code
            companies_data: companies-all.json 的內容，key = stock code
        """
        self.seeds = seeds
        self.companies_data = companies_data

    def calculate_foreign_ratio(self, shareholding_df: pd.DataFrame) -> Dict[str, float]:
        """
        從 TaiwanStockShareholding 直接取外資持股比例。

        Returns:
            {date: foreign_ratio} (百分比值，e.g. 78.5)
        """
        if shareholding_df.empty:
            return {}

        result = {}
        for _, row in shareholding_df.iterrows():
            date = str(row.get("date", ""))[:10]
            ratio = row.get("ForeignInvestmentSharesRatio")
            if date and ratio is not None:
                result[date] = float(ratio)
        return result

    def calculate_trust_dealer_ratio(
        self,
        code: str,
        shares_df: pd.DataFrame,
    ) -> Dict[str, Dict]:
        """
        從種子值 + 累積買賣超推估投信/自營商持股比例。

        Args:
            code: 股票代號
            shares_df: FinMind TaiwanStockInstitutionalInvestors 原始 DataFrame
                       欄位: date, name, buy, sell（單位：股）

        Returns:
            {date: {"trust_ratio": float, "dealer_ratio": float}}
        """
        seed = self.seeds.get(code, {})
        seed_date: Optional[str] = seed.get("seed_date")
        trust_shares: float = float(seed.get("trust_shares", 0))
        dealer_shares: float = float(seed.get("dealer_shares", 0))

        company = self.companies_data.get(code, {})
        issued_shares = (
            company.get("gov", {})
            .get("capital", {})
            .get("issuedCommonShares")
        )

        if not issued_shares or issued_shares <= 0:
            logger.debug(f"{code}: 無 issuedCommonShares，跳過 trust/dealer 比例計算")
            return {}

        # 總發行張數（issuedCommonShares 是股數，除以 1000 得張數）
        issued_lots = issued_shares / 1000

        if shares_df.empty:
            return {}

        df = shares_df.copy()
        df["date"] = df["date"].astype(str).str[:10]

        # 各日期的 trust / dealer 淨買超（張）
        # buy/sell 欄位單位是股，除以 1000 轉為張
        trust_mask = df["name"] == "Investment_Trust"
        dealer_mask = df["name"].isin(["Dealer_self", "Dealer_hedging"])

        trust_net = (
            df[trust_mask]
            .assign(net=lambda d: (d["buy"] - d["sell"]) / 1000)
            .groupby("date")["net"]
            .sum()
        )
        dealer_net = (
            df[dealer_mask]
            .assign(net=lambda d: (d["buy"] - d["sell"]) / 1000)
            .groupby("date")["net"]
            .sum()
        )

        # 只處理種子日期之後的資料（含種子日當天不重複計算）
        all_dates = sorted(set(trust_net.index) | set(dealer_net.index))
        if seed_date:
            all_dates = [d for d in all_dates if d > seed_date]

        result = {}
        running_trust = trust_shares
        running_dealer = dealer_shares

        for date in all_dates:
            running_trust += trust_net.get(date, 0.0)
            running_dealer += dealer_net.get(date, 0.0)
            # 避免負數（理論上不應發生，但做防護）
            running_trust = max(0.0, running_trust)
            running_dealer = max(0.0, running_dealer)

            result[date] = {
                "trust_ratio": round(running_trust / issued_lots * 100, 6),
                "dealer_ratio": round(running_dealer / issued_lots * 100, 6),
            }

        return result
