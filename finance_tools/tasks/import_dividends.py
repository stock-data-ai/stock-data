import logging
from core.file_manager import FileManager
from core.timezone import today_str
from fetchers.dividends_from_csv import DividendsFromCSVFetcher

logger = logging.getLogger(__name__)

def run_import_dividends(args):
    """
    處理從 CSV 匯入股利數據的任務
    - 從 CSV 檔案中讀取所有公司的股利資料。
    - 遍歷每家公司，將其股利資料更新或添加到對應的 financial JSON 檔案中。
    """
    logger.info("Handling dividend import from CSVs...")

    file_mgr = FileManager()
    all_companies_map = {c["code"]: c["name"] for c in file_mgr.load_companies()}
    csv_fetcher = DividendsFromCSVFetcher()

    all_dividends_data = csv_fetcher.fetch_all()

    if not all_dividends_data:
        logger.warning("No dividend data found in CSV files. Exiting.")
        return

    logger.info(f"Found dividend data for {len(all_dividends_data)} companies in CSVs.")

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
        logger.info(f"  Processing dividends for {code}...")

        financial_data = file_mgr.load_financial_data(code)
        if not financial_data:
            name = all_companies_map.get(code, code)
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

        financial_data['latest']['dividendFrequency'] = dividend_data.get('frequency')

        new_dividend_list = []
        for year, year_data in sorted(dividend_data.get('years', {}).items(), reverse=True):
            total = year_data['cash'] + year_data['stock']
            new_dividend_list.append({
                "year": int(year),
                "cashDividend": round(year_data['cash'], 4),
                "stockDividend": round(year_data['stock'], 4),
                "totalDividend": round(total, 4),
            })

        financial_data['historical']['dividends'] = new_dividend_list
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