"""
Finance Tools - Refactored Unified Command Line Interface (CLI)

This script acts as the main entry point for all financial data processing tasks.
It parses command-line arguments and dispatches them to the appropriate task modules
located in the `finance_tools.tasks` package.

Usage:
  uv run finance_tools/cli.py [COMMAND] [OPTIONS]

Commands:
  full-update           - Update financials, revenue, and dividends.
  import-dividends      - Import dividend information from local CSV files.
  update-revenue        - Update monthly revenue data only.
  update-valuation      - Update PE/PB valuation metrics for existing files.
  update-marketcap      - Update market capitalization using local price data.
  update-company-info   - Update basic information for all listed companies.
  check-quality         - Check data quality and generate a failure queue.
  fetch-shareholder-data- Fetch and store TDCC shareholder distribution data.

Common Options:
  --code CODE           - Process a single company by its stock code (e.g., 2330).
  --topic TOPIC         - Process all companies within a specific topic.
  --limit LIMIT         - Limit the number of companies to process (for testing).
  --force               - Force update even if the data file already exists.
  --rerun               - Rerun failed companies from the queue.
"""
from dotenv import load_dotenv
load_dotenv() # take environment variables from .env.

import argparse
import logging

# Configure logging globally
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Import the run functions from the newly created task modules
from tasks.orchestrators.full_update import run_full_update
from tasks.fundamentals.import_dividends import run_import_dividends
from tasks.periodic.revenue import run_update_revenue
from tasks.periodic.valuation import run_update_valuation
from tasks.daily.marketcap import run_update_marketcap
from tasks.fundamentals.company_info import run_update_company_info
from foreign_companies.tasks import run_update_us_company_info
from foreign_companies.tasks import run_update_jp_company_info
from tasks.maintenance.check_quality import run_check_quality
from tasks.periodic.shareholder_data import run_fetch_shareholder_data
from tasks.daily.institutional_investors import run_update_institutional_investors

def add_common_arguments(parser, include_force=False, include_rerun=False):
    """Adds common filtering arguments to a subparser."""
    parser.add_argument("--code", type=str, help="依股票代碼處理單一公司。")
    parser.add_argument("--topic", type=str, help="處理特定主題中的所有公司。")
    parser.add_argument("--limit", type=int, help="限制要處理的公司數量。")
    parser.add_argument("--batch", type=str, help="批次 N/M：處理 M 個總批次中的第 N 批 (例如 2/4)。")
    if include_force:
        parser.add_argument("--force", action="store_true", help="強制更新現有檔案。")
    if include_rerun:
        parser.add_argument("--rerun", action="store_true", help="從佇列中重新執行失敗的公司。")

def main():
    """Main function: Parses arguments and dispatches to the correct task."""
    parser = argparse.ArgumentParser(
        description="財經工具 CLI",
        formatter_class=argparse.RawTextHelpFormatter
    )
    
    subparsers = parser.add_subparsers(dest="command", required=True, help="可用的指令")

    # --- 'full-update' command ---
    parser_full = subparsers.add_parser("full-update", help="更新財務報表、營收和股利。")
    add_common_arguments(parser_full, include_force=True, include_rerun=True)
    parser_full.set_defaults(func=run_full_update)

    # --- 'import-dividends' command ---
    parser_import_div = subparsers.add_parser("import-dividends", help="從 MOPS 抓取最新股利公告並寫入 JSON。")
    add_common_arguments(parser_import_div)
    parser_import_div.set_defaults(func=run_import_dividends)

    # --- 'update-revenue' command ---
    parser_revenue = subparsers.add_parser("update-revenue", help="僅更新月營收資料。")
    add_common_arguments(parser_revenue, include_force=True, include_rerun=True)
    parser_revenue.set_defaults(func=run_update_revenue)
    
    # --- 'update-valuation' command ---
    parser_valuation = subparsers.add_parser("update-valuation", help="為現有檔案更新本益比/股價淨值比估值指標。")
    add_common_arguments(parser_valuation, include_force=True, include_rerun=True)
    parser_valuation.set_defaults(func=run_update_valuation)

    # --- 'update-marketcap' command ---
    parser_marketcap = subparsers.add_parser("update-marketcap", help="透過 API 取得股價更新市值。")
    add_common_arguments(parser_marketcap, include_force=True, include_rerun=True)
    parser_marketcap.set_defaults(func=run_update_marketcap)

    # --- 'update-company-info' command ---
    parser_info = subparsers.add_parser("update-company-info", help="更新所有上市公司的基本資訊。")
    parser_info.set_defaults(func=run_update_company_info)

    # --- 'update-us-company-info' command ---
    parser_us_info = subparsers.add_parser("update-us-company-info", help="從 Yahoo Finance 更新美國公司的基本資訊。")
    parser_us_info.add_argument("--code", type=str, help="依股票代碼擷取單一美國公司 (例如 NVDA)。")
    parser_us_info.set_defaults(func=run_update_us_company_info)

    # --- 'update-jp-company-info' command ---
    parser_jp_info = subparsers.add_parser("update-jp-company-info", help="從 Yahoo Finance 更新日本公司的基本資訊。")
    parser_jp_info.add_argument("--code", type=str, help="依股票代碼擷取單一日本公司 (例如 5201.JP)。")
    parser_jp_info.set_defaults(func=run_update_jp_company_info)

    # --- 'check-quality' command ---
    parser_quality = subparsers.add_parser("check-quality", help="檢查資料品質並產生失敗佇列。")
    parser_quality.set_defaults(func=run_check_quality)

    # --- 'fetch-shareholder-data' command ---
    parser_shareholder = subparsers.add_parser("fetch-shareholder-data", help="擷取 TDCC 股東分配資料。")
    add_common_arguments(parser_shareholder, include_force=True, include_rerun=True)
    parser_shareholder.set_defaults(func=run_fetch_shareholder_data)

    # --- 'update-institutional-investors' command ---
    parser_inst_inv = subparsers.add_parser(
        "update-institutional-investors", help="更新三大法人每日買賣超資料。"
    )
    add_common_arguments(parser_inst_inv, include_force=True, include_rerun=True)
    parser_inst_inv.set_defaults(func=run_update_institutional_investors)

    args = parser.parse_args()
    
    # Execute the function assigned to the command
    if hasattr(args, 'func'):
        args.func(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
