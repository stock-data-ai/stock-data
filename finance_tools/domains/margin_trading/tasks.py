import time
import logging
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict, Any

from finance_tools.core import DataProcessor, FileManager
from finance_tools.core.timezone import now_tw, today_str
from finance_tools.domains.margin_trading.fetcher import MarginTradingFetcher
from finance_tools.utils.company_list_loader import load_companies_for_processing
import finance_tools.config as config

logger = logging.getLogger(__name__)


def _trading_days_between(start_str: str, end_str: str) -> List[str]:
    """回傳 start～end 之間所有週一到週五的日期 (YYYYMMDD)，不含假日判斷（假日當天 TWSE 回空）。"""
    start = datetime.strptime(start_str, "%Y%m%d")
    end = datetime.strptime(end_str, "%Y%m%d")
    days = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # Mon-Fri
            days.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return days


def _process_one_date(target_date: str, fetcher, processor, file_mgr, company_codes) -> int:
    """抓單日全市場融資融券並寫入 JSON，回傳成功筆數。"""
    combined_df = fetcher.fetch_all(target_date)
    if combined_df is None or combined_df.empty:
        logger.warning(f"  [{target_date}] 無資料（假日或尚未公布）。")
        return 0

    formatted_date = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:]}"
    success_count = 0

    for row in combined_df.to_dict('records'):
        code = str(row['stock_id']).strip()
        if company_codes and code not in company_codes:
            continue

        existing_data = file_mgr.load_financial_data(code)
        if not existing_data:
            continue

        existing_data.setdefault("historical", {}).setdefault("marginTrading", {})[formatted_date] = {
            "marginBuy": int(row['margin_buy']),
            "marginSell": int(row['margin_sell']),
            "marginBalance": int(row['margin_balance']),
            "shortBuy": int(row['short_buy']),
            "shortSell": int(row['short_sell']),
            "shortBalance": int(row['short_balance']),
        }
        existing_data.setdefault("latest", {}).update({
            "marginBalance": int(row['margin_balance']),
            "shortBalance": int(row['short_balance']),
        })

        if file_mgr.save_financial_data(code, processor.clean_nan(existing_data)):
            success_count += 1

    logger.info(f"  [{target_date}] 完成 {success_count} 家公司。")
    return success_count


def run_update_margin_trading(args):
    """更新融資融券：支援單日（--date）或補齊區間（--backfill-from）。"""
    fetcher = MarginTradingFetcher()
    processor = DataProcessor()
    file_mgr = FileManager()

    companies = load_companies_for_processing(args, file_mgr)
    company_codes = {c["code"] for c in companies} if companies else None

    backfill_from = getattr(args, 'backfill_from', None)
    if backfill_from:
        # 補齊歷史區間
        start = backfill_from.replace("-", "")
        now = now_tw()
        end = (now - timedelta(days=1) if now.hour < 18 else now).strftime("%Y%m%d")
        dates = _trading_days_between(start, end)
        logger.info(f"補齊融資融券：{start} ～ {end}，共 {len(dates)} 個交易日。")
        total = 0
        for d in dates:
            total += _process_one_date(d, fetcher, processor, file_mgr, company_codes)
            time.sleep(2)
        logger.info(f"補齊完成，總計 {total} 筆。")
        return

    # 單日模式
    target_date = getattr(args, 'date', None)
    if not target_date:
        now = now_tw()
        target_date = (now - timedelta(days=1) if now.hour < 18 else now).strftime("%Y%m%d")
    target_date = target_date.replace("-", "")

    logger.info(f"Target Date: {target_date}")
    _process_one_date(target_date, fetcher, processor, file_mgr, company_codes)
