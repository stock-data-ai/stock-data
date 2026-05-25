"""
Balance Sheet & Cash Flow Update Task
資產負債表 + 現金流量表 → ROE/ROA/流動比率/負債比率/OCF/FCF

獨立工作流，讀取現有 company-financials JSON，
補充 annual[] 中各年份的財務健全度指標後存回。
"""

import logging
import time
import random
from datetime import timedelta

from finance_tools.core import FinMindClient, DataProcessor, FileManager
from finance_tools.core.exceptions import ApiExhaustedError
from finance_tools.core.timezone import now_tw
from finance_tools.utils.company_list_loader import load_companies_for_processing
from finance_tools.utils.rerun_manager import RerunManager
from finance_tools.utils.task_helpers import handle_api_exhausted
import finance_tools.config as config

logger = logging.getLogger(__name__)

_START_YEARS_BACK = 2557  # ~7 years


def run_update_balance_sheet(args):
    """【定期】抓取資產負債表 + 現金流量表，計算並合併 ROE/ROA/比率/OCF/FCF。"""
    logger.info("開始更新資產負債表 & 現金流量表...")

    batch = args.batch.split("/")[0] if getattr(args, "batch", None) else None
    read_mgr = RerunManager("balance_sheet")
    write_mgr = RerunManager("balance_sheet", batch)

    client = FinMindClient()
    processor = DataProcessor()
    file_mgr = FileManager()

    user_count, api_limit = client.check_api_usage()
    if user_count is not None and api_limit is not None:
        logger.info(f"目前 FinMind API 使用量: {user_count}/{api_limit}")

    companies = load_companies_for_processing(args, file_mgr, read_mgr)
    if not companies:
        logger.info("沒有公司需要更新，結束。")
        return

    logger.info(f"準備處理 {len(companies)} 家公司。")

    start_date = (now_tw() - timedelta(days=_START_YEARS_BACK)).strftime("%Y-%m-%d")
    success_count = 0
    failed_companies = []

    for idx, company in enumerate(companies, 1):
        code = company["code"]
        name = company.get("name", code)

        try:
            ok = _update_one(client, processor, file_mgr, code, name, start_date)
            if ok:
                success_count += 1
                logger.info(f"[{idx}/{len(companies)}] ✔️  {code} {name}")
            else:
                logger.warning(f"[{idx}/{len(companies)}] ❌  {code} {name}")
                failed_companies.append(code)
        except ApiExhaustedError:
            handle_api_exhausted(
                "balance_sheet", batch, write_mgr,
                failed_companies, code, [c for c in companies[idx:]],
                success_count, len(companies), [],
            )
            return
        except Exception:
            logger.exception(f"  X 處理 {code} 時發生錯誤")
            failed_companies.append(code)

        if idx < len(companies):
            time.sleep(random.uniform(*config.DEFAULT_SLEEP_RANGE))

    logger.info(f"\n{'='*60}")
    logger.info(f"更新完成: {success_count}/{len(companies)} 家公司")
    if failed_companies:
        logger.warning(f"失敗: {len(failed_companies)} 家")
        write_mgr.save(failed_companies)
    else:
        write_mgr.clear()

    user_count, api_limit = client.check_api_usage()
    if user_count is not None and api_limit is not None:
        logger.info(f"最終 FinMind API 使用量: {user_count}/{api_limit}")
    logger.info(f"{'='*60}\n")


def _update_one(
    client: FinMindClient,
    processor: DataProcessor,
    file_mgr: FileManager,
    code: str,
    name: str,
    start_date: str,
) -> bool:
    """讀取現有 JSON → 抓 BS+CF → 計算指標 → 合併 → 存回。"""
    financial_data = file_mgr.load_financial_data(code)
    if not financial_data:
        logger.debug(f"  [{code}] 無現有 JSON，跳過")
        return True  # 無 JSON 不算失敗，full_update 還沒跑到而已

    annual_list = financial_data.get("historical", {}).get("annual", [])
    if not annual_list:
        logger.debug(f"  [{code}] annual 為空，跳過")
        return True

    # 抓資產負債表
    bs_by_year = {}
    bs_df, bs_ok = client.fetch_balance_sheet(code, start_date)
    if bs_ok and not bs_df.empty:
        bs_by_year = processor.process_balance_sheet(code, bs_df)

    # 抓現金流量表
    cf_by_year = {}
    cf_df, cf_ok = client.fetch_cash_flows_statement(code, start_date)
    if cf_ok and not cf_df.empty:
        cf_by_year = processor.process_cash_flows(code, cf_df)

    if not bs_by_year and not cf_by_year:
        logger.debug(f"  [{code}] BS + CF 均無資料，跳過")
        return True

    # 合併指標進 annual[]
    for item in annual_list:
        year = item.get("year")
        if year is None:
            continue

        net_income = item.get("netIncome", 0) or 0
        bs = bs_by_year.get(year, {})
        cf = cf_by_year.get(year, {})

        total_assets   = bs.get("totalAssets")
        total_liab     = bs.get("totalLiabilities")
        equity         = bs.get("equity")
        current_assets = bs.get("currentAssets")
        current_liab   = bs.get("currentLiabilities")
        ocf            = cf.get("ocf")
        capex          = cf.get("capex")

        item["roe"]          = round(net_income / equity * 100, 2)            if equity          and equity > 0          else None
        item["roa"]          = round(net_income / total_assets * 100, 2)      if total_assets    and total_assets > 0    else None
        item["currentRatio"] = round(current_assets / current_liab * 100, 2)  if current_assets  and current_liab and current_liab > 0 else None
        item["debtRatio"]    = round(total_liab / total_assets * 100, 2)      if total_liab      and total_assets > 0    else None
        item["ocf"]          = round(ocf, 0)                                  if ocf   is not None else None
        item["fcf"]          = round(ocf + capex, 0)                          if ocf   is not None and capex is not None else None

    financial_data["historical"]["annual"] = annual_list
    return file_mgr.save_financial_data(code, financial_data)
