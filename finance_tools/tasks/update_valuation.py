import os
import json
import sys
import time
import random
import logging

from core.file_manager import FileManager
from core.timezone import today_str
from utils.company_list_loader import load_companies_for_processing, filter_already_updated
from core.api_client import FinMindClient, ApiExhaustedError
from fetchers.per_pbr import fetch_per_pbr
from utils.rerun_manager import RerunManager
from utils.quality_report import save_quality_report
import config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DEFAULT_SLEEP_RANGE = config.DEFAULT_SLEEP_RANGE

def run_update_valuation(args):
    """
    處理更新 PE/PB 估值任務
    - 遍歷指定或所有公司的 financial data 檔案。
    - 從 FinMind API 獲取最新的 PE/PB 值。
    - 將新值寫回 JSON 檔案中。
    """
    logger.info("正在處理 PE/PB 估值更新任務...")

    # RerunManager: 讀取用 combined file，寫入用 per-batch file
    batch = args.batch.split("/")[0] if getattr(args, "batch", None) else None
    read_mgr = RerunManager("valuation")           # 讀: rerun_queue_valuation.txt
    write_mgr = RerunManager("valuation", batch)   # 寫: rerun_queue_valuation_N.txt

    client = FinMindClient()
    file_mgr = FileManager()

    # Display current API usage at the start
    user_count, api_limit = client.check_api_usage()
    if user_count is not None and api_limit is not None:
        logger.info(f"當前 FinMind API 用量: {user_count}/{api_limit} 次請求。")
    else:
        logger.warning("無法取得 FinMind API 用量資訊。")

    # Use the centralized company list loader
    companies_to_update = load_companies_for_processing(args, file_mgr, read_mgr)

    if not companies_to_update:
        logger.info("沒有選定公司進行估值更新。退出。")
        return

    is_force_update = getattr(args, 'force', False)
    companies_to_update = filter_already_updated(companies_to_update, file_mgr, force_update=is_force_update)
    if not companies_to_update:
        logger.info("所有公司均已於今日更新，無需處理。")
        write_mgr.clear()
        return

    logger.info(f"找到 {len(companies_to_update)} 間公司準備更新估值指標。")

    success_count = 0
    failed_companies = []
    quality_issues = []

    for idx, company in enumerate(companies_to_update, 1):
        code = company["code"]
        name = company["name"]

        try:
            financial_data = file_mgr.load_financial_data(code)
            if not financial_data:
                logger.warning(f"  ! 未找到 {code} 的財務檔案，跳過。")
                failed_companies.append(code)
                continue

            # --- Smart Update Logic (safety net if pre-filter missed) ---
            if not is_force_update:
                last_updated = financial_data.get("lastUpdated")
                if last_updated == today_str():
                    logger.info(f"  ✓ 跳過公司 {code} (已於今日更新)")
                    success_count += 1
                    continue
            # --- End Smart Update Logic ---

            per_pbr_row, success = fetch_per_pbr(code, client)
            if not success:
                logger.error(f"  X 抓取 {code} 的 P/E P/B 數據失敗，跳過。")
                failed_companies.append(code)
                time.sleep(random.uniform(*DEFAULT_SLEEP_RANGE))
                continue

            if 'latest' not in financial_data:
                financial_data['latest'] = {}

            if per_pbr_row is not None:
                financial_data['latest']['pe'] = float(per_pbr_row.get('PER', 0))
                financial_data['latest']['pb'] = float(per_pbr_row.get('PBR', 0))
            else:
                quality_issues.append(f"{code} {name}: 缺失估值資料 (PE/PB 為空)")

            financial_data['lastUpdated'] = today_str()

            # 使用 FileManager 的原子寫入方法
            if not file_mgr.save_financial_data(code, financial_data):
                logger.error(f"  X 儲存 {code} 的數據失敗，跳過。")
                failed_companies.append(code)
                continue

            logger.info(f"[{idx}/{len(companies_to_update)}] OK {code} {name}")
            success_count += 1

            if idx < len(companies_to_update):
                time.sleep(random.uniform(*DEFAULT_SLEEP_RANGE))

        except ApiExhaustedError:
            logger.warning(f"\n⚠️  API 額度已耗盡，發生於公司 {code}。所有可用 token 皆已用盡。")
            remaining = companies_to_update[idx:]
            
            save_quality_report("valuation", batch, quality_issues)
            write_mgr.save_api_exhausted(failed_companies, code, remaining)
            logger.info(f"本輪已完成: {success_count}/{len(companies_to_update)} 間公司。")
            logger.info("退出，等待 GitHub Actions 觸發下一輪重試。")
            sys.exit(1)
        except Exception as e:
            logger.exception(f"  X 處理 {code} 時發生未預期錯誤:")
            failed_companies.append(code)

    # Final save quality report
    save_quality_report("valuation", batch, quality_issues)

    logger.info(f"\n{'='*60}")
    logger.info(f"OK 更新完成: {success_count}/{len(companies_to_update)} 間公司")
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