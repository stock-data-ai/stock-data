import logging
import os
import sys

import pandas as pd

from loguru import logger as loguru_logger
loguru_logger.disable("FinMind")

from finance_tools.core import FinMindClient, DataProcessor, FileManager
from finance_tools.core.exceptions import ApiError, ApiExhaustedError
from finance_tools.core.timezone import now_tw
from finance_tools.core import market_fetch
import json
from pathlib import Path

from finance_tools.orchestration.company_processor import CompanyProcessor
from finance_tools.domains.institutional_investors.calculator import InstRatioCalculator
from finance_tools.utils.company_list_loader import load_companies_for_processing
from finance_tools.utils.rerun_manager import RerunManager
from finance_tools.utils.quality_report import save_quality_report
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


def _save_stats(task_name: str, batch, fin: int, rev: int, success: int, total: int) -> None:
    """每個 batch 寫出統計數字，供 merge job 彙整後顯示於 GitHub Summary。"""
    suffix = f"_{batch}" if batch else ""
    stats_path = os.path.join(str(config.RERUN_DIR), f"stats_{task_name}{suffix}.txt")
    with open(stats_path, "w", encoding="utf-8") as f:
        f.write(f"fin={fin}\nrev={rev}\nsuccess={success}\ntotal={total}\n")


def fetch_market_statements_and_revenue(client: FinMindClient):
    """全市場抓損益表（完整視窗）與月營收，回傳依代號分組的兩個 dict。"""
    today = now_tw().date()
    statements = market_fetch.fetch_market(
        client, "TaiwanStockFinancialStatements",
        market_fetch.quarter_end_dates(config.INCOME_STATEMENT_YEARS, today),
        keep=market_fetch.item_filter(DataProcessor.INCOME_STATEMENT_ITEMS),
    )
    revenue = market_fetch.fetch_market(
        client, "TaiwanStockMonthRevenue",
        market_fetch.month_start_dates(config.MONTHLY_REVENUE_YEARS, today),
    )
    return market_fetch.split_by_stock(statements), market_fetch.split_by_stock(revenue)


def run_financials_update(args):
    """
    處理財務報表更新任務 (損益表、月營收)：每一期抓一次全市場，再逐家合併寫檔。
    """
    logger.info("處理財務報表更新任務 (損益表、月營收)...")

    # RerunManager: 讀取用 combined file，寫入用 per-batch file
    batch = args.batch.split("/")[0] if getattr(args, "batch", None) else None
    read_mgr = RerunManager("financials_update")           # 讀: rerun_queue_financials_update.txt
    write_mgr = RerunManager("financials_update", batch)   # 寫: rerun_queue_financials_update_N.txt

    client = FinMindClient()
    processor_data = DataProcessor()
    file_mgr = FileManager()

    user_count, api_limit = client.check_api_usage()
    if user_count is not None and api_limit is not None:
        logger.info(f"目前 FinMind API 使用量: {user_count}/{api_limit} 次請求。")

    companies = load_companies_for_processing(args, file_mgr, read_mgr)
    if not companies:
        logger.info("沒有選定公司進行完整更新。正在退出。")
        return

    # 1. 全市場抓取。任何一期失敗就整個停下來，一個檔都不寫。
    try:
        statements_by_code, revenue_by_code = fetch_market_statements_and_revenue(client)
    except ApiExhaustedError as e:
        logger.warning(f"⚠️  FinMind 額度用盡：{e}；全部 {len(companies)} 家留待下一輪重跑。")
        write_mgr.save([c["code"] for c in companies])
        sys.exit(2)  # exit 2 = API exhausted (rerun needed), not a real error
    except (ApiError, Exception) as e:
        logger.error(f"❌ 全市場抓取失敗，本輪不寫任何檔：{e}")
        sys.exit(1)

    companies_data = _load_json(_COMPANIES_FILE)
    seeds = _load_json(_SEEDS_FILE)
    processor = CompanyProcessor(
        processor=processor_data,
        file_mgr=file_mgr,
        all_companies_details=companies_data,
        inst_ratio_calculator=InstRatioCalculator(seeds=seeds, companies_data=companies_data),
    )

    # 2. 逐家合併寫檔（純本地運算，不再打 API）
    empty = pd.DataFrame()
    success_count = fin_count = rev_count = 0
    failed_companies = []
    quality_issues = []

    for idx, company in enumerate(companies, 1):
        code = company["code"]
        name = company.get("name", code)
        success, status = processor.process_company(
            code, name, statements_by_code.get(code, empty), revenue_by_code.get(code, empty))
        if not success:
            logger.warning(f"[{idx}/{len(companies)}] ❌  {code} {name} 處理失敗")
            failed_companies.append(code)
            continue
        success_count += 1
        fin_count += status["fin"]
        rev_count += status["rev"]
        missings = [label for key, label in (("fin", "財報"), ("rev", "營收")) if not status[key]]
        if missings:
            quality_issues.append(f"{code} {name}: 缺失 {', '.join(missings)}")

    # 儲存品質報告 + 統計數字
    save_quality_report("financials_update", batch, quality_issues)
    _save_stats("financials_update", batch, fin_count, rev_count, success_count, len(companies))

    logger.info(f"\n{'='*60}")
    logger.info(f"更新完成: {success_count}/{len(companies)} 家公司 (損益表 {fin_count} / 月營收 {rev_count})")
    if failed_companies:
        logger.warning(f"失敗: {len(failed_companies)} 家公司（寫檔失敗）: {', '.join(sorted(failed_companies)[:10])}")
        write_mgr.save(failed_companies)
    else:
        write_mgr.clear()

    user_count, api_limit = client.check_api_usage()
    if user_count is not None and api_limit is not None:
        logger.info(f"最終 FinMind API 使用量: {user_count}/{api_limit} 次請求。")
    logger.info(f"{'='*60}\n")
