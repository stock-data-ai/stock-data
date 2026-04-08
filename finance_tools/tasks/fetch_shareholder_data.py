import json
import os
import logging

from core.file_manager import FileManager
from core.timezone import today_str
from fetchers.tdcc_fetcher import fetch_multiple_stocks
from utils.company_list_loader import load_companies_for_processing
from utils.rerun_manager import RerunManager
from utils.quality_report import save_quality_report
import config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def run_fetch_shareholder_data(args):
    """
    處理抓取並儲存 TDCC 股權分散數據的任務
    - 除非使用 --force，否則會跳過已有 shareholderDataRecent 資料的公司。
    """
    logger.info("正在抓取並儲存 TDCC 股權分散數據...")

    # RerunManager: 讀取用 combined file，寫入用 per-batch file
    batch = args.batch.split("/")[0] if getattr(args, "batch", None) else None
    read_mgr = RerunManager("shareholder")           # 讀: rerun_queue_shareholder.txt
    write_mgr = RerunManager("shareholder", batch)   # 寫: rerun_queue_shareholder_N.txt

    file_mgr = FileManager()

    # 1. Get the initial list of all potential companies
    all_potential_companies = load_companies_for_processing(args, file_mgr, read_mgr)
    if not all_potential_companies:
        logger.info("沒有選定公司進行處理。退出。")
        return

    # Filter out non-numeric codes (e.g., US stocks like 'AAPL')
    # TDCC only provides data for TW stocks (numeric codes)
    numeric_companies = []
    for comp in all_potential_companies:
        if comp['code'].isdigit():
            numeric_companies.append(comp)

    if len(numeric_companies) < len(all_potential_companies):
        logger.info(f"已過濾掉 {len(all_potential_companies) - len(numeric_companies)} 間非數字代碼 (如美股) 的公司。")

    all_potential_companies = numeric_companies

    if not all_potential_companies:
        logger.info("沒有找到有效的數字公司代碼進行處理。退出。")
        return

    # 2. Filter out companies that should be skipped
    companies_to_fetch = []
    skipped_count = 0
    if not args.force:
        logger.info("正在檢查現有數據... (使用 --force 可強制覆蓋)")
        for company in all_potential_companies:
            financial_data = file_mgr.load_financial_data(company['code'])
            if financial_data and financial_data.get('shareholderDataRecent'):
                logger.info(f"  ✓ 跳過公司 {company['code']} {company['name']} (已有數據)")
                skipped_count += 1
                continue
            companies_to_fetch.append(company)
    else:
        logger.info("已設定強制更新旗標。所有選定公司都將被更新。")
        companies_to_fetch = all_potential_companies

    if not companies_to_fetch:
        logger.info(f"所有 {len(all_potential_companies)} 間公司都已有股權分散數據。退出。")
        return

    logger.info(f"\n找到 {len(companies_to_fetch)} 間需要更新的公司 (已跳過 {skipped_count} 間)。")

    # 3. Fetch data only for the filtered list
    codes_to_process = [c['code'] for c in companies_to_fetch]
    company_map = {c['code']: c['name'] for c in companies_to_fetch}

    logger.info(f"正在為 {len(codes_to_process)} 間公司啟動並行抓取...")
    tdcc_results = fetch_multiple_stocks(codes_to_process, max_dates=12, headless=True)
    logger.info("並行抓取完成。開始將數據整合到財務檔案中...")

    # 4. Process and save the results
    success_count = 0
    failed_companies = []
    quality_issues = []

    for idx, code in enumerate(codes_to_process, 1):
        name = company_map.get(code, "N/A")
        tdcc_data_list = tdcc_results.get(code)
        if tdcc_data_list:
            try:
                # Group data by date
                data_by_date = {}
                for record in tdcc_data_list:
                    date_str = record.get('data_date')
                    if date_str:
                        if date_str not in data_by_date:
                            data_by_date[date_str] = []
                        # Keep only relevant fields
                        clean_record = {k: v for k, v in record.items() if k not in ['data_date', 'stock_id']}
                        data_by_date[date_str].append(clean_record)

                if not data_by_date:
                    logger.warning(f"  ! 未找到 {code} 可處理的 TDCC 數據記錄。跳過整合。")
                    quality_issues.append(f"{code} {name}: 缺失股權分散數據 (API 記錄為空)")
                    success_count += 1
                    continue

                financial_data = file_mgr.load_financial_data(code)
                if not financial_data:
                    logger.info(f"  ! 未找到 {code} 的現有財務檔案。正在創建新檔案。")
                    financial_data = {
                        "companyCode": code,
                        "companyName": name,
                        "latest": {},
                        "historical": {}
                    }

                # Ensure shareholderDataHistory exists
                if 'shareholderDataHistory' not in financial_data:
                    financial_data['shareholderDataHistory'] = {}

                # Integrate new data
                for date_slash, records in data_by_date.items():
                    date_dash = date_slash.replace('/', '-')
                    financial_data['shareholderDataHistory'][date_dash] = records

                # Update shareholderDataRecent to the latest data
                if financial_data['shareholderDataHistory']:
                    latest_date = max(financial_data['shareholderDataHistory'].keys())
                    financial_data['shareholderDataRecent'] = financial_data['shareholderDataHistory'][latest_date]

                financial_data['lastUpdated'] = today_str()

                if file_mgr.save_financial_data(code, financial_data):
                    logger.info(f"[{idx}/{len(codes_to_process)}] OK {code} {name}")
                    success_count += 1
                else:
                    logger.error(f"\n{'='*60}")
                    logger.error(f"[{idx}/{len(codes_to_process)}] X {code} {name} 儲存失敗")
                    failed_companies.append(code)
            except Exception:
                logger.exception(f"  ❌ 處理 {code} 的 TDCC 數據時發生錯誤:")
                failed_companies.append(code)
        else:
            logger.warning(f"  ! 未從 {code} 抓取到 TDCC 數據。跳過整合。")
            quality_issues.append(f"{code} {name}: 缺失股權分散數據 (抓取不到資料)")
            success_count += 1
            continue

    # Final save quality report
    save_quality_report("shareholder", batch, quality_issues)

    logger.info(f"\n{'='*60}")
    logger.info(f"OK 完成: {success_count}/{len(codes_to_process)} 間公司")
    if failed_companies:
        logger.warning(f"X 處理失敗: {len(failed_companies)} 間公司")
        unique_failed_companies = sorted(list(set(failed_companies)))
        logger.warning(f"   失敗公司代碼 (前10個): {', '.join(unique_failed_companies[:10])}{'...' if len(unique_failed_companies) > 10 else ''}")
        write_mgr.save(failed_companies)
    else:
        write_mgr.clear()

    logger.info(f"{'='*60}\n")