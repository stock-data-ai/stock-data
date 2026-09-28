# finance_tools/processing/financial_calculator.py
import logging
from typing import Dict, Any, Optional
import pandas as pd

logger = logging.getLogger(__name__)


class FinancialCalculator:
    """
    Handles pure financial calculations.
    """

    @staticmethod
    def calculate_yoy_and_update_block(latest_block: Dict, latest_quarter: Dict, quarterly_data: list):
        """Updates the 'latest' data block with the most recent quarter's info and YoY."""
        # 找不到去年同季就是「沒有年增率」（None），**不是 0%**——0 會被畫面讀成「持平」。
        # 這裡只看本次抓取視窗；合併完整歷史後 build_final_data 會用 quarter_yoy() 重算一次。
        quarter_yoy = None
        prev_year_quarter_data = next((q for q in quarterly_data if q['year'] == latest_quarter['year'] - 1 and q['quarter'] == latest_quarter['quarter']), None)

        if prev_year_quarter_data:
            prev_revenue = prev_year_quarter_data.get("revenue") or 0
            curr_revenue = latest_quarter.get("revenue") or 0
            if prev_revenue > 0 and curr_revenue:
                quarter_yoy = round(((curr_revenue - prev_revenue) / prev_revenue) * 100, 1)

        latest_block.update({
            "year": latest_quarter.get("year"),
            "quarter": latest_quarter.get("quarter"),
            "revenue": latest_quarter.get("revenue", 0),
            "yoy": quarter_yoy,
            "grossMargin": latest_quarter.get("grossMargin", 0),
            "operatingMargin": latest_quarter.get("operatingMargin", 0),
            "netMargin": latest_quarter.get("netMargin", 0),
            "eps": latest_quarter.get("eps", 0),
        })
        logger.debug(f"Calculated latest block with YoY: {quarter_yoy}")

    @staticmethod
    def quarter_yoy(quarterly: list, year, quarter) -> Optional[float]:
        """用**合併後的完整季度歷史**算指定季的營收年增率（%，一位小數）；缺去年同季回 None。

        每日增量只抓約一年的視窗，去年同季常常不在視窗裡——以前在視窗內算、找不到就填 0，
        2026-09-28 稽核時 2,343 家台股有 2,121 家的 latest.yoy 是 0（台積電實際約 +40%）。
        """
        if not quarterly or year is None or quarter is None:
            return None
        rev = {(q.get('year'), q.get('quarter')): q.get('revenue') for q in quarterly}
        curr, prev = rev.get((year, quarter)), rev.get((year - 1, quarter))
        if not curr or not prev or prev <= 0:
            return None
        return round((curr - prev) / prev * 100, 1)

    @staticmethod
    def fill_monthly_yoy(monthly: Optional[list]) -> Optional[list]:
        """用**合併後的完整月營收**重算每一筆的 yoy（%，兩位小數）；缺去年同月為 None。

        抓取視窗只有一年，視窗內算的 yoy 幾乎全是 None（稽核時 2,339 家有 2,209 家整份沒有值），
        取用端（營收標籤、持股健檢、AI 分析）因此永遠拿不到。一律在合併之後算。
        """
        if not monthly:
            return monthly
        rev = {(m.get('year'), m.get('month')): m.get('revenue') for m in monthly}
        for m in monthly:
            curr, prev = m.get('revenue'), rev.get((m.get('year', 0) - 1, m.get('month')))
            m['yoy'] = round((curr - prev) / prev * 100, 2) if (curr and prev and prev > 0) else None
        return monthly

    @staticmethod
    def calculate_market_cap(latest_price: Optional[float], issued_shares: Optional[float]) -> float:
        """Calculates market cap from latest price and issued shares."""
        if latest_price is not None and issued_shares and issued_shares > 0:
            market_cap = round(latest_price * issued_shares, 2)
            logger.debug(f"Calculated market cap: {market_cap}")
            return market_cap
        return 0.0

    @staticmethod
    def calculate_institutional_investors_net_buy(shares_df: pd.DataFrame) -> pd.DataFrame:
        """Calculates net buy/sell for institutional investors from a DataFrame."""
        if shares_df.empty:
            return shares_df

        df = shares_df.copy()
        df['buy'] = df['buy'] / 1000
        df['sell'] = df['sell'] / 1000
        df['net_buy'] = df['buy'] - df['sell']

        name_map = {
            'Foreign_Investor': 'foreign',
            'Foreign_Dealer':   'foreign_dealer',  # 外資自營商（T86 col[5-7]）
            'Investment_Trust': 'trust',
            'Dealer_self':      'dealer_self',
            'Dealer_hedging':   'dealer_hedging',
        }
        df['inst_type'] = df['name'].map(name_map)
        df.dropna(subset=['inst_type'], inplace=True)

        pivot_df = df.pivot_table(index='date', columns='inst_type', values=['buy', 'sell', 'net_buy'])
        pivot_df.columns = [f'{val[1]}_{val[0]}' for val in pivot_df.columns]

        pivot_df['dealer_buy'] = pivot_df.get('dealer_self_buy', 0) + pivot_df.get('dealer_hedging_buy', 0)
        pivot_df['dealer_sell'] = pivot_df.get('dealer_self_sell', 0) + pivot_df.get('dealer_hedging_sell', 0)
        pivot_df['dealer_net_buy'] = pivot_df.get('dealer_self_net_buy', 0) + pivot_df.get('dealer_hedging_net_buy', 0)

        logger.debug("Calculated institutional investors net buy.")
        return pivot_df

