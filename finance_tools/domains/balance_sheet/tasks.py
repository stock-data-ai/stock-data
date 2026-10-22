"""
Balance Sheet & Cash Flow Update Task
資產負債表 + 現金流量表 → ROE/ROA/流動比率/負債比率/OCF/FCF

獨立工作流，讀取現有 company-financials JSON，
補充 annual[] 中各年份的財務健全度指標後存回。
"""

import logging
import os
import sys

import pandas as pd

from finance_tools.core import FinMindClient, DataProcessor, FileManager
from finance_tools.core import market_fetch
from finance_tools.core.exceptions import ApiError, ApiExhaustedError
from finance_tools.core.timezone import now_tw
from finance_tools.utils.company_list_loader import load_companies_for_processing
from finance_tools.utils.rerun_manager import RerunManager
import finance_tools.config as config

logger = logging.getLogger(__name__)


def fetch_market_balance_sheet_and_cash_flows(client: FinMindClient):
    """全市場抓資產負債表與現金流量表（完整視窗），回傳依代號分組的兩個 dict。"""
    dates = market_fetch.quarter_end_dates(config.BALANCE_SHEET_YEARS, now_tw().date())
    bs = market_fetch.fetch_market(
        client, "TaiwanStockBalanceSheet", dates,
        keep=market_fetch.item_filter(DataProcessor.BALANCE_SHEET_ITEMS),
    )
    cf = market_fetch.fetch_market(
        client, "TaiwanStockCashFlowsStatement", dates,
        keep=market_fetch.item_filter(DataProcessor.CASH_FLOW_ITEMS),
    )
    return market_fetch.split_by_stock(bs), market_fetch.split_by_stock(cf)


def run_update_balance_sheet(args):
    """【定期】全市場抓資產負債表 + 現金流量表，計算並合併 ROE/ROA/比率/OCF/FCF。"""
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

    # 1. 全市場抓取。任何一期失敗就整個停下來，一個檔都不寫。
    try:
        bs_by_code, cf_by_code = fetch_market_balance_sheet_and_cash_flows(client)
    except ApiExhaustedError as e:
        logger.warning(f"⚠️  FinMind 額度用盡：{e}；全部 {len(companies)} 家留待下一輪重跑。")
        write_mgr.save([c["code"] for c in companies])
        sys.exit(2)  # exit 2 = API exhausted (rerun needed), not a real error
    except (ApiError, Exception) as e:
        logger.error(f"❌ 全市場抓取失敗，本輪不寫任何檔：{e}")
        sys.exit(1)

    # 2. 逐家合併寫檔（純本地運算，不再打 API）
    empty = pd.DataFrame()
    success_count = 0
    failed_companies = []
    for idx, company in enumerate(companies, 1):
        code = company["code"]
        name = company.get("name", code)
        try:
            ok = _update_one(processor, file_mgr, code, name,
                             bs_by_code.get(code, empty), cf_by_code.get(code, empty))
        except Exception:
            logger.exception(f"  X 處理 {code} 時發生錯誤")
            ok = False
        if ok:
            success_count += 1
        else:
            logger.warning(f"[{idx}/{len(companies)}] ❌  {code} {name}")
            failed_companies.append(code)

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


def _upsert_quarter(qmap: dict, quarterly_list: list, year: int, quarter: int, fields: dict) -> None:
    """把欄位寫進 quarterly[] 的 (year, quarter)；沒有這季（例如只有年度資料）就新增一筆。"""
    if (year, quarter) in qmap:
        qmap[(year, quarter)].update(fields)
    else:
        new_q = {"year": year, "quarter": quarter, **fields}
        qmap[(year, quarter)] = new_q
        quarterly_list.append(new_q)


