import os
import logging
import json
import config
from utils.rerun_manager import RerunManager

logger = logging.getLogger(__name__)

def run_check_quality(args):
    """
    處理數據品質檢查任務
    - 檢查 company-financials 目錄下的所有 JSON 檔案。
    - 找出 dataQuality 為 "low" 的公司。
    - 將這些公司的代碼寫入 rerun queue 以便後續處理。
    """
    OUTPUT_DIR = str(config.COMPANY_FINANCIALS_DIR)

    rerun_mgr = RerunManager("quality")

    logger.info("Checking data quality...")
    low_quality_companies = []

    if not os.path.exists(OUTPUT_DIR):
        logger.error(f"Output directory not found: {OUTPUT_DIR}")
        return

    for filename in os.listdir(OUTPUT_DIR):
        if filename.endswith(".json"):
            file_path = os.path.join(OUTPUT_DIR, filename)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("dataQuality") == "low":
                        low_quality_companies.append({
                            "code": data.get("companyCode"),
                            "name": data.get("companyName"),
                            "file": filename
                        })
            except Exception as e:
                logger.error(f"Error reading {filename}: {e}")

    if low_quality_companies:
        logger.warning("Companies with low data quality:")
        codes_to_rerun = []
        for company in low_quality_companies:
            logger.warning(f"- {company['code']} {company['name']} (File: {company['file']})")
            if company['code']:
                codes_to_rerun.append(company['code'])

        rerun_mgr.save(codes_to_rerun)
        logger.warning(f"\nFound {len(codes_to_rerun)} companies with issues. Their codes have been saved to '{rerun_mgr.file_path}' for the rerun process.")

    else:
        logger.info("All processed companies have high data quality.")
        rerun_mgr.clear()
