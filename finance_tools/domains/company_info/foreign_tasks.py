"""
Foreign Company Update Tasks - 用於更新外國公司基本資料的任務邏輯
"""
import sys
import logging
from typing import Type
from finance_tools.core.file_manager import FileManager

logger = logging.getLogger(__name__)
from .foreign_base import BaseForeignCompanyFetcher
from .foreign_jp import JPCompanyInfoFetcher
from .foreign_us import USCompanyInfoFetcher

def run_update_foreign_company_task(
    args,
    fetcher_cls: Type[BaseForeignCompanyFetcher],
    task_name: str
):
    """
    通用任務函式：更新外國公司基本資料
    
    Args:
        args: 命令列參數物件
        fetcher_cls: 爬蟲類別 (JPCompanyInfoFetcher 或 USCompanyInfoFetcher)
        task_name: 任務名稱 (用於 log 顯示)
    """
    logger.info(f"Handling update for {task_name} company basic info...")
    
    fetcher = fetcher_cls()
    file_mgr = FileManager()

    # 支援 --code 單一公司參數
    codes = None
    if hasattr(args, "code") and args.code:
        # 確保代碼為大寫 (有些 user 輸入習慣)
        codes = [args.code.upper()]

    # 1. Fetch data
    fetched_data = fetcher.fetch_all(codes=codes)

    if not fetched_data:
        logger.warning(f"No {task_name} company data was fetched.")
        return

    # 2. Load existing companies-all.json and merge
    # 我們不希望覆蓋掉其他的台股資料，所以先讀取現有的
    all_companies = file_mgr.load_all_companies_with_details()
    
    # Merge new data into existing data, preserving manually-set shortName
    for code, new_data in fetched_data.items():
        existing = all_companies.get(code, {})
        if existing.get("shortName"):
            new_data["shortName"] = existing["shortName"]
            if new_data.get("gov", {}).get("profile"):
                new_data["gov"]["profile"]["companyShortName"] = existing["shortName"]
        all_companies[code] = new_data

    # 3. Save back
    if file_mgr.save_all_companies_data(all_companies):
        logger.info(f"{task_name} company info update finished. Added/updated {len(fetched_data)} companies.")
    else:
        logger.error(f"Failed to save {task_name} company data.")
        sys.exit(1)


def run_update_jp_company_info(args):
    """進入點：更新日本公司資料"""
    run_update_foreign_company_task(args, JPCompanyInfoFetcher, "JP")

def run_update_us_company_info(args):
    """進入點：更新美國公司資料"""
    run_update_foreign_company_task(args, USCompanyInfoFetcher, "US")
