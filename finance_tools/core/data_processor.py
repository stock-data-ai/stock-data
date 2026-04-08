"""
Data Processor - 統一的數據處理邏輯
處理單位轉換、計算 YoY、格式化等
"""

import logging
import pandas as pd
import math
from typing import List, Dict, Any, Tuple

logger = logging.getLogger(__name__)


class DataProcessor:
    """數據處理器 - 統一處理財務數據

    注意：所有 FinMind API 返回的金額數據單位為「元 (NTD)」
    輸出時保持「元」單位，不做任何單位轉換
    """

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

        # 目標財務指標 mapping
        TARGET_ITEMS = {
            "Revenue": ["營業收入", "營業收入合計", "淨收益", "Revenue"],
            "GrossProfit": ["營業毛利（毛損）", "GrossProfit"],
            "OperatingIncome": ["營業利益（損失）", "OperatingIncome"],
            "NetIncome": [
                "淨利（淨損）歸屬於母公司業主",
                "本期淨利（淨損）",
                "NetIncome",
            ],
            "EPS": ["基本每股盈餘", "基本每股盈餘（元）", "EPS"],
        }

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
            quarterly_data.append(q)

        quarterly_data.sort(key=lambda x: (x["year"], x["quarter"]))

        # 計算年度數據（從季度加總）
        annual_map = {}
        
        # Determine the latest year and its quarter count
        quarter_counts = {}
        for q in quarterly_data:
            year = q["year"]
            if year not in quarter_counts:
                quarter_counts[year] = set()
            quarter_counts[year].add(q["quarter"])

        latest_year = max(quarter_counts.keys()) if quarter_counts else 0
        
        for q in quarterly_data:
            year = q["year"]

            if year not in annual_map:
                annual_map[year] = {
                    "year": year,
                    "revenue": 0,
                    "grossProfit": 0,
                    "operatingIncome": 0,
                    "netIncome": 0,
                    "eps": 0,
                }

            annual_map[year]["revenue"] += q["revenue"]
            annual_map[year]["grossProfit"] += q["grossProfit"]
            annual_map[year]["operatingIncome"] += q["operatingIncome"]
            annual_map[year]["netIncome"] += q["netIncome"]
            annual_map[year]["eps"] += q["eps"]

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
