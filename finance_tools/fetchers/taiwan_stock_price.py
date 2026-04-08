# finance_tools/fetchers/taiwan_stock_price.py
import requests
import pandas as pd
from datetime import timedelta
import os
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

from core.api_client import FinMindClient
from core.timezone import now_tw

def fetch_taiwan_stock_price_history(stock_id: str, client: FinMindClient, days: int = 90) -> pd.DataFrame:
    """
    Fetches historical Taiwan stock price data for a given stock_id from FinMind API via FinMindClient.
    Stores up to 'days' (default 90) of information.
    """
    end_date = now_tw()
    start_date = end_date - timedelta(days=days)
    start_date_str = start_date.strftime("%Y-%m-%d")

    try:
        df, success = client.fetch_taiwan_stock_price(stock_id, start_date_str)

        if success and not df.empty:
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                df = df.sort_values(by='date').reset_index(drop=True)
                logger.debug(f"Fetched {len(df)} records for {stock_id} from FinMind.")
                return df
            else:
                logger.warning(f"Fetched data for {stock_id} is missing 'date' column.")
                return pd.DataFrame()
        else:
            logger.warning(f"No data returned from FinMind for {stock_id} in the last {days} days.")
            return pd.DataFrame()

    except Exception as e:
        logger.error(f"Error fetching TaiwanStockPrice for {stock_id} from FinMind: {e}")
        return pd.DataFrame()

def update_stock_price_history_file(new_df: pd.DataFrame, stock_id: str, output_dir: str, days_to_keep: int = 90):
    """
    Updates the historical stock price file for a given stock_id.
    Merges new data with existing data, trims to days_to_keep, and saves to a single file.
    Filename: {stock_id}-price-history.json
    """
    if new_df.empty:
        logger.info(f"No new data to update for {stock_id}.")
        return

    os.makedirs(output_dir, exist_ok=True)
    file_path = os.path.join(output_dir, f"{stock_id}-price-history.json")

    existing_df = pd.DataFrame()
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                existing_data = json.load(f)
            if existing_data:
                existing_df = pd.DataFrame(existing_data)
                existing_df['date'] = pd.to_datetime(existing_df['date'])
        except (IOError, json.JSONDecodeError) as e:
            logger.warning(f"  警告: 無法讀取或解析公司 {stock_id} 的現有股價歷史檔案 {file_path}：{e}。將從頭開始創建。")
            existing_df = pd.DataFrame()

    combined_df = pd.concat([existing_df, new_df]).drop_duplicates(subset=['date', 'stock_id']).sort_values(by='date').reset_index(drop=True)

    if not combined_df.empty:
        combined_df = combined_df.tail(days_to_keep)

    if combined_df.empty:
        logger.info(f"  公司 {stock_id} 在保留 {days_to_keep} 天後沒有數據可儲存。")
        return

    df_to_save = combined_df.copy()
    df_to_save['date'] = df_to_save['date'].dt.strftime("%Y-%m-%d")

    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(df_to_save.to_dict(orient='records'), f, ensure_ascii=False, indent=2)
        logger.debug(f"  Successfully updated stock price history for {stock_id} to {file_path} (latest {len(df_to_save)} records).")
    except IOError as e:
        logger.error(f"  Error updating stock price history for {stock_id} to {file_path}: {e}")

def get_latest_price_from_df(df: pd.DataFrame) -> Optional[float]:
    """Extracts the latest closing price from a DataFrame."""
    if not df.empty and 'close' in df.columns:
        return df['close'].iloc[-1]
    return None