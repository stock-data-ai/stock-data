import sys
import time
import random
from datetime import timedelta
import logging
import json
from pathlib import Path

from loguru import logger as loguru_logger
loguru_logger.disable("FinMind")

from finance_tools.core import FinMindClient, DataProcessor, FileManager
from finance_tools.core.exceptions import ApiExhaustedError
from finance_tools.core.timezone import now_tw
from finance_tools.domains.shareholder.shareholding_fetcher import fetch_shareholding
from finance_tools.domains.institutional_investors.shares_fetcher import fetch_institutional_investors_shares
from finance_tools.orchestration.company_processor import CompanyProcessor
from finance_tools.domains.institutional_investors.calculator import InstRatioCalculator
from finance_tools.utils.company_list_loader import load_companies_for_processing
from finance_tools.utils.rerun_manager import RerunManager
from finance_tools.utils.quality_report import save_quality_report
from finance_tools.utils.task_helpers import handle_api_exhausted
import finance_tools.config as config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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


def run_update_daily(args):
    """
    每日更新任務：市值（Yahoo Finance）+ 三大法人（FinMind）。
    每家公司一次 load/save，兩者都成功才移出 rerun queue。
    任一 API 回傳空資料（可能為暫時性問題）→ 儲存已取得部分 + 進 rerun queue。
    FinMind ApiExhaustedError → 立即 exit，等 GitHub Actions 觸發下一輪重試。
    """
    logger.info("正在執行每日更新（市值 + 三大法人）...")

    batch = args.batch.split("/")[0] if getattr(args, "batch", None) else None
    read_mgr = RerunManager("daily")
    write_mgr = RerunManager("daily", batch)

    client = FinMindClient()
    processor_data = DataProcessor()
    file_mgr = FileManager()

    # API 用量預檢
    user_count, api_limit = client.check_api_usage()
    if user_count is not None and api_limit is not None:
        logger.info(f"FinMind API 用量: {user_count}/{api_limit}")
        if user_count >= api_limit * 0.9:
            logger.warning("⚠️  API 用量接近上限，提前退出。")
            companies_for_queue = load_companies_for_processing(args, file_mgr, read_mgr)
            write_mgr.save([c["code"] for c in companies_for_queue])
            sys.exit(1)
    else:
        logger.warning("無法取得 FinMind API 用量資訊。")

    seeds = _load_json(_SEEDS_FILE)
    companies_data = _load_json(_COMPANIES_FILE)
    inst_ratio_calc = InstRatioCalculator(seeds=seeds, companies_data=companies_data)

    companies = load_companies_for_processing(args, file_mgr, read_mgr)
    if not companies:
        logger.info("沒有待處理的公司，退出。")
        return

    is_force_update = args.force or args.code is not None or args.rerun
    logger.info(f"正在處理 {len(companies)} 家公司（force={is_force_update}）。")

    company_processor = CompanyProcessor(
        processor=processor_data,
        file_mgr=file_mgr,
        finmind_client=client,
        financials_fetcher=None,
        revenue_fetcher=None,
        all_companies_details=companies_data,
        all_dividends_data={},
        institutional_investors_shares_fetcher=lambda stock_id, start_date: fetch_institutional_investors_shares(client, stock_id, start_date),
        shareholding_fetcher=lambda stock_id, start_date: fetch_shareholding(client, stock_id, start_date),
        inst_ratio_calculator=inst_ratio_calc,
    )

    start_date = (now_tw() - timedelta(days=config.DEFAULT_FETCH_DAYS)).strftime("%Y-%m-%d")
    success_count = 0
    failed_companies = []
    quality_issues = []

    for idx, company in enumerate(companies, 1):
        code = company["code"]
        name = company.get("name", code)

        try:
            success, status = company_processor.process_daily_only(
                code=code,
                name=name,
                start_date=start_date,
                force_update=is_force_update,
            )

            if status.get("skipped"):
                success_count += 1
                continue

            if success:
                success_count += 1
            else:
                failed_companies.append(code)
                # 記錄是哪一部分缺失，方便區分暫時性 vs 永久性問題
                missing = []
                if not status.get("marketcap"):
                    missing.append("市值(Yahoo)")
                if not status.get("inst"):
                    missing.append("三大法人(FinMind)")
                if missing:
                    quality_issues.append(f"{code} {name}: 缺失 {', '.join(missing)}")

            logger.info(f"[{idx}/{len(companies)}] {'✔' if success else '✘'} {code} {name}")

        except ApiExhaustedError:
            handle_api_exhausted(
                "daily", batch, write_mgr,
                failed_companies, code, companies[idx:],
                success_count, len(companies), quality_issues,
            )
        except Exception:
            logger.exception(f"  ❌ 處理公司 {code} 時發生未預期錯誤:")
            failed_companies.append(code)

        if idx < len(companies):
            time.sleep(random.uniform(*config.DEFAULT_SLEEP_RANGE))

    save_quality_report("daily", batch, quality_issues)

    logger.info(f"\n{'='*60}")
    logger.info(f"每日更新完成: {success_count}/{len(companies)} 家公司")
    if failed_companies:
        unique_failed = sorted(set(failed_companies))
        logger.warning(f"失敗/重試: {len(unique_failed)} 家公司 (前10): {', '.join(unique_failed[:10])}{'...' if len(unique_failed) > 10 else ''}")
        write_mgr.save(failed_companies)
    else:
        write_mgr.clear()

    user_count, api_limit = client.check_api_usage()
    if user_count is not None and api_limit is not None:
        logger.info(f"最終 FinMind API 用量: {user_count}/{api_limit}")
    logger.info(f"{'='*60}\n")
