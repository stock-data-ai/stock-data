"""
Financials Fetcher - 財務報表數據抓取器
"""

import logging
from typing import Tuple, List, Dict
import pandas as pd
from finance_tools.core import FinMindClient, DataProcessor

logger = logging.getLogger(__name__)


class FinancialsFetcher:
    """財務報表抓取器"""

    def __init__(self, client: FinMindClient, processor: DataProcessor):
        self.client = client
        self.processor = processor

    def fetch_and_process(
        self, stock_id: str, start_date: str
    ) -> Tuple[List[Dict], List[Dict], bool]:
        """
        抓取並處理財務報表數據（損益表 + 資產負債表 + 現金流量表）

        Args:
            stock_id: 股票代碼
            start_date: 起始日期

        Returns:
            (annual_data, quarterly_data, success)
            annual_data 每筆包含 roe, roa, currentRatio, debtRatio, ocf, fcf
        """
        logger.debug(f"  Fetching financial statements for {stock_id}...")

        # 1. 損益表（必要）
        fs_df, success = self.client.fetch_financial_statements(stock_id, start_date)
        if not success or fs_df.empty:
            logger.warning(f"  Failed to fetch financial statements for {stock_id}")
            return [], [], False

        annual_data, quarterly_data = self.processor.process_financials(stock_id, fs_df)

        # 2. 資產負債表（選擇性，失敗不影響主流程）
        bs_by_year: Dict[int, Dict] = {}
        bs_df, bs_ok = self.client.fetch_balance_sheet(stock_id, start_date)
        if bs_ok and not bs_df.empty:
            bs_by_year = self.processor.process_balance_sheet(stock_id, bs_df)
        else:
            logger.warning(f"  Balance sheet unavailable for {stock_id}, skipping ratios")

        # 3. 現金流量表（選擇性）
        cf_by_year: Dict[int, Dict] = {}
        cf_df, cf_ok = self.client.fetch_cash_flows_statement(stock_id, start_date)
        if cf_ok and not cf_df.empty:
            cf_by_year = self.processor.process_cash_flows(stock_id, cf_df)
        else:
            logger.warning(f"  Cash flows unavailable for {stock_id}, skipping OCF/FCF")

        # 4. 將 balance sheet + cash flow 指標合併進 annual_data
        for item in annual_data:
            year = item["year"]
            net_income = item.get("netIncome", 0) or 0
            bs = bs_by_year.get(year, {})
            cf = cf_by_year.get(year, {})

            total_assets    = bs.get("totalAssets")
            total_liab      = bs.get("totalLiabilities")
            equity          = bs.get("equity")
            current_assets  = bs.get("currentAssets")
            current_liab    = bs.get("currentLiabilities")
            ocf             = cf.get("ocf")
            capex           = cf.get("capex")

            item["roe"]          = round(net_income / equity * 100, 2)          if equity          and equity > 0          else None
            item["roa"]          = round(net_income / total_assets * 100, 2)    if total_assets    and total_assets > 0    else None
            item["currentRatio"] = round(current_assets / current_liab * 100, 2) if current_assets and current_liab and current_liab > 0 else None
            item["debtRatio"]    = round(total_liab / total_assets * 100, 2)    if total_liab      and total_assets > 0    else None
            item["ocf"]          = round(ocf, 0)                                if ocf   is not None else None
            item["fcf"]          = round(ocf + capex, 0)                        if ocf   is not None and capex is not None else None

        return annual_data, quarterly_data, True
