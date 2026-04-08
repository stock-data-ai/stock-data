import sys
import time
import random
from datetime import timedelta
import logging
import os

from loguru import logger as loguru_logger
loguru_logger.disable("FinMind")

from core import FinMindClient, DataProcessor, FileManager
from core.exceptions import ApiExhaustedError
from core.timezone import now_tw
import json
from pathlib import Path

from fetchers import FinancialsFetcher, RevenueFetcher, DividendsFromCSVFetcher
from fetchers.shareholding import fetch_shareholding
from fetchers.institutional_investors_shares import fetch_institutional_investors_shares
from processing.company_processor import CompanyProcessor
from processing.inst_ratio_calculator import InstRatioCalculator
from utils.company_list_loader import load_companies_for_processing
from utils.rerun_manager import RerunManager
from utils.quality_report import save_quality_report
import config

_REPO_ROOT = Path(__file__).parent.parent.parent
_SEEDS_FILE = Path(__file__).parent.parent / "data/inst_ratio_seeds.json"
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

def run_full_update(args):
    """
    處理完整更新任務 (財務、營收、股利)
    """
    logger.info("處理完整更新任務 (財務、營收、股利、法人持股)...")

    # RerunManager: 讀取用 combined file，寫入用 per-batch file
    batch = args.batch.split("/")[0] if getattr(args, "batch", None) else None
    read_mgr = RerunManager("full_update")           # 讀: rerun_queue_full_update.txt
    write_mgr = RerunManager("full_update", batch)   # 寫: rerun_queue_full_update_N.txt

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
    csv_dividends_fetcher = DividendsFromCSVFetcher()

    all_dividends_from_csv = csv_dividends_fetcher.fetch_all()

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
        all_dividends_data=all_dividends_from_csv,
        institutional_investors_fetcher=None,
        institutional_investors_shares_fetcher=lambda stock_id, start_date: fetch_institutional_investors_shares(client, stock_id, start_date),
        shareholding_fetcher=lambda stock_id, start_date: fetch_shareholding(client, stock_id, start_date),
        inst_ratio_calculator=inst_ratio_calc,
    )

    start_date = (now_tw() - timedelta(days=config.FULL_UPDATE_DAYS)).strftime("%Y-%m-%d")
    success_count = 0
    failed_companies = []
    quality_issues = []

    for idx, company in enumerate(companies, 1):
        code = company["code"]
        name = company.get("name", code)
        logger.debug(f"\n{'='*60}")
        logger.debug(f"[{idx}/{len(companies)}] 準備處理公司代碼: {code}, 名稱: {name}")
        logger.debug(f"  處理流程: 檢查資料、處理財務報表/營收/股利、更新季度數據/年成長率、整合法人持股、清理 NaN 值並儲存。")
        logger.debug(f"{'-'*60}")


        # 全量更新抓取的資料維度不同，不應受限於「今日已更新」檢查
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
                    # 紀錄資料缺失情況
                    missings = []
                    if not status.get("fin"): missings.append("財報")
                    if not status.get("rev"): missings.append("營收")
                    if not status.get("inst"): missings.append("法人")
                    if not status.get("div"): missings.append("股利")
                    
                    if missings:
                        quality_issues.append(f"{code} {name}: 缺失 {', '.join(missings)}")
                
                logger.info(f"[{idx}/{len(companies)}] ✔️  {code} {name}")
            else:
                logger.warning(f"[{idx}/{len(companies)}] ❌  {code} {name} 處理失敗")
                failed_companies.append(code)
        except ApiExhaustedError:
            logger.warning(f"\n⚠️  API 額度已耗盡，發生於公司 {code}。所有可用 token 皆已用盡。")
            remaining = companies[idx:]
            
            # 儲存品質報告 (即使 API 斷掉也要存目前已有的)
            save_quality_report("full_update", batch, quality_issues)
            write_mgr.save_api_exhausted(failed_companies, code, remaining)
            logger.info(f"本輪已完成: {success_count}/{len(companies)} 間公司。")
            logger.info("退出，等待 GitHub Actions 觸發下一輪重試。")
            sys.exit(1)
        except Exception as e:
            logger.exception(f"  X 處理公司 {code} 時發生未預期錯誤:")
            failed_companies.append(code)


        if idx < len(companies):
             time.sleep(random.uniform(*config.DEFAULT_SLEEP_RANGE))

    # 最終儲存品質報告
    save_quality_report("full_update", batch, quality_issues)

    logger.info(f"\n{'='*60}")
    logger.info(f"更新完成: {success_count}/{len(companies)} 家公司")
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