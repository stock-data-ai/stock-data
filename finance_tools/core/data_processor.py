"""
Data Processor - 統一的數據處理邏輯
處理單位轉換、計算 YoY、格式化等
"""

import logging
import pandas as pd
import math
from typing import Optional, List, Dict, Any, Tuple

logger = logging.getLogger(__name__)

# FinMind 的科目名稱同一個科目有半形與全形括號兩種寫法，而且**依年度而異**：
# 現金流量表 2020～2023 年混用（2020、2021 最嚴重），2024 年起全部全形。
# 程式裡的科目清單只寫全形，於是「營業活動之淨現金流入(流出)」整年對不上，
# ocf 變 None、fcf 跟著算不出來。2026-09-11 實測 823 家公司的 2021 年就是這樣缺的。
# 三張報表比對前一律先正規化；損益表目前沒遇到，但它對不上的科目會變成 **0**
# 而不是 None，比現金流量表更危險，所以一併預防。
_HALF_TO_FULL = str.maketrans({"(": "（", ")": "）"})


def normalize_item_name(name) -> str:
    """科目名稱正規化：半形括號 → 全形、去掉首尾空白。"""
    return str(name).translate(_HALF_TO_FULL).strip()


class DataProcessor:
    """數據處理器 - 統一處理財務數據

    注意：所有 FinMind API 返回的金額數據單位為「元 (NTD)」
    輸出時保持「元」單位，不做任何單位轉換
    """

    # 損益表目標科目（也是全市場抓取時要留下的科目，見 `market_fetch`）
    INCOME_STATEMENT_ITEMS = {
        "Revenue": ["營業收入", "營業收入合計", "淨收益", "Revenue"],
        "GrossProfit": ["營業毛利（毛損）", "GrossProfit"],
        "OperatingIncome": ["營業利益（損失）", "OperatingIncome"],
        "NetIncome": [
            "淨利（淨損）歸屬於母公司業主",
            "本期淨利（淨損）",
            "NetIncome",
        ],
        "EPS": ["基本每股盈餘", "基本每股盈餘（元）", "EPS"],
        # 稅前淨利與所得稅：算有效稅率用（判斷淨利成長是否被稅率放大）。
        # 只認「所得稅費用（利益）」：另一種寫法「所得稅（費用）利益」正負號相反，不能混用。
        "PreTaxIncome": ["稅前淨利（淨損）", "繼續營業單位稅前淨利（淨損）"],
        "IncomeTax": ["所得稅費用（利益）"],
    }

    # 資產負債表目標科目（全市場抓取時據此過濾）
    BALANCE_SHEET_ITEMS = {
        "totalAssets":        ["資產總額", "資產合計"],
        "totalLiabilities":   ["負債總額", "負債合計"],
        "equity":             ["歸屬於母公司業主之權益合計", "權益總額", "權益合計"],
        "currentAssets":      ["流動資產合計", "流動資產"],
        "currentLiabilities": ["流動負債合計", "流動負債"],
        # 應收帳款三項分開抓，由呼叫端加總成 accountsReceivable（票據＋帳款＋關係人帳款）。
        # 只算「應收帳款淨額」會漏掉票據：8103 2026Q2 年增 45.8%，加總後 48.5% 才與市場分析一致。
        "notesReceivable":        ["應收票據淨額"],
        "tradeReceivable":        ["應收帳款淨額"],
        "tradeReceivableRelated": ["應收帳款－關係人淨額"],
        "inventory":              ["存貨", "存貨合計"],
    }

    # 現金流量表目標科目（全市場抓取時據此過濾）
    CASH_FLOW_ITEMS = {
        "ocf":   ["營業活動之淨現金流入（流出）", "營業活動之淨現金流入"],
        "capex": ["取得不動產、廠房及設備", "購置不動產、廠房及設備"],
    }

    @staticmethod
    def clean_nan(obj: Any) -> Any:
        """遞迴清除 NaN 值"""
        if isinstance(obj, float):
            return None if math.isnan(obj) else obj
        elif isinstance(obj, dict):
            return {k: DataProcessor.clean_nan(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [DataProcessor.clean_nan(i) for i in obj]
        return obj

    @staticmethod
    def aggregate_quarterly_to_annual(quarterly_data: List[Dict]) -> List[Dict]:
        """從季度損益資料加總出年度資料（含累季註記與 YoY）。

        以「完整季度歷史」為輸入時可保證年度與季度一致；抓取視窗只含部分季度時，
        該年度會被標記為「累季」。金額單位為元，僅四捨五入不做單位轉換。
        Returns: annual_data，依年份新到舊排序。
        """
        annual_map: Dict = {}

        # Determine each year's quarter count — 僅計入具損益資料的季度。
        # merged quarterly 可能含資產負債表補進、只有 currentRatio/debtRatio 而無 revenue
        # 的季度，若一併計數會讓「累季」判斷失準（如最舊年度誤判為完整）。
        quarter_counts: Dict = {}
        for q in quarterly_data:
            if q.get("revenue") is None:
                continue
            quarter_counts.setdefault(q["year"], set()).add(q["quarter"])

        for q in quarterly_data:
            year = q["year"]
            # 僅加總具損益資料的季度（資產負債表補進的季度可能無 revenue）
            if q.get("revenue") is None:
                continue

            if year not in annual_map:
                annual_map[year] = {
                    "year": year,
                    "revenue": 0,
                    "grossProfit": 0,
                    "operatingIncome": 0,
                    "netIncome": 0,
                    "eps": 0,
                }

            annual_map[year]["revenue"] += q.get("revenue", 0) or 0
            annual_map[year]["grossProfit"] += q.get("grossProfit", 0) or 0
            annual_map[year]["operatingIncome"] += q.get("operatingIncome", 0) or 0
            annual_map[year]["netIncome"] += q.get("netIncome", 0) or 0
            annual_map[year]["eps"] += q.get("eps", 0) or 0

        # 年度數據後處理（金額單位為元，僅做四捨五入並計算利潤率）
        annual_data = []

        # Sort by year to make YoY calculation easier
        sorted_annual_list = sorted(annual_map.values(), key=lambda x: x['year'])
        annual_map_dict = {item['year']: item for item in sorted_annual_list}

        for year_data in sorted_annual_list:
            year = year_data['year']

            revenue_ntd = round(year_data["revenue"], 0)
            gross_ntd = round(year_data["grossProfit"], 0)
            op_ntd = round(year_data["operatingIncome"], 0)
            net_ntd = round(year_data["netIncome"], 0)

            if revenue_ntd > 0:
                gross_margin = round(gross_ntd / revenue_ntd * 100, 2)
                op_margin = round(op_ntd / revenue_ntd * 100, 2)
                net_margin = round(net_ntd / revenue_ntd * 100, 2)
            else:
                gross_margin = op_margin = net_margin = 0

            item = {
                "year": year,
                "revenue": revenue_ntd,
                "grossProfit": gross_ntd,
                "operatingIncome": op_ntd,
                "netIncome": net_ntd,
                "eps": round(year_data["eps"], 2),
                "grossMargin": gross_margin,
                "operatingMargin": op_margin,
                "netMargin": net_margin,
            }

            # Add note for partial years
            num_quarters = len(quarter_counts.get(year, set()))
            if num_quarters < 4:
                item["note"] = "累季"

            # Calculate YoY
            yoy = None
            prev_year_data_map = annual_map_dict.get(year - 1)

            if prev_year_data_map:
                current_revenue = item["revenue"]

                # If current year is partial, get partial revenue from previous year
                if num_quarters < 4:
                    quarters_to_sum = quarter_counts.get(year, set())
                    prev_revenue_partial = sum(
                        q['revenue'] for q in quarterly_data
                        if q['year'] == (year - 1) and q['quarter'] in quarters_to_sum
                        and q.get('revenue') is not None
                    )
                    if prev_revenue_partial > 0:
                        yoy = round(((current_revenue - prev_revenue_partial) / prev_revenue_partial) * 100, 2)
                # Full year comparison
                else:
                    prev_revenue_full = round(prev_year_data_map.get("revenue", 0), 0)
                    # We also need to ensure previous year was a full year
                    prev_num_quarters = len(quarter_counts.get(year - 1, set()))
                    if prev_revenue_full > 0 and prev_num_quarters == 4:
                         yoy = round(((current_revenue - prev_revenue_full) / prev_revenue_full) * 100, 2)

            item["revenueYoY"] = yoy

            annual_data.append(item)

        annual_data.sort(key=lambda x: x["year"], reverse=True)
        return annual_data

    @classmethod
    def process_financials(
        cls, stock_id: str, financials_df: pd.DataFrame
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        處理財務報表數據
        輸入單位: 元 (FinMind API 返回值)
        輸出單位: 元 (保持原單位，不做轉換)
        Returns: (annual_data, quarterly_data)
        """
        if financials_df.empty:
            return [], []

        TARGET_ITEMS = cls.INCOME_STATEMENT_ITEMS
        # 這兩欄來源沒有就寫 None，不像舊欄位補 0：0 會被讀成「稅率 0%」。
        OPTIONAL_KEY_MAP = {
            "PreTaxIncome": "preTaxIncome",
            "IncomeTax": "incomeTax",
        }

        financials_df = financials_df.copy()
        financials_df["origin_name"] = financials_df["origin_name"].map(normalize_item_name)

        name_to_key = {
            name: key for key, names in TARGET_ITEMS.items() for name in names
        }
        # Priority: earlier in the list = higher priority (0 = best)
        name_to_priority = {
            name: i
            for key, names in TARGET_ITEMS.items()
            for i, name in enumerate(names)
        }

        # 標準化指標名稱
        financials_df["metric"] = financials_df["origin_name"].map(name_to_key)
        financials_df = financials_df.dropna(subset=["metric"]).copy()
        financials_df["date"] = pd.to_datetime(financials_df["date"])

        # Deduplicate (date, metric) by priority to avoid double-counting when
        # multiple origin_names map to the same metric key (e.g. both
        # "淨利（淨損）歸屬於母公司業主" and "本期淨利（淨損）" → "NetIncome").
        financials_df["_priority"] = (
            financials_df["origin_name"].map(name_to_priority).fillna(999)
        )
        financials_df = (
            financials_df.sort_values("_priority")
            .drop_duplicates(subset=["date", "metric"], keep="first")
            .drop(columns=["_priority"])
        )

        # Pivot 成季度財報表
        pivot_df = (
            financials_df.pivot_table(
                index="date", columns="metric", values="value", aggfunc="sum"
            )
            .sort_index()
            .reset_index()
        )

        pivot_df["year"] = pivot_df["date"].dt.year
        pivot_df["quarter"] = pivot_df["date"].dt.quarter

        # 組合季度資料
        KEY_MAP = {
            "Revenue": "revenue",
            "GrossProfit": "grossProfit",
            "OperatingIncome": "operatingIncome",
            "NetIncome": "netIncome",
            "EPS": "eps",
        }

        quarterly_data = []
        for _, row in pivot_df.iterrows():
            q = {"year": int(row["year"]), "quarter": int(row["quarter"])}
            for src_key, out_key in KEY_MAP.items():
                q[out_key] = float(row.get(src_key, 0) or 0)
            for src_key, out_key in OPTIONAL_KEY_MAP.items():
                v = row.get(src_key)
                q[out_key] = None if v is None or pd.isna(v) else round(float(v), 0)
            quarterly_data.append(q)

        quarterly_data.sort(key=lambda x: (x["year"], x["quarter"]))

        # 計算年度數據（從季度加總）
        annual_data = cls.aggregate_quarterly_to_annual(quarterly_data)

        # 季度數據整理（金額單位為元，僅做四捨五入並計算利潤率）
        for q in quarterly_data:
            revenue_ntd = round(q.get("revenue", 0), 0)
            gross_ntd = round(q.get("grossProfit", 0), 0)
            op_ntd = round(q.get("operatingIncome", 0), 0)
            net_ntd = round(q.get("netIncome", 0), 0)

            if revenue_ntd > 0:
                gross_margin = round(gross_ntd / revenue_ntd * 100, 2)
                op_margin = round(op_ntd / revenue_ntd * 100, 2)
                net_margin = round(net_ntd / revenue_ntd * 100, 2)
            else:
                gross_margin = op_margin = net_margin = 0

            q["revenue"] = revenue_ntd
            q["grossProfit"] = gross_ntd
            q["operatingIncome"] = op_ntd
            q["netIncome"] = net_ntd
            q["eps"] = round(q.get("eps", 0), 2)
            q["grossMargin"] = gross_margin
            q["operatingMargin"] = op_margin
            q["netMargin"] = net_margin

        quarterly_data.sort(key=lambda x: (x["year"], x["quarter"]), reverse=True)

        return annual_data, quarterly_data

    @classmethod
    def process_monthly_revenue(
        cls, stock_id: str, revenue_df: pd.DataFrame
    ) -> List[Dict]:
        """
        處理月營收數據
        輸入單位: 元 (FinMind API 返回值)
        輸出單位: 元 (保持原單位，不做轉換)
        """
        if revenue_df.empty:
            return []

        required_cols = ["revenue", "revenue_year", "revenue_month"]
        if not all(col in revenue_df.columns for col in required_cols):
            logger.warning("Missing required columns in revenue data")
            return []

        monthly_data = []
        revenue_df = revenue_df.sort_values(["revenue_year", "revenue_month"])

        for _, row in revenue_df.iterrows():
            year = int(row["revenue_year"])
            month = int(row["revenue_month"])
            revenue_ntd = (
                float(row["revenue"]) if pd.notna(row["revenue"]) else 0
            )  # 元 (NTD)

            # 計算 YoY
            yoy = None
            prev_year_data = revenue_df[
                (revenue_df["revenue_year"] == year - 1)
                & (revenue_df["revenue_month"] == month)
            ]
            if not prev_year_data.empty:
                prev_revenue = float(prev_year_data.iloc[0]["revenue"])
                if prev_revenue > 0:
                    yoy = round(
                        ((revenue_ntd - prev_revenue) / prev_revenue) * 100, 2
                    )

            monthly_data.append(
                {"year": year, "month": month, "revenue": round(revenue_ntd, 0), "yoy": yoy}
            )

        # 降序排列
        monthly_data.sort(key=lambda x: (x["year"], x["month"]), reverse=True)

        return monthly_data

    @classmethod
    def process_dividends(cls, stock_id: str, dividend_df: pd.DataFrame) -> List[Dict]:
        """
        處理股利數據
        FinMind API 的 year 欄位可能包含中文（如 "109年第3季"），需要匯總
        """
        if dividend_df.empty:
            return []

        import re
        
        # 使用字典按年份匯總股利
        dividend_summary = {}

        for _, row in dividend_df.iterrows():
            try:
                # 解析年份
                year_raw = str(row.get("year", ""))
                year_match = re.search(r'\d+', year_raw)
                if not year_match:
                    continue

                year_num = int(year_match.group())
                # 民國年轉西元年
                year = year_num + 1911 if year_num < 1000 else year_num

                cash = float(row.get("cash_dividend", 0) or 0)
                stock = float(row.get("stock_dividend", 0) or 0)

                if year not in dividend_summary:
                    dividend_summary[year] = {
                        "cashDividend": 0.0,
                        "stockDividend": 0.0,
                    }
                
                dividend_summary[year]["cashDividend"] += cash
                dividend_summary[year]["stockDividend"] += stock

            except (ValueError, TypeError):
                continue
        
        # 轉換為最終的列表格式
        dividends = []
        for year, data in dividend_summary.items():
            total_dividend = data["cashDividend"] + data["stockDividend"]
            dividends.append({
                "year": year,
                "cashDividend": round(data["cashDividend"], 4),
                "stockDividend": round(data["stockDividend"], 4),
                "totalDividend": round(total_dividend, 4),
            })

        dividends.sort(key=lambda x: x["year"], reverse=True)
        return dividends

    _MONTH_TO_QUARTER = {3: 1, 6: 2, 9: 3, 12: 4}

    @classmethod
    def process_balance_sheet(
        cls, stock_id: str, bs_df: pd.DataFrame
    ) -> Dict[tuple, Dict]:
        """
        處理資產負債表，回傳所有季度快照。
        FinMind 每個項目有兩列：一列元值、一列百分比，
        取 abs(value) 最大者（元值）以避免取到百分比列。

        Returns: {(year, quarter): {totalAssets, totalLiabilities, equity,
                                    currentAssets, currentLiabilities}}
        e.g. {(2024, 4): {...}, (2024, 3): {...}, ...}
        Q4 資料可用於計算年度 ROE/ROA；所有季度可用於季度 currentRatio/debtRatio。
        """
        if bs_df.empty:
            return {}

        TARGET = cls.BALANCE_SHEET_ITEMS
        all_names = {name: key for key, names in TARGET.items() for name in names}
        name_priority = {
            name: i
            for key, names in TARGET.items()
            for i, name in enumerate(names)
        }

        bs_df = bs_df.copy()
        bs_df["origin_name"] = bs_df["origin_name"].map(normalize_item_name)
        bs_df["date"] = pd.to_datetime(bs_df["date"])
        bs_df["_metric"] = bs_df["origin_name"].map(all_names)
        bs_df = bs_df.dropna(subset=["_metric"])

        bs_df["_abs"] = bs_df["value"].abs()
        bs_df = (
            bs_df.sort_values("_abs", ascending=False)
            .drop_duplicates(subset=["date", "_metric"], keep="first")
        )

        result: Dict[tuple, Dict] = {}
        for _, row in bs_df.iterrows():
            date = row["date"]
            quarter = cls._MONTH_TO_QUARTER.get(date.month)
            if quarter is None:
                continue  # 非季末日期，跳過
            key = (date.year, quarter)
            metric = row["_metric"]
            value = float(row["value"])

            if key not in result:
                result[key] = {}

            existing_origin = result[key].get(f"_origin_{metric}")
            current_priority = name_priority.get(row["origin_name"], 999)
            existing_priority = name_priority.get(existing_origin, 999) if existing_origin else 999
            if existing_origin is None or current_priority < existing_priority:
                result[key][metric] = value
                result[key][f"_origin_{metric}"] = row["origin_name"]

        for key in result:
            result[key] = {k: v for k, v in result[key].items() if not k.startswith("_origin_")}

        years = sorted({k[0] for k in result})
        logger.debug(f"  Processed balance sheet for {stock_id}: {years} years ({len(result)} quarters)")
        return result

    @classmethod
    def process_cash_flows(
        cls, stock_id: str, cf_df: pd.DataFrame
    ) -> Dict[int, Dict]:
        """
        處理現金流量表，回傳每年全年累計值 (Q4 = 12/31)。
        FinMind 現金流量表為「累計制」，必須只取 Q4 值，不可加總各季。
        同一指標可能有兩個重複 origin_name，以 drop_duplicates 處理。

        Returns: {year: {ocf, capex}}
        """
        if cf_df.empty:
            return {}

        OCF_NAMES = cls.CASH_FLOW_ITEMS["ocf"]
        CAPEX_NAMES = cls.CASH_FLOW_ITEMS["capex"]

        cf_df = cf_df.copy()
        cf_df["origin_name"] = cf_df["origin_name"].map(normalize_item_name)
        cf_df["date"] = pd.to_datetime(cf_df["date"])

        # 只取年底 (Q4 = 12月)
        cf_q4 = cf_df[cf_df["date"].dt.month == 12].copy()
        if cf_q4.empty:
            return {}

        result: Dict[int, Dict] = {}
        for year, group in cf_q4.groupby(cf_q4["date"].dt.year):
            # 去除重複 origin_name（保留第一筆，值相同）
            group = group.drop_duplicates(subset=["origin_name"])

            ocf_row   = group[group["origin_name"].isin(OCF_NAMES)]
            capex_row = group[group["origin_name"].isin(CAPEX_NAMES)]

            ocf   = float(ocf_row["value"].iloc[0])   if not ocf_row.empty   else None
            capex = float(capex_row["value"].iloc[0]) if not capex_row.empty else None

            result[int(year)] = {"ocf": ocf, "capex": capex}

        logger.debug(f"  Processed cash flows for {stock_id}: {sorted(result.keys())} years")
        return result

    @classmethod
    def process_quarterly_cash_flows(
        cls, stock_id: str, cf_df: pd.DataFrame, reported: Optional[set] = None
    ) -> Dict[tuple, Dict]:
        """
        把累計制現金流量表拆成「單期」數字，期別與損益表 quarterly[] 的那一列對齊。

        FinMind 每季回報的是年初至季末的累計值（Q2 = 上半年合計），所以
        單期 = 本期累計 − 同年「前一個申報期」的累計；該年第一個申報期直接用累計值。
        `reported` 是這家公司損益表實際有的期別 {(year, quarter)}：
          - 按季申報：前一個申報期就是上一季，結果與「本季－上一季」相同。
          - 只申報半年報（沒有 Q1／Q3，約 300 家，多為生技與小型股）：quarterly[] 的
            Q2 那列是上半年、Q4 那列是下半年，現金流也要拆成上半年／下半年才對得上。
            舊規則固定減上一季，這些公司 Q1 不存在，於是全部算不出來。
        前一個申報期的累計缺資料時無法拆，該期回 None，不拿累計值冒充單期。
        `reported` 為 None 時視為每一季都有申報。

        Returns: {(year, quarter): {"ocf": float|None, "capex": float|None}}
        capex 維持來源正負號（支出為負），與年度 fcf = ocf + capex 的算法一致。
        """
        if cf_df.empty:
            return {}

        FIELDS = cls.CASH_FLOW_ITEMS

        cf_df = cf_df.copy()
        cf_df["origin_name"] = cf_df["origin_name"].map(normalize_item_name)
        cf_df["date"] = pd.to_datetime(cf_df["date"])

        # 先取各季末的累計值：{(year, quarter): {field: value}}
        cumulative: Dict[tuple, Dict] = {}
        for date, group in cf_df.groupby("date"):
            quarter = cls._MONTH_TO_QUARTER.get(date.month)
            if quarter is None:
                continue
            values = {}
            for field, names in FIELDS.items():
                for name in names:  # 依優先順序取第一個有值的科目
                    rows = group[group["origin_name"] == name]
                    if not rows.empty:
                        values[field] = float(rows["value"].iloc[0])
                        break
            if values:
                cumulative[(date.year, quarter)] = values

        result: Dict[tuple, Dict] = {}
        def previous_period(year: int, quarter: int) -> Optional[int]:
            """同一年、在 quarter 之前最近的申報期；沒有就回 None（本期是當年第一期）。"""
            # 現金流量表本身有那一期，也算有申報：損益表少一列時不會把上半年累計當成單季
            earlier = [q for q in range(quarter - 1, 0, -1)
                       if reported is None or (year, q) in reported or (year, q) in cumulative]
            return earlier[0] if earlier else None

        for (year, quarter), cum in cumulative.items():
            prev_q = previous_period(year, quarter)
            prev = cumulative.get((year, prev_q)) if prev_q is not None else None
            single = {}
            for field in FIELDS:
                cur = cum.get(field)
                if cur is None:
                    single[field] = None
                elif prev_q is None:
                    single[field] = round(cur, 0)
                elif prev is not None and prev.get(field) is not None:
                    single[field] = round(cur - prev[field], 0)
                else:
                    single[field] = None
            result[(year, quarter)] = single

        logger.debug(f"  Processed quarterly cash flows for {stock_id}: {len(result)} quarters")
        return result
