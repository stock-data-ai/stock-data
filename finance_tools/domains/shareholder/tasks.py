import json
import os
import logging

from finance_tools.core.file_manager import FileManager
from finance_tools.core.timezone import today_str
from finance_tools.domains.shareholder.fetcher import fetch_all_tdcc_shareholding_via_api
from finance_tools.utils.company_list_loader import load_companies_for_processing
from finance_tools.utils.rerun_manager import RerunManager
from finance_tools.utils.quality_report import save_quality_report
import finance_tools.config as config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def run_fetch_shareholder_data(args):
    """
    處理抓取並儲存 TDCC 股權分散數據的任務 (API 全量更新模式)
    """
    logger.info("正在透過 TDCC API 抓取股權分散數據...")

    # RerunManager: 讀取用 combined file，寫入用 per-batch file
    batch = args.batch.split("/")[0] if getattr(args, "batch", None) else None
    read_mgr = RerunManager("shareholder")           # 讀: rerun_queue_shareholder.txt
    write_mgr = RerunManager("shareholder", batch)   # 寫: rerun_queue_shareholder_N.txt

    file_mgr = FileManager()

    # 1. 載入需要處理的公司列表 (受 batch/filter/force 等參數影響)
    all_potential_companies = load_companies_for_processing(args, file_mgr, read_mgr)
    if not all_potential_companies:
        logger.info("沒有選定公司進行處理。退出。")
        return

    # 2. 呼叫一次 API 獲取全市場資料 (這是新邏輯的核心：極速下載)
    tdcc_all_market_data = fetch_all_tdcc_shareholding_via_api()
    if not tdcc_all_market_data:
        logger.error("無法從 TDCC API 獲取任何資料。終止任務。")
        return

    # 3. 過濾出需要更新的公司 (除非 force，否則檢查是否已有數據)
    companies_to_process = []
    skipped_count = 0
    for comp in all_potential_companies:
        code = comp['code']
        if not args.force:
            financial_data = file_mgr.load_financial_data(code)
            if financial_data and financial_data.get('shareholderDataRecent'):
                # 檢查現有數據日期是否跟 API 日期一致 (優化：如果 API 的日期也已在 History，則可跳過)
                # 這裡暫時維持原 logic: 有資料就跳過，除非 --force
                logger.info(f"  ✓ 跳過公司 {code} {comp.get('name')} (已有數據)")
                skipped_count += 1
                continue
        companies_to_process.append(comp)

    if not companies_to_process:
        logger.info(f"所有 {len(all_potential_companies)} 間公司都已有數據。退出。")
        return

    logger.info(f"\n開始整合 {len(companies_to_process)} 間公司的數據 (已跳過 {skipped_count} 間)...")

    # 4. 處理並儲存
    success_count = 0
    failed_companies = []
    quality_issues = []

    for idx, company in enumerate(companies_to_process, 1):
        code = company['code']
        name = company.get('name', "N/A")
        
        # 從全市場資料中取出該公司的部分
        tdcc_data_list = tdcc_all_market_data.get(code)
        
        if tdcc_data_list:
            try:
                # 準備整合進財務檔案
                financial_data = file_mgr.load_financial_data(code)
                if not financial_data:
                    financial_data = {
                        "companyCode": code,
                        "companyName": name,
                        "latest": {},
                        "historical": {},
                        "shareholderDataHistory": {}
                    }

                if 'shareholderDataHistory' not in financial_data:
                    financial_data['shareholderDataHistory'] = {}

                # 取得資料日期 (API 格式 YYYYMMDD)
                raw_date = tdcc_data_list[0].get('data_date')
                # 轉換為 YYYY-MM-DD
                formatted_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
                
                # 清理記錄 (移除冗餘欄位後存入 history)
                clean_records = []
                for r in tdcc_data_list:
                    clean_records.append({
                        "序": r["序"],
                        "holding_range": r["holding_range"],
                        "holder_count": r["holder_count"],
                        "shares": r["shares"],
                        "ratio_pct": r["ratio_pct"]
                    })

                # 更新歷史與最新資料
                financial_data['shareholderDataHistory'][formatted_date] = clean_records
                financial_data['shareholderDataRecent'] = clean_records
                financial_data['lastUpdated'] = today_str()

                if file_mgr.save_financial_data(code, financial_data):
                    logger.info(f"[{idx}/{len(companies_to_process)}] OK {code} {name}")
                    success_count += 1
                else:
                    logger.error(f"[{idx}/{len(companies_to_process)}] X {code} {name} 儲存失敗")
                    failed_companies.append(code)
            except Exception as e:
                logger.error(f"  ❌ 處理 {code} 時發生錯誤: {e}")
                failed_companies.append(code)
        else:
            # 沒在 API 裡找到 (可能是新股、減資中、或非數字代碼)
            if code.isdigit():
                logger.warning(f"  ! API 中查無 {code} {name} 的資料")
                quality_issues.append(f"{code} {name}: API 無資料")
            success_count += 1 # 視為完成(跳過)

    # 最終儲存品質報告
    save_quality_report("shareholder", batch, quality_issues)

    logger.info(f"\n{'='*60}")
    logger.info(f"OK 完成: {success_count}/{len(companies_to_process)} 間公司")
    if failed_companies:
        write_mgr.save(failed_companies)
    else:
        write_mgr.clear()

    logger.info(f"{'='*60}\n")
