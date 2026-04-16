# finance_tools/processing/data_assembler.py
import logging
from typing import Dict, Any, List
import pandas as pd
from core.timezone import today_str

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
        existing_data['historical']['institutionalInvestors'] = institutional_investors_data if institutional_investors_data else None
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
        if 'marketCap' in valuation_stats and valuation_stats['marketCap']:
            existing_data['latest']['marketCap'] = valuation_stats['marketCap']
        
        if 'trailingPE' in valuation_stats and valuation_stats['trailingPE']:
            existing_data['latest']['peRatio'] = valuation_stats['trailingPE']
            
        if 'priceToBook' in valuation_stats and valuation_stats['priceToBook']:
            existing_data['latest']['pbRatio'] = valuation_stats['priceToBook']
            
        if 'dividendYield' in valuation_stats and valuation_stats['dividendYield']:
            # Yahoo returns yield as decimal (0.05), we store as percentage? 
            # Let's keep consistency with project. (Checking other files...)
            existing_data['latest']['dividendYield'] = valuation_stats['dividendYield'] * 100
            
        existing_data['lastUpdated'] = today_str()
        return existing_data

    @staticmethod
    def merge_marketcap(existing_data: Dict, market_cap: float) -> Dict:
        """Merges only market cap into existing company data."""
        if not existing_data:
            existing_data = {}
        if 'latest' not in existing_data:
            existing_data['latest'] = {}
        existing_data['latest']['marketCap'] = market_cap
        existing_data['lastUpdated'] = today_str()
        return existing_data

    @staticmethod
    def build_final_data(existing_data: Dict, code: str, name: str, latest_block: Dict, annual: List, quarterly: List, monthly: List, dividends: List, quality: str, institutional_investors_data: Dict[str, Any]):
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
            "institutionalInvestors": institutional_investors_data if institutional_investors_data else None,
        })
        final_data["lastUpdated"] = today_str()
        final_data["dataQuality"] = quality
        logger.debug(f"Assembled final data for {code} with quality '{quality}'.")
        return final_data

