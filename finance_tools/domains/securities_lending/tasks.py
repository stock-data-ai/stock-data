"""借券賣出餘額更新任務。結構刻意與 margin_trading/tasks.py 對齊，兩者同源同節奏。"""

import sys
import time
import logging
import math
from datetime import datetime, timedelta
from typing import Dict, List

from finance_tools.core import DataProcessor, FileManager
from finance_tools.core.timezone import now_tw
from finance_tools.core.trading_day import is_tw_trading_day, parse_yyyymmdd
from finance_tools.domains.securities_lending.fetcher import SecuritiesLendingFetcher
from finance_tools.utils.company_list_loader import load_companies_for_processing

logger = logging.getLogger(__name__)

# 落檔欄位 → fetcher 欄位。單位皆為張（fetcher 已由股換算）。
_RECORD_FIELDS = {
    "sblBalance": "sbl_balance",
    "sblShortSales": "sbl_short_sales",
    "sblReturns": "sbl_returns",
    "sblAdjustments": "sbl_adjustments",
    "sblAvailable": "sbl_available",
}


def _trading_days_between(start_str: str, end_str: str) -> List[str]:
    """回傳 start～end 之間所有週一到週五的日期 (YYYYMMDD)，不含假日判斷（假日當天回空）。"""
    start = datetime.strptime(start_str, "%Y%m%d")
    end = datetime.strptime(end_str, "%Y%m%d")
    days = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # Mon-Fri
            days.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return days


def _to_lots(value) -> int:
    """張數落檔成整數。畸零股造成的小數殘差 < 1 張，見 fetcher 模組註解。"""
    return int(round(float(value)))


def _process_one_date(target_date: str, fetcher, processor, file_mgr, company_codes) -> int:
    """抓單日全市場借券賣出餘額並寫入 JSON，回傳成功筆數。"""
    combined_df = fetcher.fetch_all(target_date)
    if combined_df is None or combined_df.empty:
        logger.warning(f"  [{target_date}] 無資料（假日或尚未公布）。")
        return 0

    formatted_date = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:]}"
    success_count = 0

    for row in combined_df.to_dict("records"):
        code = str(row["stock_id"]).strip()
        if company_codes and code not in company_codes:
            continue

        # 全市場回應含權證等非個股代號，數值欄位可能是 NaN；NaN 進 JSON 會讓
        # Node 端讀不回來（Python 讀得回來，本機測不出來），所以整筆跳過。
        values = {k: row.get(src) for k, src in _RECORD_FIELDS.items()}
        if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in values.values()):
            continue

        existing_data = file_mgr.load_financial_data(code)
        if not existing_data:
            continue

        record = {k: _to_lots(v) for k, v in values.items()}
        existing_data.setdefault("historical", {}).setdefault("securitiesLending", {})[formatted_date] = record
        existing_data.setdefault("latest", {}).update({"sblBalance": record["sblBalance"]})

        if file_mgr.save_financial_data(code, processor.clean_nan(existing_data)):
            success_count += 1

    logger.info(f"  [{target_date}] 完成 {success_count} 家公司。")
    return success_count


def _backfill_range(dates: List[str], fetcher, processor, file_mgr, company_codes) -> None:
    """
    補齊區間：先把整段抓進記憶體，再對每家公司只讀寫一次。

    **不要改回逐日呼叫 `_process_one_date`。** 那條路是為「單日」設計的，每個日期都會把
    全部約 2,300 個 company-financials JSON（單檔可達 1 MB）各讀寫一次；用在區間上就變成
    日數 × 公司數 次檔案 IO。2026-09-04 實測補 126 個交易日跑六分鐘才做完第一天，
    外推約 17 小時。改成這裡的兩段式後，檔案 IO 從 126×2,300 降到 2,300 次。

    記憶體無虞：126 天 × 2,300 檔 × 5 個整數，量級在數十 MB 以內。
    """
    by_code: Dict[str, Dict[str, Dict[str, int]]] = {}
    fetched_days = 0

    for d in dates:
        combined_df = fetcher.fetch_all(d)
        if combined_df is None or combined_df.empty:
            logger.warning(f"  [{d}] 無資料（假日或尚未公布）。")
            continue
        fetched_days += 1
        formatted_date = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        for row in combined_df.to_dict("records"):
            code = str(row["stock_id"]).strip()
            if company_codes and code not in company_codes:
                continue
            values = {k: row.get(src) for k, src in _RECORD_FIELDS.items()}
            if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in values.values()):
                continue
            by_code.setdefault(code, {})[formatted_date] = {k: _to_lots(v) for k, v in values.items()}
        time.sleep(2)

    logger.info(f"取數完成：{fetched_days}/{len(dates)} 個交易日有資料，涵蓋 {len(by_code)} 家公司。開始寫檔。")

    success_count = 0
    for code, records in by_code.items():
        existing_data = file_mgr.load_financial_data(code)
        if not existing_data:
            continue
        bucket = existing_data.setdefault("historical", {}).setdefault("securitiesLending", {})
        bucket.update(records)
        # latest 取整份資料裡最新的一天，而不是這次補齊的最後一天——補歷史時
        # 區間結尾可能早於檔案裡已有的日期，直接覆蓋會讓 latest 倒退。
        newest = max(bucket)
        existing_data.setdefault("latest", {}).update({"sblBalance": bucket[newest]["sblBalance"]})
        if file_mgr.save_financial_data(code, processor.clean_nan(existing_data)):
            success_count += 1

    logger.info(f"補齊完成，總計 {success_count} 家公司。")


def run_update_securities_lending(args):
    """更新借券賣出餘額：支援單日（--date）或補齊區間（--backfill-from）。"""
    fetcher = SecuritiesLendingFetcher()
    processor = DataProcessor()
    file_mgr = FileManager()

    companies = load_companies_for_processing(args, file_mgr)
    company_codes = {c["code"] for c in companies} if companies else None

    backfill_from = getattr(args, "backfill_from", None)
    if backfill_from:
        start = backfill_from.replace("-", "")
        now = now_tw()
        end = (now - timedelta(days=1) if now.hour < 18 else now).strftime("%Y%m%d")
        dates = _trading_days_between(start, end)
        logger.info(f"補齊借券賣出餘額：{start} ～ {end}，共 {len(dates)} 個交易日。")
        _backfill_range(dates, fetcher, processor, file_mgr, company_codes)
        return

    target_date = getattr(args, "date", None)
    if not target_date:
        now = now_tw()
        target_date = (now - timedelta(days=1) if now.hour < 18 else now).strftime("%Y%m%d")
    target_date = target_date.replace("-", "")

    logger.info(f"Target Date: {target_date}")
    count = _process_one_date(target_date, fetcher, processor, file_mgr, company_codes)

    # 交易日卻拿不到當日資料 → STALE 失敗，不可綠燈（休市日照舊跳過）
    if count == 0 and is_tw_trading_day(parse_yyyymmdd(target_date)):
        logger.error(f"STALE: {target_date} 為交易日，但借券賣出餘額尚未公布或撈取失敗，終止任務。")
        sys.exit(1)
