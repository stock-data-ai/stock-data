import logging
import time
import random
from typing import Dict, Any, Callable

import pandas as pd
from datetime import timedelta
from finance_tools.core import DataProcessor, FileManager, FinMindClient
from finance_tools.core.timezone import now_tw
from finance_tools.core.api_client import ApiExhaustedError
import finance_tools.config as config
from finance_tools.orchestration.fetch_orchestrator import FetchOrchestrator
from finance_tools.domains.financials.calculator import FinancialCalculator
from finance_tools.orchestration.data_assembler import DataAssembler
from finance_tools.domains.institutional_investors.calculator import InstRatioCalculator
logger = logging.getLogger(__name__)


def history_is_short(data: Dict[str, Any]) -> bool:
    """檔裡的季報或月營收少於門檻（見 `config.MIN_*_FOR_NORMAL_WINDOW`）。"""
    hist = (data or {}).get("historical") or {}
    quarters = sum(1 for q in hist.get("quarterly") or [] if q.get("revenue") is not None)
    months = len(hist.get("monthlyRevenue") or [])
    return (quarters < config.MIN_QUARTERS_FOR_NORMAL_WINDOW
            or months < config.MIN_MONTHS_FOR_NORMAL_WINDOW)


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
          - None   → FinMind per-stock 呼叫（financials_update 路徑）
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
                # {"ratio": pct, "shares": lots}；shares 可能缺（來源當天沒給），
                # 缺的時候只寫比率，不要用比率回推張數。
                holding = pre_shareholding.get(code) or {}
                inst_date = inst_record.date if inst_record else None
                foreign_holdings = {inst_date: holding} if holding and inst_date else {}
            else:
                foreign_holdings = {}
        else:
            # ── FinMind 路徑（financials_update）──────────────────────
            # 三大法人買賣超張數由 daily-update TWSE T86 批次每日維護，financials-update 不重抓
            shares_df, shares_success = pd.DataFrame(), False
            foreign_holdings = {}

        ratios: dict = {}
        if shares_success:
            processed_shares = self.calculator.calculate_institutional_investors_net_buy(shares_df).to_dict(orient="index")
            for date, share_info in processed_shares.items():
                ratios[date] = share_info
                ratios[date]["code"] = code

        # TWSE 路徑：合併今日外資持股比例與張數（daily-update 用）
        for date, holding in foreign_holdings.items():
            entry = ratios.setdefault(date, {})
            if holding.get("ratio") is not None:
                entry["foreign_ratio"] = holding["ratio"]
            if holding.get("shares") is not None:
                entry["foreign_shares"] = holding["shares"]

        inst_success = bool(foreign_holdings or shares_success)
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
            "skipped": False,
            "full_window": False,
        }

        if not force_update and self.file_mgr.is_updated_today(code):
            logger.info(f"  ✓ Skipping {code} (already updated today)")
            status["skipped"] = True
            return True, status

        logger.debug(f"正在處理 {code} {name}...")
        try:
            # 0. 檔裡的歷史不夠就把視窗放寬，讓這一次直接把歷史長回來。
            #    判準看「檔裡有多少」，不看「檔案在不在／壞不壞」：新公司（日更會先建
            #    只有市值的空殼）、壞檔、上次放寬卻抓失敗而寫出的空殼，全都落在「不夠」，
            #    而且沒補齊之前每一輪都會再試——訊號不會因為寫過一次檔就消失。
            existing_data = self.file_mgr.load_financial_data(code)
            if existing_data is None:
                logger.warning(f"  ⚠️  {code} 財報檔損壞，視同沒有歷史")
                existing_data = {}
            if history_is_short(existing_data):
                logger.info(f"  ↺ {code} 歷史不足，改用完整視窗（{config.FULL_HISTORY_DAYS} 天）")
                start_date = (now_tw() - timedelta(days=config.FULL_HISTORY_DAYS)).strftime("%Y-%m-%d")
                status["full_window"] = True

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
            if existing_data is None:
                # 損壞的檔案由 financials-update 用完整視窗重建；日更不得拿
                # 市值／法人這種片段資料去建一份沒有歷史的新檔。
                logger.warning(f"  ⚠️  {code} 既有歷史讀不回來，日更跳過（等 financials-update 重建）")
                return True, status
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


