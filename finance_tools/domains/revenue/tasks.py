import logging
import sys

import pandas as pd

from finance_tools.core import FinMindClient, DataProcessor, FileManager
from finance_tools.core import market_fetch, merge_policy
from finance_tools.core.exceptions import ApiError, ApiExhaustedError
from finance_tools.domains.financials.calculator import FinancialCalculator
from finance_tools.core.timezone import now_tw, today_str
from finance_tools.utils.company_list_loader import load_companies_for_processing
from finance_tools.utils.rerun_manager import RerunManager
import finance_tools.config as config

logger = logging.getLogger(__name__)


def run_update_revenue(args):
    """
    只更新月營收：全市場抓完整視窗（見 `config.MONTHLY_REVENUE_YEARS`），逐家合併。
    """
    logger.info("Handling revenue update...")

    rerun_mgr = RerunManager("revenue")
    client = FinMindClient()
    processor = DataProcessor()
    file_mgr = FileManager()

    companies = load_companies_for_processing(args, file_mgr, rerun_mgr)
    if not companies:
        logger.info("No companies selected for revenue update. Exiting.")
        return

    try:
        revenue = market_fetch.fetch_market(
            client, "TaiwanStockMonthRevenue",
            market_fetch.month_start_dates(config.MONTHLY_REVENUE_YEARS, now_tw().date()),
        )
    except ApiExhaustedError as e:
        logger.warning(f"⚠️  FinMind 額度用盡：{e}；全部留待重跑。")
        rerun_mgr.save([c["code"] for c in companies])
        sys.exit(2)
    except (ApiError, Exception) as e:
        logger.error(f"❌ 全市場抓取失敗，本輪不寫任何檔：{e}")
        sys.exit(1)
    revenue_by_code = market_fetch.split_by_stock(revenue)

    success_count = 0
    failed_companies = []
    for company in companies:
        code = company["code"]
        existing_data = file_mgr.load_financial_data(code)
        if not existing_data:
            # 沒有既有檔（或壞檔）由 financials-update 建檔／重建，這裡不拿片段資料建新檔。
            logger.warning(f"  ! No existing data found for {code}, skipping.")
            continue

        monthly_revenue = processor.process_monthly_revenue(code, revenue_by_code.get(code, pd.DataFrame()))
        if not monthly_revenue:
            continue

        existing_data.setdefault("historical", {})
        # 與 `build_final_data` 走同一條規則：以 (年,月) 合併、上限集中在 merge_policy。
        # 舊版是「整批取代、留 36」——抓取視窗只有 REVENUE_DAYS(365)，整批取代等於
        # 把既有的六年月營收砍成 13 個月，而且無聲。
        existing_data["historical"]["monthlyRevenue"] = merge_policy.merge_by_key(
            existing_data["historical"].get("monthlyRevenue"), monthly_revenue,
            key=lambda m: (m["year"], m["month"]),
            limit=merge_policy.MONTHLY_REVENUE_LIMIT,
        ) or None
        # 與 build_final_data 同一條：合併後用完整歷史重算年增率
        FinancialCalculator.fill_monthly_yoy(existing_data["historical"]["monthlyRevenue"])
        existing_data["lastUpdated"] = today_str()

        if file_mgr.save_financial_data(code, processor.clean_nan(existing_data)):
            success_count += 1
        else:
            logger.error(f"  X Failed to save {code}")
            failed_companies.append(code)

    logger.info(f"OK Completed: {success_count}/{len(companies)} companies")
    if failed_companies:
        logger.error(f"X Failed: {len(failed_companies)} companies: {', '.join(failed_companies)}")
        rerun_mgr.save(failed_companies)
    else:
        rerun_mgr.clear()
