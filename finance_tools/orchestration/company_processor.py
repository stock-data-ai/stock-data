import logging
from typing import Dict, Any

import pandas as pd
from finance_tools.core import DataProcessor, FileManager
from finance_tools.core.api_client import ApiExhaustedError
from finance_tools.domains.financials.calculator import FinancialCalculator
from finance_tools.orchestration.data_assembler import DataAssembler
from finance_tools.domains.institutional_investors.calculator import InstRatioCalculator
logger = logging.getLogger(__name__)


class CompanyProcessor:
    """
    逐家組裝 company-financials/{code}.json。

    不自己打 API：財報（`process_company`）收的是全市場抓取後分到這家的資料
    （見 `core/market_fetch.py`），日更（`process_daily_only`）收的是 TWSE／TPEx 批次資料。
    """
    def __init__(self, processor: DataProcessor, file_mgr: FileManager,
                 all_companies_details: Dict[str, Any],
                 inst_ratio_calculator: InstRatioCalculator = None):
        self.file_mgr = file_mgr
        self.processor = processor
        self.all_companies_details = all_companies_details

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

    def process_company(self, code: str, name: str,
                        statements_df: pd.DataFrame, revenue_df: pd.DataFrame) -> tuple[bool, dict]:
        """
        把這家公司的損益表與月營收（全市場抓取後分到這家的列）處理後併進財報檔。

        每次都是完整視窗的資料（見 `config.INCOME_STATEMENT_YEARS`），所以新公司、壞檔、
        新加的欄位都在這一輪補齊；來源沒有這家的資料時傳空 DataFrame，合併時保留既有歷史。
        Returns (save_success, status_details).
        """
        status = {
            "fin": False,
            "rev": False,
            "quality": "low",
        }

        logger.debug(f"正在處理 {code} {name}...")
        try:
            # 壞檔：三態讀取回 None。這裡拿到的是完整視窗，所以直接當成沒有歷史重建
            #（壞檔本身留在原處的規則見 FileManager.load_financial_data）。
            existing_data = self.file_mgr.load_financial_data(code)
            if existing_data is None:
                logger.warning(f"  ⚠️  {code} 財報檔損壞，以本次完整視窗重建")
                existing_data = {}

            annual_data, quarterly_data = ([], [])
            if not statements_df.empty:
                annual_data, quarterly_data = self.processor.process_financials(code, statements_df)
            status["fin"] = bool(quarterly_data)

            monthly_revenue = self.processor.process_monthly_revenue(code, revenue_df) if not revenue_df.empty else []
            status["rev"] = bool(monthly_revenue)

            latest_block = {}
            latest_quarter = quarterly_data[0] if quarterly_data else {}
            if latest_quarter:
                self.calculator.calculate_yoy_and_update_block(latest_block, latest_quarter, quarterly_data)

            data_quality = "high" if (status["fin"] and status["rev"]) else "medium"
            if not status["fin"]:
                data_quality = "low"
            status["quality"] = data_quality

            # 三大法人由 daily-update 的 TWSE T86 批次每日維護，財報更新不碰（傳空）。
            final_data = self.assembler.build_final_data(
                existing_data, code, name, latest_block,
                annual_data, quarterly_data, monthly_revenue,
                [], data_quality, {}
            )

            if self._save_cleaned(code, final_data):
                logger.debug(f"  ✔️  處理完畢 {code} {name}。品質: {data_quality}")
                return True, status
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


