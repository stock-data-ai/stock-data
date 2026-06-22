import time
import random
from datetime import timedelta
import logging
import os

from loguru import logger as loguru_logger
loguru_logger.disable("FinMind")

from finance_tools.core import FinMindClient, DataProcessor, FileManager
from finance_tools.core.exceptions import ApiExhaustedError
from finance_tools.core.timezone import now_tw
import json
from pathlib import Path

from finance_tools.domains.financials.fetcher import FinancialsFetcher
from finance_tools.domains.revenue.fetcher import RevenueFetcher
from finance_tools.domains.institutional_investors.shares_fetcher import fetch_institutional_investors_shares
from finance_tools.orchestration.company_processor import CompanyProcessor
from finance_tools.domains.institutional_investors.calculator import InstRatioCalculator
from finance_tools.utils.company_list_loader import load_companies_for_processing
from finance_tools.utils.rerun_manager import RerunManager
from finance_tools.utils.quality_report import save_quality_report
from finance_tools.utils.task_helpers import handle_api_exhausted
import finance_tools.config as config

_REPO_ROOT = Path(__file__).parent.parent.parent
_SEEDS_FILE = Path(__file__).parent.parent / "assets/seeds/inst_ratio_seeds.json"
_COMPANIES_FILE = _REPO_ROOT / "src/data/layer3/companies/companies-all.json"


def _load_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"無法讀取 {path}: {e}")
        return {}

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)



DEFAULT_SLEEP_RANGE = config.DEFAULT_SLEEP_RANGE


def _save_stats(task_name: str, batch, fin: int, rev: int, success: int, total: int) -> None:
    """每個 batch 寫出統計數字，供 merge job 彙整後顯示於 GitHub Summary。"""
    suffix = f"_{batch}" if batch else ""
    stats_path = os.path.join(str(config.RERUN_DIR), f"stats_{task_name}{suffix}.txt")
    with open(stats_path, "w", encoding="utf-8") as f:
        f.write(f"fin={fin}\nrev={rev}\nsuccess={success}\ntotal={total}\n")


def run_financials_update(args):
    """
    處理財務報表更新任務 (損益表、月營收)
    """
    logger.info("處理財務報表更新任務 (損益表、月營收)...")

    # RerunManager: 讀取用 combined file，寫入用 per-batch file
    batch = args.batch.split("/")[0] if getattr(args, "batch", None) else None
    read_mgr = RerunManager("financials_update")           # 讀: rerun_queue_financials_update.txt
    write_mgr = RerunManager("financials_update", batch)   # 寫: rerun_queue_financials_update_N.txt

    client = FinMindClient()
    processor_data = DataProcessor()
    file_mgr = FileManager()

    # 在開始時顯示當前的 API 使用情況
    user_count, api_limit = client.check_api_usage()
    if user_count is not None and api_limit is not None:
        logger.info(f"目前 FinMind API 使用量: {user_count}/{api_limit} 次請求。")
    else:
        logger.warning("無法獲取 FinMind API 使用量資訊。")

    financials_fetcher = FinancialsFetcher(client, processor_data)
    revenue_fetcher = RevenueFetcher(client, processor_data)

    # 載入種子值與公司基本資料（供持股比例推估使用）
    seeds = _load_json(_SEEDS_FILE)
    companies_data = _load_json(_COMPANIES_FILE)
    inst_ratio_calc = InstRatioCalculator(seeds=seeds, companies_data=companies_data)

    # 載入待處理的公司列表
    companies = load_companies_for_processing(args, file_mgr, read_mgr)
    if not companies:
        logger.info("沒有選定公司進行完整更新。正在退出。")
        return

    logger.info(f"\n準備處理 {len(companies)} 家公司。\n")

    # Initialize the processor with all required data
    processor = CompanyProcessor(
        financials_fetcher=financials_fetcher,
        revenue_fetcher=revenue_fetcher,
        processor=processor_data,
        file_mgr=file_mgr,
        finmind_client=client,
        all_companies_details=companies_data,
        institutional_investors_shares_fetcher=lambda stock_id, start_date: fetch_institutional_investors_shares(client, stock_id, start_date),
        shareholding_fetcher=None,
        inst_ratio_calculator=inst_ratio_calc,
    )

    start_date = (now_tw() - timedelta(days=config.FULL_UPDATE_DAYS)).strftime("%Y-%m-%d")
    success_count = 0
    fin_count = 0
    rev_count = 0
    failed_companies = []
    quality_issues = []

    for idx, company in enumerate(companies, 1):
        code = company["code"]
        name = company.get("name", code)
        logger.debug(f"\n{'='*60}")
        logger.debug(f"[{idx}/{len(companies)}] 準備處理公司代碼: {code}, 名稱: {name}")
        logger.debug(f"{'-'*60}")

        is_force_update = True

        try:
            success, status = processor.process_company(
                code=code,
                name=name,
                start_date=start_date,
                force_update=is_force_update,
            )

            if success:
                success_count += 1
                if not status.get("skipped"):
                    if status.get("fin"): fin_count += 1
                    if status.get("rev"): rev_count += 1
                    missings = []
                    if not status.get("fin"): missings.append("財報")
                    if not status.get("rev"): missings.append("營收")
                    if missings:
                        quality_issues.append(f"{code} {name}: 缺失 {', '.join(missings)}")

                logger.info(f"[{idx}/{len(companies)}] ✔️  {code} {name}")
            else:
                logger.warning(f"[{idx}/{len(companies)}] ❌  {code} {name} 處理失敗")
                failed_companies.append(code)
        except ApiExhaustedError:
            handle_api_exhausted(
                "financials_update", batch, write_mgr,
                failed_companies, code, companies[idx:],
                success_count, len(companies), quality_issues,
            )
        except Exception as e:
            logger.exception(f"  X 處理公司 {code} 時發生未預期錯誤:")
            failed_companies.append(code)

        if idx < len(companies):
            time.sleep(random.uniform(*config.DEFAULT_SLEEP_RANGE))

    # 儲存品質報告 + 統計數字
    save_quality_report("financials_update", batch, quality_issues)
    _save_stats("financials_update", batch, fin_count, rev_count, success_count, len(companies))

    logger.info(f"\n{'='*60}")
    logger.info(f"更新完成: {success_count}/{len(companies)} 家公司 (損益表 {fin_count} / 月營收 {rev_count})")
    if failed_companies:
        logger.warning(f"失敗/剩餘: {len(failed_companies)} 家公司")
        logger.warning(f"   失敗/剩餘公司代碼 (前10個): {', '.join(sorted(list(set(failed_companies)))[:10])}{'...' if len(set(failed_companies)) > 10 else ''}")
        write_mgr.save(failed_companies)
    else:
        write_mgr.clear()

    # 顯示最終 API 使用情況
    user_count, api_limit = client.check_api_usage()
    if user_count is not None and api_limit is not None:
        logger.info(f"最終 FinMind API 使用量: {user_count}/{api_limit} 次請求。")
    else:
        logger.warning("無法獲取最終 FinMind API 使用量資訊。")

    logger.info(f"{'='*60}\n")