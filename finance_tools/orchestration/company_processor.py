import logging
import time
import random
from typing import Dict, Any, Callable

import pandas as pd
from finance_tools.core import DataProcessor, FileManager, FinMindClient
from finance_tools.core.api_client import ApiExhaustedError
import finance_tools.config as config
from finance_tools.orchestration.fetch_orchestrator import FetchOrchestrator
from finance_tools.domains.financials.calculator import FinancialCalculator
from finance_tools.orchestration.data_assembler import DataAssembler
from finance_tools.domains.institutional_investors.calculator import InstRatioCalculator
logger = logging.getLogger(__name__)

class CompanyProcessor:
    """
    Encapsulates the logic for processing all financial data for a single company
    by orchestrating fetching, calculation, and data assembly.
    """
    def __init__(self, processor: DataProcessor, file_mgr: FileManager, finmind_client: FinMindClient,
                 financials_fetcher: Callable, revenue_fetcher: Callable,
                 all_companies_details: Dict[str, Any],
                 institutional_investors_shares_fetcher: Callable,
                 shareholding_fetcher: Callable = None,
                 inst_ratio_calculator: InstRatioCalculator = None):
        self.file_mgr = file_mgr
        self.processor = processor
        self.all_companies_details = all_companies_details

        self.fetch_orchestrator = FetchOrchestrator(
            finmind_client, financials_fetcher, revenue_fetcher,
            institutional_investors_shares_fetcher,
            shareholding_fetcher=shareholding_fetcher,
        )
        self.calculator = FinancialCalculator()
        self.assembler = DataAssembler()
        self.inst_ratio_calculator = inst_ratio_calculator

    def _save_cleaned(self, code: str, data: dict) -> bool:
        """clean_nan + save — shared by all process_*() methods."""
        cleaned = self.processor.clean_nan(data)
        return self.file_mgr.save_financial_data(code, cleaned)

    def _make_finmind_df(self, code: str, record) -> pd.DataFrame:
        """TWSE InstitutionalRecord → FinMind 相容 DataFrame（date, stock_id, name, buy, sell）"""
        rows = [
            {"date": record.date, "stock_id": code, "name": "Foreign_Investor",  "buy": record.Foreign_Investor_buy,  "sell": record.Foreign_Investor_sell},
            {"date": record.date, "stock_id": code, "name": "Foreign_Dealer",    "buy": record.Foreign_Dealer_buy,    "sell": record.Foreign_Dealer_sell},
            {"date": record.date, "stock_id": code, "name": "Investment_Trust",  "buy": record.Investment_Trust_buy,  "sell": record.Investment_Trust_sell},
            {"date": record.date, "stock_id": code, "name": "Dealer_self",       "buy": record.Dealer_self_buy,       "sell": record.Dealer_self_sell},
            {"date": record.date, "stock_id": code, "name": "Dealer_hedging",    "buy": record.Dealer_hedging_buy,    "sell": record.Dealer_hedging_sell},
        ]
        return pd.DataFrame(rows)

    def _build_ratios(self, code: str, start_date: str,
                      pre_inst=None, pre_shareholding=None) -> tuple[dict, bool]:
        """
        擷取並整合三大法人持股比例與買賣超資料，回傳 (ratios_dict, inst_success)。

        pre_inst / pre_shareholding（來自 TWSEInstitutionalFetcher / TWSEShareholdingFetcher）:
          - 有傳入 → 使用 TWSE/TPEx 預先批次撈取的資料
          - None   → FinMind per-stock 呼叫（full_update 路徑）
        """
        if pre_inst is not None:
            # ── TWSE/TPEx 路徑 ─────────────────────────────────────
            inst_record = pre_inst.get(code)
            if inst_record:
                shares_df = self._make_finmind_df(code, inst_record)
                shares_success = True
            else:
                shares_df = pd.DataFrame()
                shares_success = False

            if pre_shareholding is not None:
                ratio = pre_shareholding.get(code)
                inst_date = inst_record.date if inst_record else None
                foreign_ratios = {inst_date: ratio} if ratio is not None and inst_date else {}
            else:
                foreign_ratios = {}
        else:
            # ── FinMind 路徑（full_update）─────────────────────────
            shares_df, shares_success = self.fetch_orchestrator.fetch_institutional_investors_shares(code, start_date)
            # foreign_ratio 由 daily-update TWSE 批次維護，full-update 不寫入，避免覆蓋正確值

        ratios: dict = {}
        if shares_success:
            processed_shares = self.calculator.calculate_institutional_investors_net_buy(shares_df).to_dict(orient="index")
            for date, share_info in processed_shares.items():
                ratios[date] = share_info
                ratios[date]["code"] = code

        # TWSE 路徑：合併今日外資比例（daily-update 用）
        for date, foreign_ratio in foreign_ratios.items():
            ratios.setdefault(date, {})["foreign_ratio"] = foreign_ratio

        inst_success = bool(foreign_ratios or shares_success)
        return ratios, inst_success

    def process_company(self, code: str, name: str, start_date: str, force_update: bool = False) -> tuple[bool, dict]:
        """
        Processes a single company by orchestrating fetching, calculation, and assembly.
        Returns (save_success, status_details).
        """
        status = {
            "fin": False,
            "rev": False,
            "inst": False,
            "div": False,
            "quality": "low",
            "skipped": False
        }

        if not force_update and self.file_mgr.is_updated_today(code):
            logger.info(f"  ✓ Skipping {code} (already updated today)")
            status["skipped"] = True
            return True, status

        logger.debug(f"正在處理 {code} {name}...")
        try:
            # 1. 擷取所有需要的資料
            annual_data, quarterly_data, fin_success = self.fetch_orchestrator.fetch_financials(code, start_date)
            status["fin"] = fin_success
            time.sleep(random.uniform(*config.DEFAULT_SLEEP_RANGE))
            
            monthly_revenue_df, revenue_success = self.fetch_orchestrator.fetch_revenue(code, start_date)
            status["rev"] = revenue_success
            time.sleep(random.uniform(*config.DEFAULT_SLEEP_RANGE))
            
            ratios, inst_success = self._build_ratios(code, start_date)
            status["inst"] = inst_success
            time.sleep(random.uniform(*config.DEFAULT_SLEEP_RANGE))

            latest_block = {}
            latest_quarter = quarterly_data[0] if quarterly_data else {}
            if latest_quarter:
                self.calculator.calculate_yoy_and_update_block(latest_block, latest_quarter, quarterly_data)

            # 3. 組合資料
            data_quality = "high" if (fin_success and revenue_success) else "medium"
            if not fin_success:
                data_quality = "low"
            status["quality"] = data_quality

            existing_data = self.file_mgr.load_financial_data(code)
            final_data = self.assembler.build_final_data(
                existing_data, code, name, latest_block,
                annual_data, quarterly_data, monthly_revenue_df,
                [], data_quality, ratios
            )

            # 4. 完成並儲存
            if self._save_cleaned(code, final_data):
                logger.debug(f"  ✔️  處理完畢 {code} {name}。品質: {data_quality}")
                return True, status
            else:
                logger.error(f"  ❌ 儲存 {code} 的資料失敗。")
                return False, status

        except ApiExhaustedError:
            raise
        except Exception:
            logger.exception(f"  ❌ 處理 {code} 時發生未預期錯誤：")
            return False, status

    def _build_valuation(self, code: str, pre_valuation) -> dict:
        """TWSE/TPEx 批次估值資料 → market cap / pe / pb / dividend yield（相容 merge_valuation 介面）。"""
        if not pre_valuation:
            return {}
        record = pre_valuation.get(code)
        if not record:
            return {}

        company = self.all_companies_details.get(code, {})
        issued_shares = company.get("gov", {}).get("capital", {}).get("issuedCommonShares")
        market_cap = record.close * issued_shares if issued_shares else None

        return {
            "marketCap": market_cap,
            "trailingPE": record.pe,
            "priceToBook": record.pb,
            # TWSE/TPEx 殖利率為百分比（e.g. 1.84），merge_valuation 預期 decimal（e.g. 0.0184）
            "dividendYield": record.dividend_yield / 100 if record.dividend_yield is not None else None,
        }

    def process_daily_only(self, code: str, name: str, start_date: str, force_update: bool = False,
                           pre_inst=None, pre_shareholding=None, pre_valuation=None) -> tuple[bool, dict]:
        """
        每日更新：市值（TWSE/TPEx批次lookup）+ 三大法人（TWSE/TPEx批次lookup）。
        找不到 = 今日無資料，不算失敗，不進 rerun queue。
        """
        status = {"marketcap": False, "inst": False, "skipped": False}

        if not force_update and self.file_mgr.is_updated_today(code):
            logger.info(f"  ✓ Skipping {code} (already updated today)")
            status["skipped"] = True
            return True, status

        try:
            # 1. 市值/估值批次 lookup（找不到 = 今日無資料，非失敗）
            valuation_stats = self._build_valuation(code, pre_valuation)
            status["marketcap"] = bool(valuation_stats.get("marketCap"))

            # 2. 三大法人批次 lookup（找不到 = 今日無法人交易，非失敗）
            ratios, _ = self._build_ratios(code, start_date, pre_inst, pre_shareholding)
            status["inst"] = bool(ratios)

            # 今日無任何資料可更新 → 略過儲存（正常情況，非失敗）
            if not status["marketcap"] and not status["inst"]:
                logger.debug(f"  ⏭ {code} {name}: 今日無市值或法人資料，略過。")
                return True, status

            existing_data = self.file_mgr.load_financial_data(code)
            if status["marketcap"]:
                existing_data = self.assembler.merge_valuation(existing_data, valuation_stats)
            if status["inst"]:
                existing_data = self.assembler.merge_institutional_investors(existing_data, ratios)
            self._save_cleaned(code, existing_data)
            return True, status

        except ApiExhaustedError:
            raise
        except Exception:
            logger.exception(f"  ❌ 處理 {code} {name} 每日更新時發生未預期錯誤：")
            return False, status


