"""損益表缺值寫 None、真的 0 留 0（data_processor「缺值一律 None」）。

2026-11 查證：金融業損益表本來就沒有「營業毛利」「營業利益」，以前補 0，
富邦金等 19 家顯示「毛利率 0%」，並以「營業利益率 0%」送進 AI 分析。
"""
import pandas as pd

from finance_tools.core.data_processor import DataProcessor


def statements(rows):
    """FinMind TaiwanStockFinancialStatements 的長表：[(date, origin_name, value), ...]"""
    return pd.DataFrame([{"date": d, "origin_name": n, "value": v} for d, n, v in rows])


def bank_quarter(date, revenue=1000.0, net=400.0, eps=1.5):
    # 金融業：有淨收益、淨利、EPS，沒有營業毛利、營業利益
    return [(date, "淨收益", revenue), (date, "本期淨利（淨損）", net), (date, "基本每股盈餘", eps)]


def normal_quarter(date, revenue=1000.0, gross=500.0, op=300.0, net=200.0, eps=1.0):
    return [(date, "營業收入合計", revenue), (date, "營業毛利（毛損）", gross), (date, "營業利益（損失）", op),
            (date, "本期淨利（淨損）", net), (date, "基本每股盈餘", eps)]


QUARTER_ENDS = ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]


def test_financial_company_missing_items_are_none_not_zero():
    rows = [r for d in QUARTER_ENDS for r in bank_quarter(d)]
    annual, quarterly = DataProcessor.process_financials("2881", statements(rows))
    q = quarterly[0]
    assert q["grossProfit"] is None and q["operatingIncome"] is None
    assert q["grossMargin"] is None and q["operatingMargin"] is None
    # 有的科目照常
    assert q["revenue"] == 1000 and q["netIncome"] == 400 and q["eps"] == 1.5 and q["netMargin"] == 40.0
    a = annual[0]
    assert a["grossProfit"] is None and a["grossMargin"] is None and a["operatingMargin"] is None
    assert a["revenue"] == 4000 and a["netIncome"] == 1600 and a["netMargin"] == 40.0 and a["eps"] == 6.0


def test_normal_company_unchanged():
    rows = [r for d in QUARTER_ENDS for r in normal_quarter(d)]
    annual, quarterly = DataProcessor.process_financials("2330", statements(rows))
    q = quarterly[0]
    assert (q["grossMargin"], q["operatingMargin"], q["netMargin"], q["eps"]) == (50.0, 30.0, 20.0, 1.0)
    a = annual[0]
    assert (a["revenue"], a["grossProfit"], a["grossMargin"], a["eps"]) == (4000, 2000, 50.0, 4.0)


def test_real_zero_stays_zero():
    # 來源真的報 0（毛利 0、淨利 0、EPS 0）不可變成 None
    rows = [r for d in QUARTER_ENDS for r in normal_quarter(d, gross=0.0, net=0.0, eps=0.0)]
    annual, quarterly = DataProcessor.process_financials("9999", statements(rows))
    q = quarterly[0]
    assert (q["grossProfit"], q["grossMargin"], q["netIncome"], q["netMargin"], q["eps"]) == (0, 0.0, 0, 0.0, 0.0)
    assert (annual[0]["grossProfit"], annual[0]["grossMargin"], annual[0]["eps"]) == (0, 0.0, 0.0)


def test_one_quarter_missing_makes_annual_none_not_partial_sum():
    # 三季有毛利、一季沒有：全年毛利不能是三季的和
    rows = [r for d in QUARTER_ENDS[:3] for r in normal_quarter(d)]
    rows += [r for r in normal_quarter(QUARTER_ENDS[3]) if r[1] != "營業毛利（毛損）"]
    annual, quarterly = DataProcessor.process_financials("1234", statements(rows))
    assert quarterly[0]["grossProfit"] is None
    assert annual[0]["grossProfit"] is None and annual[0]["grossMargin"] is None
    assert annual[0]["operatingIncome"] == 1200 and annual[0]["operatingMargin"] == 30.0


def test_zero_revenue_margin_is_none():
    # 營收 0 時利潤率算不出來（不是 0%）；營收本身是真的 0，保留
    rows = normal_quarter(QUARTER_ENDS[0], revenue=0.0, gross=0.0, op=-50.0, net=-40.0, eps=-0.1)
    _, quarterly = DataProcessor.process_financials("5555", statements(rows))
    q = quarterly[0]
    assert q["revenue"] == 0 and q["grossMargin"] is None and q["operatingMargin"] is None and q["netMargin"] is None


def test_half_year_reporter_full_years_are_not_partial():
    # 只申報半年報：FinMind 一年只有 06-30（上半年）與 12-31（下半年）。以前一律標「累季」
    rows = []
    for d, rev in [("2024-06-30", 1000.0), ("2024-12-31", 1100.0), ("2025-06-30", 1200.0), ("2025-12-31", 1300.0),
                   ("2026-06-30", 1500.0)]:
        rows += normal_quarter(d, revenue=rev, gross=rev / 2, op=rev / 5, net=rev / 10, eps=1.0)
    annual, _ = DataProcessor.process_financials("1260", statements(rows))
    by_year = {a["year"]: a for a in annual}
    assert "note" not in by_year[2024] and "note" not in by_year[2025]
    assert by_year[2025]["revenue"] == 2500 and by_year[2025]["eps"] == 2.0
    assert by_year[2025]["revenueYoY"] == round((2500 - 2100) / 2100 * 100, 2)   # 整年比整年
    # 今年只有上半年：照樣是累季，年增率拿去年上半年比
    assert by_year[2026]["note"] == "累季"
    assert by_year[2026]["revenueYoY"] == 25.0


def test_quarterly_reporter_partial_year_still_partial():
    rows = [r for d in QUARTER_ENDS for r in normal_quarter(d)] + normal_quarter("2026-03-31") + normal_quarter("2026-06-30")
    annual, _ = DataProcessor.process_financials("2330", statements(rows))
    by_year = {a["year"]: a for a in annual}
    assert "note" not in by_year[2025] and by_year[2026]["note"] == "累季"
    # 只有 Q2、Q3 這種缺頭的年度不是半年報格式，仍是累季
    rows = normal_quarter("2025-06-30") + normal_quarter("2025-09-30")
    annual, _ = DataProcessor.process_financials("1234", statements(rows))
    assert annual[0]["note"] == "累季"


def test_financial_company_balance_sheet_names_are_recognised():
    # 富邦金 2025-12-31 的 FinMind 原文科目（金額列＋百分比列）。以前對不上，ROE／ROA／負債比全是 None
    d = "2025-12-31"
    bs = pd.DataFrame([{"date": d, "origin_name": n, "value": v} for n, v in [
        ("資產總計", 12875478154000.0), ("資產總計", 100.0),
        ("負債總", 11877670227000.0), ("負債總", 92.25),
        ("歸屬於母公司業主之權益", 982581684000.0), ("歸屬於母公司業主之權益", 7.63),
        ("權益總計", 997807927000.0), ("權益總計", 7.75),
        ("負債及權益總計", 12875478154000.0),
    ]])
    snap = DataProcessor.process_balance_sheet("2881", bs)[(2025, 4)]
    assert snap["totalAssets"] == 12875478154000.0
    assert snap["totalLiabilities"] == 11877670227000.0
    # 與一般公司同規則：同一項目多個名稱取金額最大者（權益總計，含非控制權益）
    assert snap["equity"] == 997807927000.0
