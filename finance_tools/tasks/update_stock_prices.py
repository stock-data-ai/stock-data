# finance_tools/tasks/update_stock_prices.py
import sys
import time
import random
import logging

from core import FinMindClient
from core.exceptions import ApiExhaustedError
from core.file_manager import FileManager
import config
from utils.company_list_loader import load_companies_for_processing
from utils.rerun_manager import RerunManager
from fetchers.taiwan_stock_price import fetch_taiwan_stock_price_history, update_stock_price_history_file

logger = logging.getLogger(__name__)

DEFAULT_SLEEP_RANGE = config.DEFAULT_SLEEP_RANGE
STOCK_PRICES_DIR = str(config.COMPANY_STOCK_PRICES_DIR)

def run_update_stock_prices(args):
    """
    Fetches and saves Taiwan stock price history for specified companies.
    """
    logger.info("正在更新台股歷史股價數據...")

    # Initialize RerunManager with batch support
    batch = args.batch.split("/")[0] if getattr(args, "batch", None) else None
    rerun_mgr = RerunManager("stock_prices", batch)

    client = FinMindClient()
    file_mgr = FileManager()

    if not client.token:
        logger.error("沒有可用的 FinMind API Token。無法抓取股價數據。")
        return

    # Load the list of companies to be processed
    companies = load_companies_for_processing(args, file_mgr, rerun_mgr)
    if not companies:
        logger.info("沒有選定公司進行股價更新。退出。")
        return

    logger.info(f"準備處理 {len(companies)} 間公司。")

    success_count = 0
    failed_companies = []

    # Pre-calculate start_date for 3 months ago
    days_to_fetch = config.DEFAULT_FETCH_DAYS # Fetch up to 3 months of data

    for idx, company in enumerate(companies, 1):
        code = company["code"]
        name = company.get("name", code)
        logger.info(f"{'='*60}")
        logger.info(f"[{idx}/{len(companies)}] 正在抓取公司 {code}, 名稱: {name} 的股價數據...")

        try:
            df = fetch_taiwan_stock_price_history(
                stock_id=code,
                client=client,
                days=days_to_fetch
            )

            if not df.empty:
                update_stock_price_history_file(df, code, STOCK_PRICES_DIR, days_to_fetch)
                success_count += 1
            else:
                logger.warning(f"  ⚠️ 公司 {code} 未能抓取到股價數據，將其標記為失敗。")
                failed_companies.append(code)

        except ApiExhaustedError:
            logger.warning(f"⚠️  FinMind API 額度已耗盡，發生於公司 {code}。")
            remaining = companies[idx:]
            rerun_mgr.save_api_exhausted(failed_companies, code, remaining)
            logger.info(f"本輪已完成: {success_count}/{len(companies)} 間公司。")
            logger.info("退出。")
            sys.exit(1)
        except Exception as e:
            logger.error(f"  X 處理公司 {code} ({name}) 股價時發生未預期錯誤: {e}")
            failed_companies.append(code)

        if idx < len(companies):
            time.sleep(random.uniform(*DEFAULT_SLEEP_RANGE))

    logger.info(f"\n{'='*60}")
    logger.info(f"股價更新完成。成功處理: {success_count}/{len(companies)} 間公司。")
    if failed_companies:
        logger.warning(f"X 失敗公司: {len(failed_companies)} 間公司。")
        rerun_mgr.save(failed_companies)
    else:
        rerun_mgr.clear()
    logger.info(f"{'='*60}\n")
