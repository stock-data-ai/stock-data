"""
Revenue Fetcher - 月營收數據抓取器
"""

import logging
from typing import Tuple, List, Dict
import pandas as pd
from core import FinMindClient, DataProcessor

logger = logging.getLogger(__name__)


class RevenueFetcher:
    """月營收抓取器"""

    def __init__(self, client: FinMindClient, processor: DataProcessor):
        self.client = client
        self.processor = processor

    def fetch_and_process(
        self, stock_id: str, start_date: str
    ) -> Tuple[List[Dict], bool]:
        """
        抓取並處理月營收數據

        Args:
            stock_id: 股票代碼
            start_date: 起始日期

        Returns:
            (monthly_revenue_data, success)
        """
        # 1. 抓取原始數據
        df, success = self.client.fetch_monthly_revenue(stock_id, start_date)

        if not success or df.empty:
            logger.warning(f"  No monthly revenue data for {stock_id}")
            return [], False

        # 2. 處理數據（單位：元，保持不變）
        monthly_data = self.processor.process_monthly_revenue(stock_id, df)

        return monthly_data, True