def _update_one(
    processor: DataProcessor,
    file_mgr: FileManager,
    code: str,
    name: str,
    bs_df: pd.DataFrame,
    cf_df: pd.DataFrame,
) -> bool:
    """讀取現有 JSON → 用分到這家的 BS+CF 計算指標 → 合併 → 存回。

    - currentRatio / debtRatio → quarterly[]（季度快照，每季末更新）
    - ROE / ROA / OCF / FCF   → annual[]（全年累計，僅 Q4）
    bs_df／cf_df 為空（來源沒有這家）時什麼都不動。
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

    # ── 資產負債表（所有季度）──────────────────────────────────────
    bs_by_yq: dict = {}  # {(year, quarter): {totalAssets, ...}}
    if not bs_df.empty:
        bs_by_yq = processor.process_balance_sheet(code, bs_df)

    # ── 現金流量表（年度取 Q4 累計；季度拆成單季）────────────────────────────────
    cf_by_year: dict = {}
    cf_by_yq: dict = {}  # {(year, quarter): {ocf, capex}}，單季
    if not cf_df.empty:
        cf_by_year = processor.process_cash_flows(code, cf_df)
        # 單期現金流要對齊損益表實際申報的期別（半年報公司只有 Q2／Q4，見 process_quarterly_cash_flows）
        reported = {(q["year"], q["quarter"]) for q in quarterly_list
                    if "quarter" in q and q.get("revenue") is not None}
        cf_by_yq = processor.process_quarterly_cash_flows(code, cf_df, reported or None)

    if not bs_by_yq and not cf_by_year and not cf_by_yq:
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

        # 應收帳款＝票據＋帳款＋關係人帳款；主科目「應收帳款淨額」缺就整欄 None，
        # 不拿只剩票據的數字冒充。
        trade = bs.get("tradeReceivable")
        ar = (
            round(trade + (bs.get("notesReceivable") or 0) + (bs.get("tradeReceivableRelated") or 0), 0)
            if trade is not None else None
        )
        inv = bs.get("inventory")
        inv = round(inv, 0) if inv is not None else None

        fields = {"currentRatio": cr, "debtRatio": dr, "accountsReceivable": ar, "inventory": inv}
        _upsert_quarter(qmap, quarterly_list, year, quarter, fields)

    # ── 合併單季 OCF / capex / FCF → quarterly[] ───────────────────────
    # 只寫來源這次有回的季度，沒回的季度保留舊值（與年度欄位同一原則）。
    for (year, quarter), cf in cf_by_yq.items():
        ocf, capex = cf.get("ocf"), cf.get("capex")
        fcf = round(ocf + capex, 0) if ocf is not None and capex is not None else None
        _upsert_quarter(qmap, quarterly_list, year, quarter, {"ocf": ocf, "capex": capex, "fcf": fcf})

    # ── 合併 ROE / ROA / OCF / FCF → annual[]（Q4 年度數字）────────
    # 只在「來源這次真的回了這個年度」時才寫。抓取視窗是 7 年、單次呼叫也可能失敗，
    # 無條件寫 None 會把上次存好的歷史逐年抹掉：正式資料上 2022–2025 幾乎每檔都有 ocf，
    # 2021 卻有 800 檔只剩 roe——就是視窗前緣每跑一次往前吃一年。
    # 來源有回該年度但算不出比率（權益 0／負數）時仍寫 None，那是真的「算不出來」。
    # 對照 `data_assembler.build_final_data` 的 `_PRESERVE`：這些欄位本來就設計成跨次保留。
    for item in annual_list:
        year = item.get("year")
        if year is None:
            continue

        net_income = item.get("netIncome", 0) or 0
        bs_q4      = bs_by_yq.get((year, 4))
        cf         = cf_by_year.get(year)

        if bs_q4 is not None:
            total_assets = bs_q4.get("totalAssets")
            total_liab   = bs_q4.get("totalLiabilities")
            equity       = bs_q4.get("equity")

            item["roe"] = round(net_income / equity * 100, 2)       if equity       and equity > 0       else None
            item["roa"] = round(net_income / total_assets * 100, 2) if total_assets and total_assets > 0 else None
            # debtRatio 也放年度（Q4 快照），方便年度表格顯示
            item["debtRatio"] = round(total_liab / total_assets * 100, 2) if total_liab and total_assets and total_assets > 0 else None

        if cf is not None:
            ocf   = cf.get("ocf")
            capex = cf.get("capex")

            item["ocf"] = round(ocf, 0)             if ocf is not None else None
            item["fcf"] = round(ocf + capex, 0)     if ocf is not None and capex is not None else None

    financial_data["historical"]["annual"]    = annual_list
    financial_data["historical"]["quarterly"] = sorted(
        quarterly_list, key=lambda q: (q["year"], q.get("quarter", 0)), reverse=True
    )
    return file_mgr.save_financial_data(code, financial_data)
