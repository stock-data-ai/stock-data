import time
import random
import logging
from datetime import timedelta
from datetime import timedelta

from finance_tools.core import FinMindClient, DataProcessor, FileManager
from finance_tools.core import merge_policy
from finance_tools.core.timezone import now_tw, today_str
from finance_tools.domains.revenue.fetcher import RevenueFetcher
from finance_tools.utils.company_list_loader import load_companies_for_processing, filter_already_updated
from finance_tools.utils.rerun_manager import RerunManager
import finance_tools.config as config

logger = logging.getLogger(__name__)

def run_update_revenue(args):
    """
    處理更新月營收任務
    """
    logger.info("Handling revenue update...")

    # Initialize RerunManager
    rerun_mgr = RerunManager("revenue")

    client = FinMindClient()
    processor = DataProcessor()
    file_mgr = FileManager()
    revenue_fetcher = RevenueFetcher(client, processor)

    user_count, api_limit = client.check_api_usage()
    if user_count is not None and api_limit is not None:
        logger.info(f"FinMind API Usage: {user_count} / {api_limit}")
        if user_count >= api_limit * 0.9:
            logger.warning("⚠️  API usage near limit! Exiting.")
            return
    else:
        logger.warning("Could not check API usage.")

    companies = load_companies_for_processing(args, file_mgr, rerun_mgr)
    if not companies:
        logger.info("No companies selected for revenue update. Exiting.")
        return

    is_force_update = getattr(args, 'force', False)
    companies = filter_already_updated(companies, file_mgr, force_update=is_force_update)
    if not companies:
        logger.info("All companies already updated today. Exiting.")
        rerun_mgr.clear()
        return

    logger.info(f"Processing {len(companies)} companies for revenue update.\n")

    start_date = (now_tw() - timedelta(days=config.REVENUE_DAYS)).strftime("%Y-%m-%d")
    success_count = 0
    failed_companies = []

    for idx, company in enumerate(companies, 1):
        code = company["code"]
        name = company.get("name", code)
        logger.info(f"[{idx}/{len(companies)}] {code} {name}")

        existing_data = file_mgr.load_financial_data(code)
        if not existing_data:
            logger.warning(f"  ! No existing data found for {code}, skipping.")
            failed_companies.append(code)
            continue

        # --- Smart Update Logic (safety net if pre-filter missed) ---
        if not is_force_update:
            last_updated = existing_data.get("lastUpdated")
            if last_updated == today_str():
                logger.info(f"  ✓ Skipping {code} (already updated today)")
                continue
        # --- End Smart Update Logic ---

        monthly_revenue, success = revenue_fetcher.fetch_and_process(code, start_date)
        if not success:
            failed_companies.append(code)
            logger.error(f"  X Failed to fetch revenue")
            time.sleep(random.uniform(*config.DEFAULT_SLEEP_RANGE))
            continue

        if "historical" not in existing_data:
            existing_data["historical"] = {}

        # 與 `build_final_data` 走同一條規則：以 (年,月) 合併、上限集中在 merge_policy。
        # 舊版是「整批取代、留 36」——抓取視窗只有 REVENUE_DAYS(365)，整批取代等於
        # 把既有的六年月營收砍成 13 個月，而且無聲。
        existing_data["historical"]["monthlyRevenue"] = merge_policy.merge_by_key(
            existing_data["historical"].get("monthlyRevenue"), monthly_revenue,
            key=lambda m: (m["year"], m["month"]),
            limit=merge_policy.MONTHLY_REVENUE_LIMIT,
        ) or None
        existing_data["lastUpdated"] = today_str()

        final_data = processor.clean_nan(existing_data)

        if file_mgr.save_financial_data(code, final_data):
            logger.info(f"  OK Updated: {len(monthly_revenue)} months")
            success_count += 1
        else:
            logger.error(f"  X Failed to save")
            failed_companies.append(code)

        if idx < len(companies):
            time.sleep(random.uniform(*config.DEFAULT_SLEEP_RANGE))

    logger.info(f"\n{'='*60}")
    logger.info(f"OK Completed: {success_count}/{len(companies)} companies")
    if failed_companies:
        logger.error(f"X Failed: {len(failed_companies)} companies: {', '.join(failed_companies)}")
        rerun_mgr.save(failed_companies)
    else:
        rerun_mgr.clear()
    logger.info(f"{ '='*60}\n")