# finance_tools/processing/fetch_orchestrator.py
import logging
from typing import Dict, Any, Callable, Tuple, Optional
import pandas as pd

from finance_tools.core.api_client import FinMindClient
from finance_tools.domains.valuation.yahoo_fetcher import YahooFetcher

logger = logging.getLogger(__name__)


class FetchOrchestrator:
    """
    Coordinates fetching data from various sources.
    """

    def __init__(self,
                 finmind_client: FinMindClient,
                 financials_fetcher: Callable,
                 revenue_fetcher: Callable,
                 institutional_investors_shares_fetcher: Callable,
                 shareholding_fetcher: Optional[Callable] = None):
        self.finmind_client = finmind_client
        self.financials_fetcher = financials_fetcher
        self.revenue_fetcher = revenue_fetcher
        self.institutional_investors_shares_fetcher = institutional_investors_shares_fetcher
        self.shareholding_fetcher = shareholding_fetcher
        self._yahoo = YahooFetcher()

    def fetch_financials(self, code: str, start_date: str) -> Tuple[list, list, bool]:
        logger.debug(f"正在擷取 {code} 的財務報表...")
        return self.financials_fetcher.fetch_and_process(code, start_date)

    def fetch_revenue(self, code: str, start_date: str) -> Tuple[pd.DataFrame, bool]:
        logger.debug(f"正在擷取 {code} 的營收資料...")
        return self.revenue_fetcher.fetch_and_process(code, start_date)

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

    def fetch_valuation_stats(self, code: str) -> Dict[str, Any]:
        """Fetches full valuation stats (market cap, PE, PB, Yield) from Yahoo Finance."""
        logger.debug(f"正在為 {code} 從 Yahoo 擷取估值數據...")
        try:
            return self._yahoo.fetch_market_stats(code)
        except Exception as e:
            logger.error(f"從 Yahoo 擷取 {code} 估值時發生錯誤： {e}")
            return {}


