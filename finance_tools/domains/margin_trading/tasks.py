import time
import logging
import pandas as pd
from datetime import timedelta
from typing import List, Dict, Any

from finance_tools.core import DataProcessor, FileManager
from finance_tools.core.timezone import now_tw, today_str
from finance_tools.domains.margin_trading.fetcher import MarginTradingFetcher
from finance_tools.utils.company_list_loader import load_companies_for_processing
import finance_tools.config as config

logger = logging.getLogger(__name__)

def run_update_margin_trading(args):
    """
    處理更新融資融券任務
    """
    logger.info("Handling margin trading update...")

    fetcher = MarginTradingFetcher()
    processor = DataProcessor()
    file_mgr = FileManager()

    # 確定要抓取的日期
    # 如果 args 中有指定日期則使用指定日期，否則使用最近的交易日（簡化起見先用今天/昨天）
    target_date = getattr(args, 'date', None)
    if not target_date:
        # 如果是凌晨，可能要抓昨天的資料
        now = now_tw()
        if now.hour < 18: # 證交所通常 17:00-18:00 更新
            target_date = (now - timedelta(days=1)).strftime("%Y%m%d")
        else:
            target_date = now.strftime("%Y%m%d")
    
    # 移除日期中的橫槓 (如果是 YYYY-MM-DD 轉為 YYYYMMDD)
    target_date = target_date.replace("-", "")
    
    logger.info(f"Target Date: {target_date}")

    # 1. 一次性抓取全市場資料 (上市 + 上櫃)
    combined_df = fetcher.fetch_all(target_date)
    
    if combined_df is None or combined_df.empty:
        logger.warning(f"No margin trading data found for {target_date}.")
        return

    logger.info(f"Fetched {len(combined_df)} records from TWSE/TPEx.")

    # 2. 載入需要處理的公司列表（用於過濾，避免處理不感興趣的股票）
    companies = load_companies_for_processing(args, file_mgr)
    company_codes = {c["code"] for c in companies} if companies else None

    success_count = 0
    
    # 3. 遍歷抓到的資料並更新對應的 JSON
    # 將 DataFrame 轉為 dict 加速查找
    records = combined_df.to_dict('records')
    
    for idx, row in enumerate(records, 1):
        code = str(row['stock_id']).strip()
        
        # 如果有指定公司列表且不在名單內，跳過
        if company_codes and code not in company_codes:
            continue

        # 讀取現有資料
        existing_data = file_mgr.load_financial_data(code)
        if not existing_data:
            # 如果 layer3 沒有該股票的檔案，視情況決定是否建立或跳過
            # 這裡選擇跳過，因為此專案通常以已存在的公司為主
            continue

        # 建立新的歷史紀錄點，放在 historical.marginTrading 裡
        formatted_date = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:]}"

        if "historical" not in existing_data:
            existing_data["historical"] = {}
        if "marginTrading" not in existing_data["historical"]:
            existing_data["historical"]["marginTrading"] = {}

        existing_data["historical"]["marginTrading"][formatted_date] = {
            "marginBuy": int(row['margin_buy']),
            "marginSell": int(row['margin_sell']),
            "marginBalance": int(row['margin_balance']),
            "shortBuy": int(row['short_buy']),
            "shortSell": int(row['short_sell']),
            "shortBalance": int(row['short_balance'])
        }
        
        # 同步更新最新狀態 (latest) 以便快速分析
        if "latest" not in existing_data:
            existing_data["latest"] = {}
        
        existing_data["latest"]["marginBalance"] = int(row['margin_balance'])
        existing_data["latest"]["shortBalance"] = int(row['short_balance'])
        
        # 存檔
        final_data = processor.clean_nan(existing_data)
        if file_mgr.save_financial_data(code, final_data):
            success_count += 1
            if success_count % 100 == 0:
                logger.info(f"Updated {success_count} companies...")

    logger.info(f"Successfully updated margin trading for {success_count} companies.")
