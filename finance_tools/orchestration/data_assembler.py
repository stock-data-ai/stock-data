# finance_tools/orchestration/data_assembler.py
import logging
from typing import Dict, Any, List
import pandas as pd
from finance_tools.core import DataProcessor
from finance_tools.core import merge_policy
from finance_tools.domains.financials.calculator import FinancialCalculator
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
        # 新上市公司的檔案常由這裡第一次建立；只寫 latest 會讓已上架 App 讀
        # `historical.xxx` 時整頁崩潰（2026-10-27 的 7859、7946），骨架一律補齊。
        if not isinstance(existing_data.get('historical'), dict):
            existing_data['historical'] = {}

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
    def merge_insider_holdings(existing_data: Dict, holdings: Dict[str, Any]) -> Dict:
        """把某公司當期的內部人持股併進財報檔。

        與 `shareholderDataRecent` / `shareholderDataHistory` 同一個拆法：
          - `insiderHoldingsRecent`  當期逐人明細（約 20~40 人、4 KB）
          - `insiderHoldingsHistory` 月份 → 兩組合計（每月約 100 bytes）
          - `insiderHoldingsChanges` 月份 → 跟上一期比、持股或質押有變的人（每月通常幾筆）

        為什麼明細只留當期：一家最多 250 人，每月留一份明細會讓檔案幾個月就翻倍；
        而要看趨勢的是「董監持股與質押比怎麼變」，那是合計層級的事，
        留在 History 就夠。這也是 TDCC 股權分散當初的取捨。
        """
        if not existing_data:
            existing_data = {}

        month = holdings.get('month')
        # 明細被覆寫之前先跟上一期比：逐人歷史不留全份明細（見上），只留「這個月誰變了」，
        # 公司頁股價圖才標得出「董事長 等 2 人 持股增加」。同月重跑不重算（會跟自己比成零變動）。
        prev = existing_data.get('insiderHoldingsRecent') or {}
        prev_month = prev.get('month')
        if month and prev_month and prev_month < month and prev.get('insiders') is not None:
            changes = DataAssembler.insider_person_changes(prev['insiders'], holdings.get('insiders') or [])
            existing_data.setdefault('insiderHoldingsChanges', {})[month] = {
                'prev': prev_month,
                'changes': changes,
            }

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
    def insider_person_changes(prev_insiders: List[Dict], cur_insiders: List[Dict]) -> List[Dict]:
        """兩期逐人明細 → 持股或質押有變的人。

        `from` 為 None＝上一期不在名單（新任、新列大股東）；`to` 為 None＝這一期不在名單（卸任）。
        卸任那一筆不能當成「賣光」：人不在名單上，持股就不再申報，不代表股票賣掉了。
        依持股變動絕對值排序，前端取前幾名組「董事長 等 N 人」。
        """
        def index(rows):
            return {r.get('person'): r for r in rows if r.get('person')}

        before, after = index(prev_insiders), index(cur_insiders)
        out = []
        for person in list(after) + [p for p in before if p not in after]:
            a, b = before.get(person), after.get(person)
            frm = a.get('shares') if a else None
            to = b.get('shares') if b else None
            pf = a.get('pledged') if a else None
            pt = b.get('pledged') if b else None
            if a and b and frm == to and pf == pt:
                continue
            out.append({
                'person': person,
                'roles': (b or a).get('roles') or [],
                'from': frm,
                'to': to,
                'pledgedFrom': pf,
                'pledgedTo': pt,
            })
        out.sort(key=lambda c: -abs((c['to'] or 0) - (c['from'] or 0)))
        return out

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

        # Merge quarterly：重抓到的季度只帶損益數字；currentRatio/debtRatio 是資產負債表
        # 任務（每週一）另外補上的季度快照，整筆換掉會把它們沖掉，個股健檢的
        # 「資料截止期別」就會倒退一季（`financialHealth.ts` 取最近一季有值的快照）。
        merged_quarterly = merge_policy.merge_by_key(
            existing_historical.get('quarterly'), quarterly,
            key=lambda q: (q['year'], q['quarter']),
            preserve=merge_policy.QUARTERLY_BS_CF_FIELDS,
        )

        # Rebuild annual from the *full* merged quarterly history. The fetch window is finite
        # (config.INCOME_STATEMENT_YEARS), so the freshly-fetched `annual` can re-derive edge
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

        merged_dividends = merge_policy.merge_by_key(
            existing_historical.get('dividends'), dividends,
            key=lambda d: (d['year'], d.get('sequence', 1)),
        )

        # 月營收：保留上限集中在 merge_policy，`update-revenue` 走同一條規則。
        merged_monthly = merge_policy.merge_by_key(
            existing_historical.get('monthlyRevenue'), monthly_list,
            key=lambda m: (m['year'], m['month']),
            limit=merge_policy.MONTHLY_REVENUE_LIMIT,
        ) or None

        # 年增率一律用**合併後的完整歷史**重算：抓取視窗只有一年，去年同期多半不在視窗裡
        FinancialCalculator.fill_monthly_yoy(merged_monthly)
        if final_data['latest'].get('year') is not None:
            final_data['latest']['yoy'] = FinancialCalculator.quarter_yoy(
                merged_quarterly, final_data['latest'].get('year'), final_data['latest'].get('quarter'))

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

