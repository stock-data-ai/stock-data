# finance_tools/orchestration/data_assembler.py
import logging
from typing import Dict, Any, List
import pandas as pd
from finance_tools.core import DataProcessor
from finance_tools.core.timezone import today_str

logger = logging.getLogger(__name__)


class DataAssembler:
    """
    Assembles the final JSON data structure.
    """

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

        # 2. Update History (nested inside 'historical')
        if 'historical' not in existing_data:
            existing_data['historical'] = {}
        if 'marginTrading' not in existing_data['historical']:
            existing_data['historical']['marginTrading'] = {}

        date_key = margin_data.get('date', today_str())

        existing_data['historical']['marginTrading'][date_key] = {
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
    def merge_insider_holdings(existing_data: Dict, holdings: Dict[str, Any]) -> Dict:
        """把某公司當期的內部人持股併進財報檔。

        與 `shareholderDataRecent` / `shareholderDataHistory` 同一個拆法：
          - `insiderHoldingsRecent`  當期逐人明細（約 20~40 人、4 KB）
          - `insiderHoldingsHistory` 月份 → 兩組合計（每月約 100 bytes）

        為什麼明細只留當期：一家最多 250 人，每月留一份明細會讓檔案幾個月就翻倍；
        而要看趨勢的是「董監持股與質押比怎麼變」，那是合計層級的事，
        留在 History 就夠。這也是 TDCC 股權分散當初的取捨。
        """
        if not existing_data:
            existing_data = {}

        month = holdings.get('month')
        existing_data['insiderHoldingsRecent'] = {
            'month': month,
            'insiders': holdings['insiders'],
            'totals': holdings['totals'],
            'boardTotals': holdings['boardTotals'],
        }

        if 'insiderHoldingsHistory' not in existing_data:
            existing_data['insiderHoldingsHistory'] = {}
        if month:
            # 用月份當 key：同一期重跑會覆寫而不是長出重複，換月才新增一筆
            existing_data['insiderHoldingsHistory'][month] = {
                'totals': holdings['totals'],
                'boardTotals': holdings['boardTotals'],
            }

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

        existing_historical = final_data.get('historical', {})

        # Merge quarterly: keep existing records for (year, quarter) not covered by new data
        if quarterly:
            new_quarters = {(q['year'], q['quarter']) for q in quarterly}
            old_quarterly = [q for q in existing_historical.get('quarterly', []) if (q['year'], q['quarter']) not in new_quarters]
            merged_quarterly = sorted(quarterly + old_quarterly, key=lambda x: (x['year'], x['quarter']), reverse=True)
        else:
            merged_quarterly = existing_historical.get('quarterly', [])

        # Rebuild annual from the *full* merged quarterly history. Fetching only uses a
        # ~1yr window (FULL_UPDATE_DAYS), so the freshly-fetched `annual` re-derives recent
        # years from partial quarters; merging that by year would let a partial (累季) entry
        # overwrite a complete stored year and desync annual vs quarterly. Recomputing from
        # merged_quarterly keeps them consistent and self-heals already-corrupted files.
        # Balance-sheet/cash-flow fields (roe/roa/ocf/fcf…) live only on the existing annual
        # entries, so preserve them under the recomputed income-statement figures.
        if merged_quarterly:
            # 只保留由資產負債表/現金流量表補上的年度欄位；損益數字（含 note/revenueYoY）
            # 一律以季度重算為準，避免舊的「累季」註記殘留。
            _PRESERVE = ("roe", "roa", "ocf", "fcf", "currentRatio", "debtRatio")
            existing_annual_by_year = {a['year']: a for a in existing_historical.get('annual', [])}
            merged_annual = []
            for item in DataProcessor.aggregate_quarterly_to_annual(merged_quarterly):
                old = existing_annual_by_year.get(item['year'], {})
                item.update({k: old[k] for k in _PRESERVE if k in old})
                merged_annual.append(item)
        else:
            merged_annual = existing_historical.get('annual', [])

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

        # Merge monthly revenue: new months overwrite existing, old months outside fetch window preserved
        if monthly_list:
            new_month_keys = {(m['year'], m['month']) for m in monthly_list}
            old_monthly = [m for m in existing_historical.get('monthlyRevenue') or [] if (m['year'], m['month']) not in new_month_keys]
            merged_monthly = sorted(monthly_list + old_monthly, key=lambda x: (x['year'], x['month']), reverse=True)[:72]
        else:
            merged_monthly = existing_historical.get('monthlyRevenue') or None

        final_data['historical'].update({
            "annual": merged_annual,
            "quarterly": merged_quarterly,
            "monthlyRevenue": merged_monthly,
            "dividends": merged_dividends if merged_dividends else None,
            "institutionalInvestors": {**(existing_historical.get('institutionalInvestors') or {}), **(institutional_investors_data or {})} or None,
        })
        final_data["lastUpdated"] = today_str()
        final_data["dataQuality"] = quality
        logger.debug(f"Assembled final data for {code} with quality '{quality}'.")
        return final_data

