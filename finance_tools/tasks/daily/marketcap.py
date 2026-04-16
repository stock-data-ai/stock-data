import time
import random
import logging

from core import DataProcessor, FileManager
from processing.company_processor import CompanyProcessor
from utils.company_list_loader import load_companies_for_processing, filter_already_updated
from utils.rerun_manager import RerunManager
from utils.quality_report import save_quality_report
import config

logger = logging.getLogger(__name__)

DEFAULT_SLEEP_RANGE = config.DEFAULT_SLEEP_RANGE


def run_update_marketcap(args):
    """
    更新市值到 company-financials 檔案。
    透過 Yahoo Finance API 直接取得最新市值數據。
    """
    logger.info("正在透過 Yahoo Finance 取得數據更新市值...")

    batch = args.batch.split("/")[0] if getattr(args, "batch", None) else None
    read_mgr = RerunManager("marketcap")
    write_mgr = RerunManager("marketcap", batch)

    processor_data = DataProcessor()
    file_mgr = FileManager()

    companies = load_companies_for_processing(args, file_mgr, read_mgr)
    if not companies:
        logger.info("沒有選定公司進行市值更新。退出。")
        return

    is_force_update = args.force or args.code is not None or args.rerun
    companies = filter_already_updated(companies, file_mgr, force_update=is_force_update)
    if not companies:
        logger.info("所有公司均已於今日更新，無需處理。")
        write_mgr.clear()
        return

    logger.info(f"正在處理 {len(companies)} 家公司的市值資料。")

    all_companies_details = file_mgr.load_all_companies_with_details()

    # 初始化 Processor，市值更新現在主要依賴底層的 YahooFetcher 邏輯
    company_processor = CompanyProcessor(
        processor=processor_data,
        file_mgr=file_mgr,
        finmind_client=None, # 市值更新不需要 FinMind
        financials_fetcher=None,
        revenue_fetcher=None,
        all_companies_details=all_companies_details,
        all_dividends_data={},
        institutional_investors_fetcher=None,
        institutional_investors_shares_fetcher=None,
    )

    success_count = 0
    failed_companies = []
    quality_issues = []

    for idx, company in enumerate(companies, 1):
        code = company["code"]
        name = company.get("name", code)

        try:
            # 此方法內部已實作 Yahoo Finance 抓取邏輯
            success, status = company_processor.process_marketcap_only(
                code=code,
                name=name,
                force_update=is_force_update,
            )

            if success:
                success_count += 1
                if not status.get("skipped") and not status.get("marketcap"):
                    quality_issues.append(f"{code} {name}: 缺失市值資料 (無法從 Yahoo 取得)")
                logger.info(f"[{idx}/{len(companies)}] OK {code} {name}")
            else:
                logger.warning(f"[{idx}/{len(companies)}] X {code} {name} 處理失敗")
                failed_companies.append(code)
        except Exception:
            logger.exception(f"  X 處理公司 {code} 時發生未預期錯誤:")
            failed_companies.append(code)

        if idx < len(companies):
            # Yahoo API 雖然較寬鬆，但仍建議保持適度間隔
            time.sleep(random.uniform(*DEFAULT_SLEEP_RANGE))

    # Final save quality report
    save_quality_report("marketcap", batch, quality_issues)

    logger.info(f"\n{'='*60}")
    logger.info(f"OK 市值更新完成: {success_count}/{len(companies)} 家公司")
    if failed_companies:
        logger.warning(f"X 失敗: {len(failed_companies)} 家公司")
        unique_failed = sorted(list(set(failed_companies)))
        logger.warning(f"   失敗公司代碼 (前10個): {', '.join(unique_failed[:10])}{'...' if len(unique_failed) > 10 else ''}")
        write_mgr.save(failed_companies)
    else:
        write_mgr.clear()

    logger.info(f"{'='*60}\n")
