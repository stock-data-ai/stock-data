"""
Finance Tools - Unified Command Line Interface (CLI)

Usage:
  uv run finance_tools/cli.py [COMMAND] [OPTIONS]

Commands:
  update-daily          - 每日更新：市值（Yahoo）+ 三大法人（FinMind）。
  full-update           - 全量更新：財務報表、營收、股利。
  update-revenue        - 僅更新月營收。
  import-historical-dividends - 【一次性】從 CSV 匯入 2021–2024 歷史股利。
  update-dividends      - 【定期】從 MOPS 更新 2025+ 股利。
  fetch-shareholder-data- 擷取 TDCC 股東分配資料。
  update-company-info   - 更新上市公司基本資訊。
  update-margin         - 更新融資融券資料。
  check-quality         - 檢查資料品質。
  compute-big-holders   - 計算大戶加碼排行並輸出 weekly_big_holders.json。

Common Options:
  --code CODE           - 處理單一公司（股票代碼）。
  --topic TOPIC         - 處理特定主題的所有公司。
  --limit LIMIT         - 限制處理公司數（測試用）。
  --batch N/M           - 批次處理，例如 2/4。
  --force               - 強制更新（忽略今日已更新檢查）。
  --rerun               - 從失敗佇列重新執行。
"""
from dotenv import load_dotenv
load_dotenv() # take environment variables from .env.

import argparse
import logging
import sys
import os

# Add the project root to sys.path to allow absolute imports of finance_tools
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Configure logging globally
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Import the run functions from the newly created task modules
from finance_tools.orchestration.full_update import run_full_update
from finance_tools.orchestration.daily_update import run_update_daily
from finance_tools.domains.dividends.tasks import run_import_historical_dividends, run_update_mops_dividends
from finance_tools.domains.revenue.tasks import run_update_revenue
from finance_tools.domains.company_info.tasks import run_update_company_info
from finance_tools.domains.company_info.foreign_tasks import run_update_us_company_info
from finance_tools.domains.company_info.foreign_tasks import run_update_jp_company_info
from finance_tools.orchestration.check_quality import run_check_quality
from finance_tools.domains.shareholder.tasks import run_fetch_shareholder_data
from finance_tools.domains.margin_trading.tasks import run_update_margin_trading
from finance_tools.domains.market_sentiment.tasks import run_update_market_sentiment
from finance_tools.scripts.compute_weekly_big_holders import run as run_compute_big_holders

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

    # --- 'update-daily' command ---
    parser_daily = subparsers.add_parser("update-daily", help="每日更新：市值（Yahoo）+ 三大法人（FinMind），單次 load/save。")
    add_common_arguments(parser_daily, include_force=True, include_rerun=True)
    parser_daily.set_defaults(func=run_update_daily)

    # --- 'full-update' command ---
    parser_full = subparsers.add_parser("full-update", help="更新財務報表、營收和股利。")
    add_common_arguments(parser_full, include_force=True, include_rerun=True)
    parser_full.set_defaults(func=run_full_update)

    # --- 'import-historical-dividends' command (one-time) ---
    parser_hist_div = subparsers.add_parser("import-historical-dividends", help="【一次性】從 CSV 匯入 2021–2025 年度合計股利（涵蓋所有 CSV 公司）。")
    parser_hist_div.set_defaults(func=run_import_historical_dividends)

    # --- 'update-dividends' command (ongoing via GitHub Actions) ---
    parser_mops_div = subparsers.add_parser("update-dividends", help="【定期】從 MOPS 抓取 2025+ 股利並 merge 至 JSON。")
    parser_mops_div.set_defaults(func=run_update_mops_dividends)

    # --- 'update-revenue' command ---
    parser_revenue = subparsers.add_parser("update-revenue", help="僅更新月營收資料。")
    add_common_arguments(parser_revenue, include_force=True, include_rerun=True)
    parser_revenue.set_defaults(func=run_update_revenue)
    
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

    # --- 'update-margin' command ---
    parser_margin = subparsers.add_parser("update-margin", help="更新融資融券資料。")
    add_common_arguments(parser_margin, include_force=True)
    parser_margin.add_argument("--date", type=str, help="指定日期 (YYYYMMDD)。")
    parser_margin.set_defaults(func=run_update_margin_trading)

    # --- 'update-market-sentiment' command ---
    parser_sentiment = subparsers.add_parser(
        "update-market-sentiment",
        help="更新整體市場情緒：三大法人買賣超 + 融資融券加總。",
    )
    parser_sentiment.add_argument("--date", type=str, help="指定日期 (YYYYMMDD)。")
    parser_sentiment.set_defaults(func=run_update_market_sentiment)

    # --- 'compute-big-holders' command ---
    parser_big_holders = subparsers.add_parser(
        "compute-big-holders",
        help="從 TDCC 股權分散表計算大戶加碼排行，輸出 weekly_big_holders.json。",
    )
    parser_big_holders.set_defaults(func=lambda args: run_compute_big_holders())

    args = parser.parse_args()
    
    # Execute the function assigned to the command
    if hasattr(args, 'func'):
        args.func(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
