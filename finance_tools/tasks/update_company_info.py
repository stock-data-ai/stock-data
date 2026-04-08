import logging
from core.file_manager import FileManager
from fetchers.company_info import CompanyInfoFetcher

logger = logging.getLogger(__name__)

def run_update_company_info(args):
    """
    處理更新所有公司基本資料的任務
    - 從爬蟲獲取所有上市櫃公司的基本資料。
    - 將整合後的資料儲存到 'companies-all.json'。
    """
    logger.info("Handling update for all company basic info...")
    fetcher = CompanyInfoFetcher()
    file_mgr = FileManager()

    # 1. Fetch all data
    all_companies_data = fetcher.fetch_all()

    # 2. Save all data to a single file
    if all_companies_data:
        file_mgr.save_all_companies_data(all_companies_data)
    else:
        logger.error("[ERROR] No company data was fetched. The output file will not be updated.")

    logger.info(f"\n{'='*60}")
    logger.info(f"Company info update process finished.")
    logger.info(f"{ '='*60}\n")
