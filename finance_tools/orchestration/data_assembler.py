# finance_tools/orchestration/data_assembler.py
import logging
from typing import Dict, Any, List
import pandas as pd
from finance_tools.core.timezone import today_str

logger = logging.getLogger(__name__)


class DataAssembler:
    """
    Assembles the final JSON data structure.
    """

    @staticmethod
    def build_dividend_list(mops_data: Dict) -> list:
        """Builds a list of dividend records from MOPS data (one entry per payout)."""
        if not mops_data:
            return []
        records = mops_data.get("records", [])
        result = sorted(records, key=lambda r: (r["year"], r.get("sequence", 1)), reverse=True)
        logger.debug(f"Built dividend list with {len(result)} entries.")
        return result

    @staticmethod
    def merge_institutional_investors(existing_data: Dict, institutional_investors_data: Dict[str, Any]) -> Dict:
        """Merges only institutional investors data into existing company data."""
        if not existing_data:
            existing_data = {}
        if 'historical' not in existing_data:
            existing_data['historical'] = {}
        existing_inst = existing_data['historical'].get('institutionalInvestors') or {}
        existing_inst.update(institutional_investors_data or {})
        existing_data['historical']['institutionalInvestors'] = existing_inst or None
        existing_data['lastUpdated'] = today_str()
        return existing_data

    @staticmethod
    def merge_valuation(existing_data: Dict, valuation_stats: Dict[str, Any]) -> Dict:
        """Merges market cap, PE, PB, and Yield into existing company data."""
        if not existing_data:
            existing_data = {}
        if 'latest' not in existing_data:
            existing_data['latest'] = {}
        
        # Mapping Yahoo stats to our JSON structure
        if valuation_stats.get('marketCap') is not None:
            existing_data['latest']['marketCap'] = valuation_stats['marketCap']
        
        if valuation_stats.get('trailingPE') is not None:
            existing_data['latest']['pe'] = valuation_stats['trailingPE']
            
        if valuation_stats.get('priceToBook') is not None:
            existing_data['latest']['pb'] = valuation_stats['priceToBook']
            
        if valuation_stats.get('dividendYield') is not None:
            # Yahoo returns yield as decimal (0.05), we store as percentage
            existing_data['latest']['dividendYield'] = valuation_stats['dividendYield'] * 100
            
        existing_data['lastUpdated'] = today_str()
        return existing_data

    @staticmethod
    def merge_margin_trading(existing_data: Dict, margin_data: Dict[str, Any]) -> Dict:
        """Merges margin trading data for a specific date into history."""
        if not existing_data:
            existing_data = {}
        
        # 1. Update Latest
        if 'latest' not in existing_data:
            existing_data['latest'] = {}
        if margin_data.get('margin_balance') is not None:
            existing_data['latest']['marginBalance'] = int(margin_data['margin_balance'])
        if margin_data.get('short_balance') is not None:
            existing_data['latest']['shortBalance'] = int(margin_data['short_balance'])

        # 2. Update History
        if 'marginTradingHistory' not in existing_data:
            existing_data['marginTradingHistory'] = {}
        
        # Use target date if provided, otherwise today
        date_key = margin_data.get('date', today_str())
        
        existing_data['marginTradingHistory'][date_key] = {
            "marginBuy": int(margin_data.get('margin_buy', 0)),
            "marginSell": int(margin_data.get('margin_sell', 0)),
            "marginBalance": int(margin_data.get('margin_balance', 0)),
            "shortBuy": int(margin_data.get('short_buy', 0)),
            "shortSell": int(margin_data.get('short_sell', 0)),
            "shortBalance": int(margin_data.get('short_balance', 0))
        }
        
        existing_data['lastUpdated'] = today_str()
        return existing_data

    @staticmethod
    def build_final_data(existing_data: Dict, code: str, name: str, latest_block: Dict, annual: List, quarterly: List, monthly: List, dividends: List, quality: str, institutional_investors_data: Dict[str, Any], margin_trading_data: Dict[str, Any] = None):
        """Constructs the final data dictionary to be saved."""
        final_data = existing_data if existing_data else {}
        
        # Ensure 'latest' exists and preserve existing fields (like marketCap)
        if 'latest' not in final_data:
            final_data['latest'] = {}
        final_data['latest'].update(latest_block)

        final_data.update({
            "companyCode": code,
            "companyName": name,
        })
        if 'historical' not in final_data:
            final_data['historical'] = {}

        if margin_trading_data:
            final_data = DataAssembler.merge_margin_trading(final_data, margin_trading_data)

        # The 'monthly' data is already a list of dicts from the fetcher
        monthly_list = monthly if monthly else None

        # Merge annual: keep existing records for years not covered by new data
        existing_historical = final_data.get('historical', {})
        if annual:
            new_years = {a['year'] for a in annual}
            old_annual = [a for a in existing_historical.get('annual', []) if a['year'] not in new_years]
            merged_annual = sorted(annual + old_annual, key=lambda x: x['year'], reverse=True)
        else:
            merged_annual = existing_historical.get('annual', [])

        # Merge quarterly: keep existing records for (year, quarter) not covered by new data
        if quarterly:
            new_quarters = {(q['year'], q['quarter']) for q in quarterly}
            old_quarterly = [q for q in existing_historical.get('quarterly', []) if (q['year'], q['quarter']) not in new_quarters]
            merged_quarterly = sorted(quarterly + old_quarterly, key=lambda x: (x['year'], x['quarter']), reverse=True)
        else:
            merged_quarterly = existing_historical.get('quarterly', [])

        # Merge dividends: keep existing records for (year, sequence) not covered by new data
        if dividends:
            new_div_keys = {(d['year'], d.get('sequence', 1)) for d in dividends}
            old_dividends = [
                d for d in existing_historical.get('dividends', []) or []
                if (d['year'], d.get('sequence', 1)) not in new_div_keys
            ]
            merged_dividends = sorted(
                dividends + old_dividends,
                key=lambda x: (x['year'], x.get('sequence', 1)),
                reverse=True
            )
        else:
            merged_dividends = existing_historical.get('dividends', [])

        final_data['historical'].update({
            "annual": merged_annual,
            "quarterly": merged_quarterly,
            "monthlyRevenue": monthly_list[:72] if monthly_list else None,
            "dividends": merged_dividends if merged_dividends else None,
            "institutionalInvestors": {**(existing_historical.get('institutionalInvestors') or {}), **(institutional_investors_data or {})} or None,
        })
        final_data["lastUpdated"] = today_str()
        final_data["dataQuality"] = quality
        logger.debug(f"Assembled final data for {code} with quality '{quality}'.")
        return final_data

