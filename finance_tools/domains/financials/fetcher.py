"""
Financials Fetcher - 財務報表數據抓取器
"""

import logging
from typing import Tuple, List, Dict
import pandas as pd
from finance_tools.core import FinMindClient, DataProcessor

logger = logging.getLogger(__name__)


class FinancialsFetcher:
    """財務報表抓取器"""

    def __init__(self, client: FinMindClient, processor: DataProcessor):
        self.client = client
        self.processor = processor

    def fetch_and_process(
        self, stock_id: str, start_date: str
    ) -> Tuple[List[Dict], List[Dict], bool]:
        """
        抓取並處理財務報表數據

        Args:
            stock_id: 股票代碼
            start_date: 起始日期

        Returns:
            (annual_data, quarterly_data, success)
        """
        logger.debug(f"  Fetching financial statements for {stock_id}...")

        # 1. 抓取原始數據
        df, success = self.client.fetch_financial_statements(stock_id, start_date)

        if not success or df.empty:
            logger.warning(f"  Failed to fetch financial statements for {stock_id}")
            return [], [], False

        # 2. 處理數據
        annual_data, quarterly_data = self.processor.process_financials(stock_id, df)

        return annual_data, quarterly_data, True
