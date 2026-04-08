import logging
import pandas as pd
from datetime import timedelta
from typing import Tuple, Optional
from core.api_client import FinMindClient
from core.timezone import now_tw

logger = logging.getLogger(__name__)

def fetch_per_pbr(stock_id: str, client: FinMindClient) -> Tuple[Optional[pd.Series], bool]:
    """
    Fetches the latest P/E and P/B ratio for a given stock ID using FinMindClient.
    Returns (latest_row_as_Series, success) or (None, False) on failure.
    """
    logger.debug(f"  正在抓取 {stock_id} 的 P/E P/B 數據...")
    start_date = (now_tw() - timedelta(days=30)).strftime("%Y-%m-%d")

    try:
        df, success = client.fetch_per_pbr(stock_id, start_date)

        if not success:
            return None, False

        if df.empty:
            logger.warning(f"  未找到 {stock_id} 的 P/E P/B 數據。")
            return None, True

        return df.iloc[-1], True

    except Exception as e:
        logger.error(f"  抓取 {stock_id} 的 P/E P/B 數據時發生未預期錯誤: {e}")
        return None, False