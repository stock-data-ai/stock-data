"""
Fetcher for foreign shareholding ratio data from FinMind.
Dataset: TaiwanStockShareholding
"""

import logging
import pandas as pd
from typing import Tuple
from core.api_client import FinMindClient

logger = logging.getLogger(__name__)


def fetch_shareholding(
    client: FinMindClient, stock_id: str, start_date: str
) -> Tuple[pd.DataFrame, bool]:
    """
    抓取外資持股比例資料（TaiwanStockShareholding）。

    回傳欄位包含 date, foreignInvestmentSharesPercent。
    """
    df, success = client.fetch_shareholding(stock_id, start_date)

    if not success:
        return pd.DataFrame(), False

    if df.empty:
        logger.warning(f"  找不到 {stock_id} 的外資持股比例資料。")
        return pd.DataFrame(), True

    return df, True
