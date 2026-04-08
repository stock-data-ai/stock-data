import sys
import time
import random
from datetime import timedelta
import logging

import json
from pathlib import Path

from core import FinMindClient, DataProcessor, FileManager
from core.exceptions import ApiExhaustedError
from core.timezone import now_tw
import config
from fetchers.shareholding import fetch_shareholding
from fetchers.institutional_investors_shares import fetch_institutional_investors_shares
from processing.company_processor import CompanyProcessor
from processing.inst_ratio_calculator import InstRatioCalculator
from utils.company_list_loader import load_companies_for_processing, filter_already_updated
from utils.rerun_manager import RerunManager
from utils.quality_report import save_quality_report

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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DEFAULT_SLEEP_RANGE = config.DEFAULT_SLEEP_RANGE

def run_update_institutional_investors(args):
    """
    處理三大法人每日買賣超交易數據更新任務。
    只更新三大法人持股比例與買賣超資料，不觸碰財務報表、營收、股利、市值等欄位。
    """
    logger.info("正在處理三大法人每日買賣超數據更新任務...")

    # RerunManager: 讀取用 combined file，寫入用 per-batch file
    batch = args.batch.split("/")[0] if getattr(args, "batch", None) else None
    read_mgr = RerunManager("institutional_investors")
    write_mgr = RerunManager("institutional_investors", batch)

    client = FinMindClient()
    processor_data = DataProcessor()
    file_mgr = FileManager()

    # Load companies first so we can save them to rerun file if API is exhausted
    companies = load_companies_for_processing(args, file_mgr, read_mgr)
    if not companies:
        logger.info("沒有選定公司進行三大法人數據更新。退出。")
        return

    is_force_update = args.force or args.code is not None or args.rerun
    companies = filter_already_updated(companies, file_mgr, force_update=is_force_update)
    if not companies:
        logger.info("所有公司均已於今日更新，無需處理。")
        write_mgr.clear()
        return
    logger.info(f"正在處理 {len(companies)} 間公司的三大法人數據。")

    user_count, api_limit = client.check_api_usage()
    if user_count is not None and api_limit is not None:
        logger.info(f"當前 FinMind API 用量: {user_count}/{api_limit} 次請求。")
        if user_count >= api_limit * 0.9:
            logger.warning("⚠️  API 用量接近限制！準備退出。")
            write_mgr.save([c["code"] for c in companies])
            sys.exit(1)
    else:
        logger.warning("無法取得 FinMind API 用量資訊。")

    # 載入種子值與公司基本資料（供持股比例推估使用）
    seeds = _load_json(_SEEDS_FILE)
    companies_data = _load_json(_COMPANIES_FILE)
    inst_ratio_calc = InstRatioCalculator(seeds=seeds, companies_data=companies_data)

    # Initialize the processor with new shareholding-based ratio logic
    company_processor = CompanyProcessor(
        processor=processor_data,
        file_mgr=file_mgr,
        finmind_client=client,
        financials_fetcher=None,
        revenue_fetcher=None,
        all_companies_details=companies_data,
        all_dividends_data={},
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

        try:
            # Only update institutional investors data (not full pipeline)
            success, status = company_processor.process_institutional_investors_only(
                code=code,
                name=name,
                start_date=start_date,
                force_update=is_force_update,
            )

            if success:
                success_count += 1
                if not status.get("skipped") and not status.get("inst"):
                    quality_issues.append(f"{code} {name}: 缺失三大法人資料")
                logger.info(f"[{idx}/{len(companies)}] OK {code} {name}")
            else:
                logger.warning(f"\n{'='*60}")
                logger.warning(f"[{idx}/{len(companies)}] X {code} {name} 處理失敗")
                failed_companies.append(code)
        except ApiExhaustedError:
            logger.warning(f"\n⚠️  API 額度已耗盡，發生於公司 {code}。")
            remaining = companies[idx:]
            
            # Save quality report even on API exhaust
            save_quality_report("institutional_investors", batch, quality_issues)
            write_mgr.save_api_exhausted(failed_companies, code, remaining)
            logger.info(f"本輪已完成: {success_count}/{len(companies)} 間公司。")
            logger.info("退出，等待 GitHub Actions 觸發下一輪重試。")
            sys.exit(1)
        except Exception as e:
            logger.exception(f"  X 處理公司 {code} 時發生未預期錯誤:")
            failed_companies.append(code)

        if idx < len(companies):
            time.sleep(random.uniform(*DEFAULT_SLEEP_RANGE))

    # Final save quality report
    save_quality_report("institutional_investors", batch, quality_issues)

    logger.info(f"\n{'='*60}")
    logger.info(f"OK 更新完成: {success_count}/{len(companies)} 間公司")
    if failed_companies:
        logger.warning(f"X 失敗/剩餘: {len(failed_companies)} 間公司")
        unique_failed_companies = sorted(list(set(failed_companies)))
        logger.warning(f"   失敗/剩餘公司代碼 (前10個): {', '.join(unique_failed_companies[:10])}{'...' if len(unique_failed_companies) > 10 else ''}")
        write_mgr.save(failed_companies)
    else:
        write_mgr.clear()

    # Display final API usage
    user_count, api_limit = client.check_api_usage()
    if user_count is not None and api_limit is not None:
        logger.info(f"最終 FinMind API 用量: {user_count}/{api_limit} 次請求。")
    else:
        logger.warning("無法取得最終 FinMind API 用量資訊。")

    logger.info(f"{'='*60}\n")