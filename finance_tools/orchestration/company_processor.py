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
                 all_companies_details: Dict[str, Any], all_dividends_data: Dict[str, Any],
                 institutional_investors_shares_fetcher: Callable,
                 shareholding_fetcher: Callable = None,
                 inst_ratio_calculator: InstRatioCalculator = None):
        self.file_mgr = file_mgr
        self.processor = processor
        self.all_companies_details = all_companies_details
        self.all_dividends_data = all_dividends_data

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

    def _build_ratios(self, code: str, start_date: str) -> tuple[dict, bool]:
        """
        擷取並整合三大法人持股比例與買賣超資料，回傳 (ratios_dict, inst_success)。
        外資比例來自 TaiwanStockShareholding，投信/自營商從累積推估。
        """
        shareholding_df, _ = self.fetch_orchestrator.fetch_shareholding(code, start_date)
        shares_df, shares_success = self.fetch_orchestrator.fetch_institutional_investors_shares(code, start_date)

        foreign_ratios = self.inst_ratio_calculator.calculate_foreign_ratio(shareholding_df)
        trust_dealer_ratios = self.inst_ratio_calculator.calculate_trust_dealer_ratio(code, shares_df) if shares_success else {}

        ratios: dict = {}
        if shares_success:
            processed_shares = self.calculator.calculate_institutional_investors_net_buy(shares_df).to_dict(orient="index")
            for date, share_info in processed_shares.items():
                ratios[date] = share_info
                ratios[date]["code"] = code

        # 合併外資比例
        for date, foreign_ratio in foreign_ratios.items():
            ratios.setdefault(date, {})["foreign_ratio"] = foreign_ratio

        # 合併投信/自營商比例
        for date, td in trust_dealer_ratios.items():
            ratios.setdefault(date, {}).update(td)

        # 計算 three_inst_ratio（foreign_ratio 有 1 天延遲，前向填充最近已知值）
        last_foreign: float = 0.0
        for date in sorted(ratios.keys()):
            entry = ratios[date]
            fr = entry.get("foreign_ratio")
            if fr is not None:
                last_foreign = fr
            else:
                entry["foreign_ratio"] = last_foreign
                fr = last_foreign
            tr = entry.get("trust_ratio") or 0.0
            dr = entry.get("dealer_ratio") or 0.0
            if fr or tr or dr:
                entry["three_inst_ratio"] = round(fr + tr + dr, 6)

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
            company_dividends_csv_data = self.all_dividends_data.get(code)
            if company_dividends_csv_data:
                status["div"] = True
                if company_dividends_csv_data.get('frequency'):
                    latest_block["dividendFrequency"] = company_dividends_csv_data.get('frequency')

            new_dividend_list = self.assembler.build_dividend_list(company_dividends_csv_data)

            data_quality = "high" if (fin_success and revenue_success) else "medium"
            if not fin_success:
                data_quality = "low"
            status["quality"] = data_quality

            existing_data = self.file_mgr.load_financial_data(code)
            final_data = self.assembler.build_final_data(
                existing_data, code, name, latest_block,
                annual_data, quarterly_data, monthly_revenue_df,
                new_dividend_list, data_quality, ratios
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

    def process_institutional_investors_only(self, code: str, name: str, start_date: str, force_update: bool = False) -> tuple[bool, dict]:
        """
        Only updates institutional investors data (ratios + buy/sell shares).
        Returns (success, status).
        """
        status = {"inst": False, "skipped": False}
        if not force_update and self.file_mgr.is_updated_today(code):
            logger.info(f"  ✓ Skipping {code} (already updated today)")
            status["skipped"] = True
            return True, status

        logger.debug(f"正在處理 {code} {name} 的三大法人資料...")
        try:
            # 1. 擷取並整合三大法人資料
            ratios, inst_success = self._build_ratios(code, start_date)
            status["inst"] = inst_success

            # 3. 讀取現有資料，只更新三大法人欄位
            existing_data = self.file_mgr.load_financial_data(code)
            final_data = self.assembler.merge_institutional_investors(existing_data, ratios)

            # 4. 儲存
            if self._save_cleaned(code, final_data):
                logger.debug(f"  ✔️  三大法人資料處理完畢 {code} {name}。")
                return True, status
            else:
                logger.error(f"  ❌ 儲存 {code} 的三大法人資料失敗。")
                return False, status

        except ApiExhaustedError:
            raise
        except Exception:
            logger.exception(f"  ❌ 處理 {code} 三大法人資料時發生未預期錯誤：")
            return False, status

    def process_daily_only(self, code: str, name: str, start_date: str, force_update: bool = False, margin_trading_data: Dict[str, Any] = None) -> tuple[bool, dict]:
        """
        每日更新：市值（Yahoo）+ 三大法人（FinMind）+ 融資融券（傳入資料）合併為單次 load/save。
        成功條件：所有必要 API 都拿到資料 AND 存檔成功。
        任一缺失 → 仍嘗試儲存已取得的部分，但回傳 False（進 rerun queue）。
        """
        status = {"marketcap": False, "inst": False, "margin": bool(margin_trading_data), "skipped": False}

        if not force_update and self.file_mgr.is_updated_today(code):
            logger.info(f"  ✓ Skipping {code} (already updated today)")
            status["skipped"] = True
            return True, status

        try:
            # 1. Yahoo Finance
            valuation_stats = self.fetch_orchestrator.fetch_valuation_stats(code)
            status["marketcap"] = bool(valuation_stats.get("marketCap"))
            if not status["marketcap"]:
                logger.warning(f"  ⚠️ {code}: 無法從 Yahoo 取得市值。")

            time.sleep(random.uniform(*config.DEFAULT_SLEEP_RANGE))

            # 2. FinMind 三大法人
            ratios, inst_success = self._build_ratios(code, start_date)
            status["inst"] = inst_success
            if not inst_success:
                logger.warning(f"  ⚠️ {code}: 無法從 FinMind 取得三大法人資料。")

            # 3. 三者都失敗 → 不寫檔
            if not status["marketcap"] and not status["inst"] and not status["margin"]:
                logger.warning(f"  ❌ {code} {name}: 每日更新相關資料均無法取得，略過儲存。")
                return False, status

            # 4. 讀一次 → 合併有取得的部分 → 存一次
            existing_data = self.file_mgr.load_financial_data(code)
            if status["marketcap"]:
                existing_data = self.assembler.merge_valuation(existing_data, valuation_stats)
            if status["inst"]:
                existing_data = self.assembler.merge_institutional_investors(existing_data, ratios)
            if status["margin"]:
                existing_data = self.assembler.merge_margin_trading(existing_data, margin_trading_data)

            if not self._save_cleaned(code, existing_data):
                logger.error(f"  ❌ 儲存 {code} 的每日更新資料失敗。")
                return False, status

            # 5. 市值+法人都成功才算完成；margin 為附加資料，不影響 rerun 判斷
            #    （非融資券股票不在 margin_lookup → margin_data=None → 不算失敗）
            overall_success = status["marketcap"] and status["inst"]
            if overall_success:
                logger.debug(f"  ✔️ {code} {name}: 每日更新完畢。")
            else:
                missing = [k for k in ("marketcap", "inst") if not status[k]]
                logger.warning(f"  ⚠️ {code} {name}: 部分缺失 {missing}，已儲存現有資料，列入重試。")
            return overall_success, status

        except ApiExhaustedError:
            raise
        except Exception:
            logger.exception(f"  ❌ 處理 {code} {name} 每日更新時發生未預期錯誤：")
            return False, status

    def process_marketcap_only(self, code: str, name: str, force_update: bool = False) -> tuple[bool, dict]:
        """
        Only updates market cap and valuation data (PE, PB, Yield) using Yahoo Finance.
        Returns (success, status).
        """
        status = {"marketcap": False, "skipped": False}
        if not force_update and self.file_mgr.is_updated_today(code):
            logger.info(f"  ✓ Skipping {code} (already updated today)")
            status["skipped"] = True
            return True, status

        logger.debug(f"正在處理 {code} {name} 的市值與估值資料...")
        try:
            # 直接從 Yahoo 取得完整估值統計數據
            valuation_stats = self.fetch_orchestrator.fetch_valuation_stats(code)

            if valuation_stats and valuation_stats.get("marketCap"):
                status["marketcap"] = True
            else:
                logger.warning(f"  ⚠️ 無法從 Yahoo 取得 {code} 的完整估值數據。")
                return False, status

            # 3. 讀取現有資料，更新市值與估值指標
            existing_data = self.file_mgr.load_financial_data(code)
            final_data = self.assembler.merge_valuation(existing_data, valuation_stats)

            # 4. 儲存
            if self._save_cleaned(code, final_data):
                logger.debug(f"  ✔️ 從 Yahoo 取得市值與估值並處理完畢 {code} {name}。")
                return True, status
            else:
                logger.error(f"  ❌ 儲存 {code} 的市值估值資料失敗。")
                return False, status

        except ApiExhaustedError:
            raise
        except Exception:
            logger.exception(f"  ❌ 處理 {code} 市值資料時發生未預期錯誤：")
            return False, status
