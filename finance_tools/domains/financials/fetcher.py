"""
Financials Fetcher - 財務報表數據抓取器（損益表）
資產負債表 + 現金流量表由 balance_sheet/tasks.py 的獨立工作流處理。
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
        抓取並處理損益表數據。

        Returns:
            (annual_data, quarterly_data, success)
        """
        logger.debug(f"  Fetching financial statements for {stock_id}...")

        fs_df, success = self.client.fetch_financial_statements(stock_id, start_date)
        if not success or fs_df.empty:
            logger.warning(f"  Failed to fetch financial statements for {stock_id}")
            return [], [], False

        annual_data, quarterly_data = self.processor.process_financials(stock_id, fs_df)
        return annual_data, quarterly_data, True
