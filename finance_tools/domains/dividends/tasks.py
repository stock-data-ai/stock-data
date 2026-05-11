import logging
import time
from typing import Optional
from finance_tools.core.file_manager import FileManager
from finance_tools.core.timezone import today_str
from finance_tools.domains.dividends.finmind_fetcher import FinMindDividendFetcher
from finance_tools.utils.company_list_loader import load_companies_for_processing
from finance_tools.utils.rerun_manager import RerunManager

logger = logging.getLogger(__name__)


def _infer_frequency(records: list) -> Optional[str]:
    periods = [r.get("period", "") or "" for r in records]
    if any("季" in p for p in periods):
        return "季"
    if any("半年" in p for p in periods):
        return "半年"
    if any(p for p in periods):
        return "年"
    return None


def run_import_dividends(args):
    """從 FinMind 全量匯入股利（覆蓋重建），自動推算 dividendFrequency。支援 --batch/--rerun。"""
    logger.info("Starting FinMind dividend import...")

    file_mgr = FileManager()
    batch = getattr(args, "batch", None)
    rerun_manager = RerunManager("import_dividends", batch)
    companies = load_companies_for_processing(args, file_mgr, rerun_manager)

    if not companies:
        logger.info("沒有公司需要處理。")
        return

    # --resume：跳過今天已成功寫入 dividendsUpdated 的公司
    if getattr(args, "resume", False):
        today = today_str()
        before = len(companies)
        companies = [
            c for c in companies
            if not (lambda d: d is not None and d.get("dividendsUpdated") == today)(
                file_mgr.load_financial_data(c["code"])
            )
        ]
        logger.info(f"Resume mode: 跳過 {before - len(companies)} 家，剩餘 {len(companies)} 家。")

    fetcher = FinMindDividendFetcher()
    success_count = 0
    failed = []
    total = len(companies)

    for i, company in enumerate(companies, 1):
        code = company["code"]
        name = company.get("name", code)

        if i % 50 == 0:
            logger.info(f"  進度: {i}/{total}，已成功: {success_count}")

        records = fetcher.fetch_one(code)
        time.sleep(fetcher.REQUEST_DELAY)

        if records is None:
            failed.append(code)
            continue

        financial_data = file_mgr.load_financial_data(code)
        if not financial_data:
            financial_data = {
                "companyCode": code,
                "companyName": name,
                "latest": {},
                "historical": {"annual": [], "quarterly": [], "monthlyRevenue": [], "dividends": []},
            }
        if "historical" not in financial_data:
            financial_data["historical"] = {}
        if "latest" not in financial_data:
            financial_data["latest"] = {}

        # merge：新資料的 (year, seq) 覆蓋舊資料，其餘年份保留
        existing_divs = financial_data["historical"].get("dividends") or []
        new_keys = {(r["year"], r.get("sequence", 1)) for r in records}
        kept_old = [d for d in existing_divs if (d["year"], d.get("sequence", 1)) not in new_keys]
        financial_data["historical"]["dividends"] = sorted(
            records + kept_old, key=lambda r: (r["year"], r.get("sequence", 1)), reverse=True
        )

        freq = _infer_frequency(records)
        if freq:
            financial_data["latest"]["dividendFrequency"] = freq

        today = today_str()
        financial_data["dividendsUpdated"] = today
        financial_data["lastUpdated"] = today

        if file_mgr.save_financial_data(code, financial_data):
            success_count += 1
        else:
            logger.error(f"  X Failed to save {code}")
            failed.append(code)

    rerun_manager.save(failed)

    logger.info(f"\n{'='*60}")
    logger.info(f"OK FinMind dividend import: {success_count}/{total} companies")
    if failed:
        logger.error(f"X Failed ({len(failed)}): {failed[:20]}")
    logger.info(f"{'='*60}\n")
