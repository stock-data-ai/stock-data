import logging
from finance_tools.core.file_manager import FileManager
from finance_tools.core.timezone import today_str
from finance_tools.domains.dividends.fetcher import MOPSDividendFetcher

logger = logging.getLogger(__name__)

def run_import_dividends(args):
    """
    處理匯入股利數據的任務（純 MOPS API，不再依賴本地 CSV）
    """
    logger.info("Handling dividend import (MOPS only)...")

    file_mgr = FileManager()
    all_companies_map = {c["code"]: c["name"] for c in file_mgr.load_companies()}

    mops_fetcher = MOPSDividendFetcher()
    all_dividends_data = mops_fetcher.fetch_all()
    logger.info(f"Fetched MOPS dividend data for {len(all_dividends_data)} companies.")

    if not all_dividends_data:
        logger.warning("No dividend data found. Exiting.")
        return

    companies_to_process = list(all_dividends_data.items())
    if args.code:
        if args.code in all_dividends_data:
            companies_to_process = [(args.code, all_dividends_data[args.code])]
            logger.info(f"--- SINGLE COMPANY MODE: {args.code} ---")
        else:
            logger.error(f"Company {args.code} not found in CSV dividend data. Exiting.")
            return

    if getattr(args, "limit", None) and args.limit > 0:
        companies_to_process = companies_to_process[:args.limit]
        logger.info(f"--- LIMITING to first {args.limit} companies for testing ---")

    success_count = 0
    failed_companies = []

    for code, dividend_data in companies_to_process:
        if not code.isdigit():
            logger.debug(f"  Skipping {code} (special/preferred share)")
            continue

        name = all_companies_map.get(code, code)

        logger.info(f"  Processing dividends for {code}...")

        financial_data = file_mgr.load_financial_data(code)
        if not financial_data:
            financial_data = {
                "companyCode": code,
                "companyName": name,
                "latest": {},
                "historical": {"annual": [], "quarterly": [], "monthlyRevenue": [], "dividends": []},
            }

        if 'historical' not in financial_data:
            financial_data['historical'] = {}
        if 'latest' not in financial_data:
            financial_data['latest'] = {}

        new_records = dividend_data.get("records", [])
        new_keys = {(r["year"], r.get("sequence", 1)) for r in new_records}
        old_records = [
            r for r in (financial_data['historical'].get('dividends') or [])
            if (r["year"], r.get("sequence", 1)) not in new_keys
        ]
        merged = sorted(
            new_records + old_records,
            key=lambda r: (r["year"], r.get("sequence", 1)),
            reverse=True
        )

        financial_data['historical']['dividends'] = merged
        financial_data['lastUpdated'] = today_str()

        if file_mgr.save_financial_data(code, financial_data):
            logger.info(f"    OK Saved dividend data for {code}.")
            success_count += 1
        else:
            logger.error(f"    X Failed to save dividend data for {code}.")
            failed_companies.append(code)

    logger.info(f"\n{'='*60}")
    logger.info(f"OK Dividend import completed for: {success_count}/{len(companies_to_process)} companies")
    if failed_companies:
        logger.error(f"X Failed to save for: {len(failed_companies)} companies")
    logger.info(f"{ '='*60}\n")