"""
Fetcher for institutional investors' share trading data from FinMind.
"""

import logging
import pandas as pd
from typing import Tuple
from finance_tools.core.api_client import FinMindClient

logger = logging.getLogger(__name__)

def fetch_institutional_investors_shares(
    client: FinMindClient, stock_id: str, start_date: str
) -> Tuple[pd.DataFrame, bool]:
    """
    Fetches institutional investors' buy and sell data for a given stock.

    Args:
        stock_id (str): The stock identifier.
        start_date (str): The start date for the data fetch in 'YYYY-MM-DD' format.

    Returns:
        A tuple containing a pandas DataFrame with the data and a boolean indicating success.
    """

    df, success = client.fetch_institutional_investors(stock_id, start_date)

    if not success:
         return pd.DataFrame(), False

    if df.empty:
         logger.warning(f"  找不到 {stock_id} 的三大法人買賣超資料。")
         return pd.DataFrame(), True # Treat no data as valid result (empty DF)

    return df, True