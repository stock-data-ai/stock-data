"""
Finance Tools - Unified Command Line Interface (CLI)

Usage:
  uv run finance_tools/cli.py [COMMAND] [OPTIONS]

Commands:
  update-marketcap-inst - 每日更新：市值估值 + 三大法人（皆為 TWSE/TPEx 批次）。
  financials-update     - 財務報表更新：損益表、月營收。
  update-revenue        - 僅更新月營收。
  import-historical-dividends - 【一次性】從 CSV 匯入 2021–2024 歷史股利。
  update-dividends      - 【定期】從 MOPS 更新 2025+ 股利。
  update-balance-sheet  - 【定期】更新資產負債表 + 現金流量表（ROE/ROA/比率/OCF/FCF）。
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
from finance_tools.orchestration.financials_update import run_financials_update
from finance_tools.orchestration.marketcap_inst_update import run_update_marketcap_inst
from finance_tools.domains.dividends.tasks import run_import_historical_dividends, run_update_mops_dividends
from finance_tools.domains.dividends.finmind_backfill import run_backfill_finmind_dividends
from finance_tools.domains.revenue.tasks import run_update_revenue
from finance_tools.domains.balance_sheet.tasks import run_update_balance_sheet
from finance_tools.orchestration.check_quality import run_check_quality
from finance_tools.domains.shareholder.tasks import run_fetch_shareholder_data
from finance_tools.domains.margin_trading.tasks import run_update_margin_trading
from finance_tools.domains.market_sentiment.tasks import (
    run_update_margin_history,
    run_update_market_sentiment,
)
from finance_tools.scripts.compute_weekly_big_holders import run as run_compute_big_holders
from finance_tools.scripts.generate_chip_topic import run as run_generate_chip_topic
from finance_tools.scripts.generate_disposition_forecast import run as run_generate_disposition_forecast

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

    # --- 'update-marketcap-inst' command ---
    parser_daily = subparsers.add_parser("update-marketcap-inst", help="每日更新：市值估值 + 三大法人（皆為 TWSE/TPEx 批次），單次 load/save。")
    add_common_arguments(parser_daily, include_force=True, include_rerun=True)
    parser_daily.add_argument("--date", type=str, help="指定日期 (YYYYMMDD 或 YYYY-MM-DD)，預設為今天。")
    parser_daily.set_defaults(func=run_update_marketcap_inst)

    # --- 'financials-update' command ---
    parser_full = subparsers.add_parser("financials-update", help="更新損益表、月營收。")
    add_common_arguments(parser_full, include_force=True, include_rerun=True)
    parser_full.set_defaults(func=run_financials_update)

    # --- 'import-historical-dividends' command (one-time) ---
    parser_hist_div = subparsers.add_parser("import-historical-dividends", help="【一次性】從 CSV 匯入 2021–2025 年度合計股利（涵蓋所有 CSV 公司）。")
    parser_hist_div.set_defaults(func=run_import_historical_dividends)

    # --- 'update-dividends' command (ongoing via GitHub Actions) ---
    parser_mops_div = subparsers.add_parser("update-dividends", help="【定期】從 MOPS 抓取 2025+ 股利並 merge 至 JSON。")
    parser_mops_div.set_defaults(func=run_update_mops_dividends)

    # --- 'backfill-dividends' command (one-time) ---
    parser_backfill = subparsers.add_parser("backfill-dividends", help="【一次性】從 FinMind 補齊季配/半年配公司 2021-2025 每期明細，取代 CSV 年度合計。")
    parser_backfill.set_defaults(func=run_backfill_finmind_dividends)

    # --- 'update-balance-sheet' command ---
    parser_bs = subparsers.add_parser("update-balance-sheet", help="【定期】從 FinMind 抓取資產負債表 + 現金流量表，計算 ROE/ROA/流動比率/負債比率/OCF/FCF。")
    add_common_arguments(parser_bs, include_force=True, include_rerun=True)
    parser_bs.set_defaults(func=run_update_balance_sheet)

    # --- 'update-revenue' command ---
    parser_revenue = subparsers.add_parser("update-revenue", help="僅更新月營收資料。")
    add_common_arguments(parser_revenue, include_force=True, include_rerun=True)
    parser_revenue.set_defaults(func=run_update_revenue)
    
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
    parser_margin.add_argument("--date", type=str, help="指定單日 (YYYYMMDD 或 YYYY-MM-DD)。")
    parser_margin.add_argument("--backfill-from", type=str, dest="backfill_from", help="補齊歷史：從此日起到昨天 (YYYYMMDD 或 YYYY-MM-DD)。")
    parser_margin.set_defaults(func=run_update_margin_trading)

    # --- 'update-market-sentiment' command ---
    parser_sentiment = subparsers.add_parser(
        "update-market-sentiment",
        help="更新整體市場情緒：三大法人買賣超 + 融資融券加總。",
    )
    parser_sentiment.add_argument("--date", type=str, help="指定日期 (YYYYMMDD)。")
    parser_sentiment.set_defaults(func=run_update_market_sentiment)

    # --- 'update-margin-history' command ---
    parser_margin_history = subparsers.add_parser(
        "update-margin-history",
        help="重建融資水位長序列 margin_history.json（回補／修復用；平時由 update-market-sentiment 自動帶）。",
    )
    parser_margin_history.set_defaults(func=run_update_margin_history)

    # --- 'compute-big-holders' command ---
    parser_big_holders = subparsers.add_parser(
        "compute-big-holders",
        help="從 TDCC 股權分散表計算大戶加碼排行，輸出 weekly_big_holders.json。",
    )
    parser_big_holders.set_defaults(func=lambda args: run_compute_big_holders())

    # --- 'generate-chip-topic' command ---
    parser_chip = subparsers.add_parser(
        "generate-chip-topic",
        help="聚合三大法人 + 大股東持股訊號，輸出 chip-topic.json 並推送至 stock_map。",
    )
    parser_chip.add_argument("--dry-run", action="store_true", help="產出至 /tmp 但不推送至 stock_map。")
    parser_chip.set_defaults(func=lambda args: run_generate_chip_topic(dry_run=args.dry_run))

    # --- 'generate-disposition-forecast' command ---
    parser_disp = subparsers.add_parser(
        "generate-disposition-forecast",
        help="處置股預警：回測近30日+摺算狀態機，輸出 disposition-forecast.json 並推送至 stock_map。",
    )
    parser_disp.add_argument("--dry-run", action="store_true", help="產出至 /tmp 但不推送至 stock_map。")
    parser_disp.set_defaults(func=lambda args: run_generate_disposition_forecast(dry_run=args.dry_run))

    args = parser.parse_args()
    
    # Execute the function assigned to the command
    if hasattr(args, 'func'):
        args.func(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
