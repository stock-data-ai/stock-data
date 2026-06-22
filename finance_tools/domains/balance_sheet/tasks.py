"""
Balance Sheet & Cash Flow Update Task
資產負債表 + 現金流量表 → ROE/ROA/流動比率/負債比率/OCF/FCF

獨立工作流，讀取現有 company-financials JSON，
補充 annual[] 中各年份的財務健全度指標後存回。
"""

import logging
import os
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

    _save_stats("balance_sheet", batch, success_count, len(companies))

    user_count, api_limit = client.check_api_usage()
    if user_count is not None and api_limit is not None:
        logger.info(f"最終 FinMind API 使用量: {user_count}/{api_limit}")
    logger.info(f"{'='*60}\n")


def _save_stats(task_name: str, batch, success: int, total: int) -> None:
    """寫出統計數字，供 merge job 彙整後顯示於 GitHub Summary。"""
    suffix = f"_{batch}" if batch else ""
    stats_path = os.path.join(str(config.RERUN_DIR), f"stats_{task_name}{suffix}.txt")
    with open(stats_path, "w", encoding="utf-8") as f:
        f.write(f"fin=0\nrev=0\nsuccess={success}\ntotal={total}\n")


def _update_one(
    client: FinMindClient,
    processor: DataProcessor,
    file_mgr: FileManager,
    code: str,
    name: str,
    start_date: str,
) -> bool:
    """讀取現有 JSON → 抓 BS+CF → 計算指標 → 合併 → 存回。

    - currentRatio / debtRatio → quarterly[]（季度快照，每季末更新）
    - ROE / ROA / OCF / FCF   → annual[]（全年累計，僅 Q4）
    """
    financial_data = file_mgr.load_financial_data(code)
    if not financial_data:
        logger.debug(f"  [{code}] 無現有 JSON，跳過")
        return True

    annual_list    = financial_data.get("historical", {}).get("annual", [])
    quarterly_list = financial_data.get("historical", {}).get("quarterly", [])
    if not annual_list:
        logger.debug(f"  [{code}] annual 為空，跳過")
        return True

    # ── 抓資產負債表（所有季度）──────────────────────────────────────
    bs_by_yq: dict = {}  # {(year, quarter): {totalAssets, ...}}
    bs_df, bs_ok = client.fetch_balance_sheet(code, start_date)
    if bs_ok and not bs_df.empty:
        bs_by_yq = processor.process_balance_sheet(code, bs_df)

    # ── 抓現金流量表（年度，Q4 累計）────────────────────────────────
    cf_by_year: dict = {}
    cf_df, cf_ok = client.fetch_cash_flows_statement(code, start_date)
    if cf_ok and not cf_df.empty:
        cf_by_year = processor.process_cash_flows(code, cf_df)

    if not bs_by_yq and not cf_by_year:
        logger.debug(f"  [{code}] BS + CF 均無資料，跳過")
        return True

    # ── 合併 currentRatio / debtRatio → quarterly[] ──────────────────
    # 建立快速查詢 dict，key = (year, quarter)
    qmap: dict = {(q["year"], q["quarter"]): q for q in quarterly_list if "quarter" in q}

    for (year, quarter), bs in bs_by_yq.items():
        current_assets = bs.get("currentAssets")
        current_liab   = bs.get("currentLiabilities")
        total_assets   = bs.get("totalAssets")
        total_liab     = bs.get("totalLiabilities")

        cr = round(current_assets / current_liab * 100, 2) if current_assets and current_liab and current_liab > 0 else None
        dr = round(total_liab / total_assets * 100, 2)     if total_liab and total_assets and total_assets > 0    else None

        if (year, quarter) in qmap:
            qmap[(year, quarter)]["currentRatio"] = cr
            qmap[(year, quarter)]["debtRatio"]    = dr
        else:
            # quarterly[] 可能沒有這季（例如只有年度資料），新增一筆
            new_q = {"year": year, "quarter": quarter, "currentRatio": cr, "debtRatio": dr}
            qmap[(year, quarter)] = new_q
            quarterly_list.append(new_q)

    # ── 合併 ROE / ROA / OCF / FCF → annual[]（Q4 年度數字）────────
    for item in annual_list:
        year = item.get("year")
        if year is None:
            continue

        net_income = item.get("netIncome", 0) or 0
        bs_q4      = bs_by_yq.get((year, 4), {})
        cf         = cf_by_year.get(year, {})

        total_assets = bs_q4.get("totalAssets")
        total_liab   = bs_q4.get("totalLiabilities")
        equity       = bs_q4.get("equity")
        ocf          = cf.get("ocf")
        capex        = cf.get("capex")

        item["roe"] = round(net_income / equity * 100, 2)       if equity      and equity > 0      else None
        item["roa"] = round(net_income / total_assets * 100, 2) if total_assets and total_assets > 0 else None
        item["ocf"] = round(ocf, 0)                             if ocf   is not None else None
        item["fcf"] = round(ocf + capex, 0)                     if ocf is not None and capex is not None else None
        # debtRatio 也放年度（Q4 快照），方便年度表格顯示
        item["debtRatio"] = round(total_liab / total_assets * 100, 2) if total_liab and total_assets and total_assets > 0 else None

    financial_data["historical"]["annual"]    = annual_list
    financial_data["historical"]["quarterly"] = sorted(
        quarterly_list, key=lambda q: (q["year"], q.get("quarter", 0)), reverse=True
    )
    return file_mgr.save_financial_data(code, financial_data)
