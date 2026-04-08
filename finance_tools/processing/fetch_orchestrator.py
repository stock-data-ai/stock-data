# finance_tools/processing/fetch_orchestrator.py
import logging
import time
import random
from typing import Dict, Any, Callable, Tuple, Optional
import pandas as pd

from core.api_client import FinMindClient, ApiExhaustedError

logger = logging.getLogger(__name__)


class FetchOrchestrator:
    """
    Coordinates fetching data from various sources.
    """

    def __init__(self,
                 finmind_client: FinMindClient,
                 financials_fetcher: Callable,
                 revenue_fetcher: Callable,
                 institutional_investors_fetcher: Callable,
                 institutional_investors_shares_fetcher: Callable,
                 shareholding_fetcher: Optional[Callable] = None):
        self.finmind_client = finmind_client
        self.financials_fetcher = financials_fetcher
        self.revenue_fetcher = revenue_fetcher
        self.institutional_investors_fetcher = institutional_investors_fetcher
        self.institutional_investors_shares_fetcher = institutional_investors_shares_fetcher
        self.shareholding_fetcher = shareholding_fetcher

    def fetch_financials(self, code: str, start_date: str) -> Tuple[list, list, bool]:
        logger.debug(f"正在擷取 {code} 的財務報表...")
        return self.financials_fetcher.fetch_and_process(code, start_date)

    def fetch_revenue(self, code: str, start_date: str) -> Tuple[pd.DataFrame, bool]:
        logger.debug(f"正在擷取 {code} 的營收資料...")
        return self.revenue_fetcher.fetch_and_process(code, start_date)

    def fetch_institutional_investors_ratios(self, code: str) -> Dict[str, Any]:
        """Fetches and returns institutional investor timeseries ratio data."""
        logger.debug(f"正在擷取 {code} 的三大法人持股比例...")
        raw_data = self.institutional_investors_fetcher(code)
        if not raw_data:
            logger.debug(f"找不到 {code} 的持股比例資料。")
            return {}
        if isinstance(raw_data, list):
            result = {}
            for r in raw_data:
                date = r.get("date")
                if not date:
                    continue
                entry = {k: v for k, v in r.items() if k != "date"}
                # Simple validation
                change_20 = entry.get("three_inst_ratio_change_20")
                if change_20 is not None and abs(change_20) > 5:
                    entry["three_inst_ratio_change_20"] = None
                result[date] = entry
            return result
        return raw_data

    def fetch_institutional_investors_shares(self, code: str, start_date: str) -> Tuple[Optional[pd.DataFrame], bool]:
        """Fetches institutional investor buy/sell share data."""
        logger.debug(f"正在擷取 {code} 的三大法人買賣超資料...")
        shares_df, success = self.institutional_investors_shares_fetcher(code, start_date)
        if not success or shares_df.empty:
            logger.debug(f"找不到 {code} 的買賣超資料。")
            return None, False
        return shares_df, True

    def fetch_shareholding(self, code: str, start_date: str) -> Tuple[pd.DataFrame, bool]:
        """Fetches foreign shareholding ratio data (TaiwanStockShareholding)."""
        if not self.shareholding_fetcher:
            logger.debug(f"shareholding_fetcher 未設定，跳過 {code}")
            return pd.DataFrame(), False
        logger.debug(f"正在擷取 {code} 的外資持股比例...")
        return self.shareholding_fetcher(code, start_date)

    def fetch_stock_price_for_market_cap(self, code: str) -> Optional[float]:
        """Fetches the latest stock price for market cap calculation."""
        from .financial_calculator import FinancialCalculator # Local import to avoid circular dependency
        from fetchers.taiwan_stock_price import fetch_taiwan_stock_price_history, get_latest_price_from_df

        logger.debug(f"正在為 {code} 擷取最新股價以計算市值...")
        try:
            # We fetch a short period as we only need the latest price
            df = fetch_taiwan_stock_price_history(
                stock_id=code,
                client=self.finmind_client,
                days=30 # Fetch last 30 days to be safe
            )
            if not df.empty:
                return get_latest_price_from_df(df)
            else:
                logger.warning(f"無法擷取 {code} 的股價。")
                return None
        except ApiExhaustedError:
            raise
        except Exception as e:
            logger.error(f"擷取 {code} 股價時發生錯誤： {e}")
            return None

