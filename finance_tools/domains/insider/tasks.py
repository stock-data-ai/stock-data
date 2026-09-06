"""內部人持股：每期全量寫入各公司的 company-financials 檔。

一次 request 拿全市場（三個板別各一支），再逐家併進財報檔——與 TDCC 股權分散
（shareholder/tasks.py）完全同一個形狀。

**為什麼不另外落成獨立檔案**：stock_map 的公司頁本來就整包下載
`company-financials/{code}.json`（2330 約 976 KB，帶 ETag 條件請求），
把 4 KB 併進去等於零額外請求；另開新檔則前端要多打一次 HTTP。
"""

import logging
import sys

from finance_tools.core.file_manager import FileManager
from finance_tools.domains.insider.fetcher import fetch_all_insider_holdings
from finance_tools.orchestration.data_assembler import DataAssembler
from finance_tools.utils.company_list_loader import load_companies_for_processing

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def run_update_insider_holdings(args):
    """抓內部人持股並併進各公司財報檔。"""
    file_mgr = FileManager()

    # 一次拿全市場，分批沒有意義（同股權分散表）
    if not getattr(args, "code", None):
        if getattr(args, "batch", None):
            logger.info("內部人持股一次拿全市場，忽略批次參數以提高效率。")
            args.batch = None

    companies = load_companies_for_processing(args, file_mgr, None)
    if not companies:
        logger.info("沒有選定公司進行處理。退出。")
        return

    all_market = fetch_all_insider_holdings()
    if not all_market:
        logger.error("無法取得內部人持股資料，終止任務。")
        sys.exit(1)

    updated = skipped_no_data = failed = 0
    for company in companies:
        code = company['code']
        holdings = all_market.get(code)
        if not holdings:
            # 外國公司、剛上市尚未申報等情形；不是錯誤
            skipped_no_data += 1
            continue

        try:
            financial_data = file_mgr.load_financial_data(code) or {
                "companyCode": code,
                "companyName": company.get('name', "N/A"),
                "latest": {},
                "historical": {},
            }
            merged = DataAssembler.merge_insider_holdings(financial_data, holdings)
            if file_mgr.save_financial_data(code, merged):
                updated += 1
            else:
                failed += 1
        except Exception as e:
            logger.error(f"  {code}：寫入失敗 {e}")
            failed += 1

    logger.info(
        f"內部人持股完成：更新 {updated} 家、無資料 {skipped_no_data} 家、失敗 {failed} 家"
    )
    # 全市場資料明明抓到了卻一家都沒寫進去 = 對照不上（例如公司清單空了），要吵出來
    if updated == 0:
        logger.error("一家都沒更新——公司清單與全市場資料對不上，請人工確認。")
        sys.exit(1)
