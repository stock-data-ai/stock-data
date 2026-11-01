"""壞檔自我修復（D7）、美股累積（D8）、美股匯率（D9）、0/null 不混同（F-12）。

設計前提：**這條路徑上沒有人會來看 CI 紅燈**，所以任何修復都必須全自動、
不得依賴人工步驟（使用者 2026-09-10 裁示）。測試因此驗的是「自己會好」，
而不是「有沒有正確地報錯」。
"""

import json

import pandas as pd
import pytest

from finance_tools.core import merge_policy
from finance_tools.core.data_processor import DataProcessor


GOOD = {
    "companyCode": "9999",
    "companyName": "測試公司",
    "latest": {"marketCap": 111},
    "historical": {
        "annual": [{"year": 2019, "eps": 1.0, "roe": 5.0}],
        "quarterly": [{"year": 2019, "quarter": 1, "revenue": 100.0, "eps": 1.0}],
        "monthlyRevenue": [{"year": 2019, "month": 1, "revenue": 5.0, "yoy": None}],
    },
    "lastUpdated": "2026-01-01",
}


def corrupt(file_mgr, code="9999"):
    """把檔案弄壞（模擬磁碟／傳輸損壞），回傳原始 bytes。"""
    import os
    path = os.path.join(file_mgr.financials_dir, f"{code}.json")
    file_mgr.save_financial_data(code, GOOD)
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"companyCode": "9999", "histor')
    return path


# ── D7：載入端的三態 ──────────────────────────────────────────────────

def test_missing_file_and_corrupt_file_are_different_signals(isolated_file_manager):
    """`{}`＝新公司可以建檔；`None`＝歷史讀不回來，任何人都不准覆寫。"""
    assert isolated_file_manager.load_financial_data("0000") == {}, "不存在 → {}"
    corrupt(isolated_file_manager)
    assert isolated_file_manager.load_financial_data("9999") is None, "損壞 → None"


def test_corrupt_file_stays_put_so_the_signal_does_not_vanish(isolated_file_manager):
    """壞檔**留在原地**，而且每一輪讀都要繼續回 `None`。

    這條是回歸測試。原本的寫法是把壞檔改名成 `.corrupt` 保存，看起來比較「乾淨」，
    實際上是個洞：下一輪 `load` 看到「檔案不存在」就回 `{}`＝新公司，
    於是日更的守門失效、建出一份只有市值的空殼檔；空殼是合法 JSON，
    週日財報更新讀得回來，**完整視窗重建就永遠不會觸發**，歷史照樣沒。

    訊號必須留著，直到真正被修好為止。
    """
    import os
    path = corrupt(isolated_file_manager)

    for round_no in (1, 2, 3):
        assert isolated_file_manager.load_financial_data("9999") is None, \
            f"第 {round_no} 輪就不回 None 了，守門會失效"
        assert os.path.exists(path), "壞檔必須留在原地"

    with open(path, encoding="utf-8") as f:
        assert f.read() == '{"companyCode": "9999", "histor', "原始位元組原封不動"

    assert not os.path.exists(path + ".corrupt"), "不再產生 .corrupt 旁檔"


def test_atomic_write_temp_files_are_gitignored():
    """`.tmp` 不進版控——CI 的 `git add company-financials/` 是整個目錄。"""
    from pathlib import Path
    ignore = Path(__file__).resolve().parents[2] / ".gitignore"
    assert "*.json.tmp" in ignore.read_text()


# ── D7：財報更新每輪都是完整視窗，壞檔／空殼這一輪就重建回來 ──────────────

def _processor(file_mgr, details=None):
    from finance_tools.orchestration.company_processor import CompanyProcessor
    return CompanyProcessor(processor=DataProcessor(), file_mgr=file_mgr,
                            all_companies_details=details or {})


_QUARTER_END = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}


def _statements(quarters):
    """全市場抓取後分到 9999 的損益表列（FinMind 原始格式）。"""
    rows = []
    for year, q in quarters:
        d = f"{year}-{_QUARTER_END[q]}"
        for name, value in (("營業收入合計", 100.0), ("營業毛利（毛損）", 50.0), ("營業利益（損失）", 30.0),
                            ("本期淨利（淨損）", 20.0), ("基本每股盈餘（元）", 1.0),
                            ("稅前淨利（淨損）", 25.0), ("所得稅費用（利益）", 5.0)):
            rows.append({"date": d, "stock_id": "9999", "type": "x", "origin_name": name, "value": value})
    return pd.DataFrame(rows)


def _revenue(months):
    return pd.DataFrame([{"date": f"{y}-{m:02d}-10", "stock_id": "9999", "revenue": 5.0,
                          "revenue_year": y, "revenue_month": m} for y, m in months])


_28Q = [(2019 + i // 4, i % 4 + 1) for i in range(28)]
_72M = [(2020 + i // 12, i % 12 + 1) for i in range(72)]


def test_corrupt_file_is_rebuilt_from_the_full_window(isolated_file_manager):
    """壞檔 → 這一輪拿到的就是完整視窗，歷史一次長回來。"""
    corrupt(isolated_file_manager)
    ok, status = _processor(isolated_file_manager).process_company(
        "9999", "測試公司", _statements(_28Q), _revenue(_72M))
    assert ok and status["fin"] and status["rev"]
    rebuilt = isolated_file_manager.load_financial_data("9999")["historical"]
    assert len(rebuilt["quarterly"]) == 28 and len(rebuilt["monthlyRevenue"]) == 72


def test_daily_shell_is_filled_in_one_round(isolated_file_manager):
    """日更比週日早碰到新公司，會先建一份只有市值的檔；下一次財報更新就補齊。"""
    isolated_file_manager.save_financial_data("9999", {"companyCode": "9999", "latest": {"marketCap": 1}})
    _processor(isolated_file_manager).process_company("9999", "測試公司", _statements(_28Q), _revenue(_72M))
    data = isolated_file_manager.load_financial_data("9999")
    assert data["latest"]["marketCap"] == 1, "市值是別的 writer 的欄位，必須保留"
    assert len(data["historical"]["quarterly"]) == 28


def test_company_missing_from_source_keeps_its_history(isolated_file_manager):
    """全市場資料裡沒有這家（空 DataFrame）→ 合併時保留既有歷史，不清空。"""
    isolated_file_manager.save_financial_data("9999", json.loads(json.dumps(GOOD)))
    ok, status = _processor(isolated_file_manager).process_company(
        "9999", "測試公司", pd.DataFrame(), pd.DataFrame())
    assert ok and not status["fin"] and not status["rev"]
    hist = isolated_file_manager.load_financial_data("9999")["historical"]
    assert hist["quarterly"] == GOOD["historical"]["quarterly"]
    assert hist["monthlyRevenue"] == GOOD["historical"]["monthlyRevenue"]


def test_tax_fields_come_with_every_quarter(isolated_file_manager):
    """完整視窗一次帶回稅前淨利／所得稅，不再需要「缺欄位就放寬」的特別判斷。"""
    _processor(isolated_file_manager).process_company("9999", "測試公司", _statements(_28Q), pd.DataFrame())
    quarterly = isolated_file_manager.load_financial_data("9999")["historical"]["quarterly"]
    assert all(q["preTaxIncome"] == 25.0 and q["incomeTax"] == 5.0 for q in quarterly)


def test_daily_update_backs_off_on_corrupt_file(isolated_file_manager):
    """日更只有市值／法人這種片段，看到壞檔必須退開，不得建一份沒有歷史的新檔。"""
    import os
    path = corrupt(isolated_file_manager)
    # 要有發行股數，市值才算得出來，才會真的走到「載入既有檔」那一步
    details = {"9999": {"gov": {"capital": {"issuedCommonShares": 1000}}}}
    proc = _processor(isolated_file_manager, details)

    class _Rec:
        close, pe, pb, dividend_yield = 10.0, 12.0, 1.1, 2.0

    ok, status = proc.process_daily_only("9999", "測試公司", "2026-01-01",
                                         force_update=True, pre_valuation={"9999": _Rec()})
    assert ok is True
    assert status["marketcap"] is True, "確認有走到載入既有檔那一步"
    with open(path, encoding="utf-8") as f:
        assert f.read() == '{"companyCode": "9999", "histor', \
            "日更不得覆寫壞檔——它是重抓的訊號"


def test_shareholder_job_backs_off_on_corrupt_file(isolated_file_manager, monkeypatch):
    """大戶排程看到壞檔要跳過，不得建一份只有大戶資料的骨架蓋上去。

    回歸測試：原本 `if not financial_data:` 把 None（壞檔）當成新公司，
    建骨架存檔——歷史沒了，壞檔訊號也跟著消失。旁邊的正常公司要照常寫入。
    """
    from types import SimpleNamespace
    from finance_tools.core.timezone import now_tw
    from finance_tools.domains.shareholder import tasks

    path = corrupt(isolated_file_manager)
    isolated_file_manager.save_financial_data("1101", {"companyCode": "1101", "historical": {}})

    today = now_tw().strftime("%Y%m%d")
    levels = [{"序": i, "holding_range": f"L{i}", "holder_count": 10, "shares": 100,
               "ratio_pct": 1.0, "data_date": today} for i in range(1, 16)]
    tdcc = {code: levels for code in ("9999", "1101")}

    monkeypatch.setattr(tasks, "FileManager", lambda: isolated_file_manager)
    monkeypatch.setattr(tasks, "fetch_all_tdcc_shareholding_via_api", lambda: tdcc)
    monkeypatch.setattr(tasks, "load_companies_for_processing", lambda *a: [
        {"code": "9999", "name": "壞檔"}, {"code": "1101", "name": "台泥"}])
    monkeypatch.setattr(tasks, "save_quality_report", lambda *a: None)

    tasks.run_fetch_shareholder_data(SimpleNamespace(force=True, code=None, rerun=None, batch=None))

    with open(path, encoding="utf-8") as f:
        assert f.read() == '{"companyCode": "9999", "histor', "壞檔必須原封不動"
    assert today in isolated_file_manager.load_financial_data("1101")["shareholderDataHistory"], \
        "正常公司照常寫入（確認真的有跑到寫檔那一步）"


# ── D8：美股改成累積 ──────────────────────────────────────────────────

def _us():
    from finance_tools.us_financials import fetch_us_financials as us
    return us


def test_us_merge_keeps_years_the_source_no_longer_returns():
    """yfinance 只回 4 年；比那更舊的年度必須留著。"""
    existing = {"companyCode": "NVDA", "historical": {
        "annual": [{"year": y, "revenue": y} for y in (2021, 2020, 2019)], "quarterly": []}}
    fresh = {"companyCode": "NVDA", "historical": {
        "annual": [{"year": y, "revenue": y * 10} for y in (2025, 2024, 2023, 2022)], "quarterly": []}}

    merged = _us().merge_us_output(existing, fresh)
    years = [a["year"] for a in merged["historical"]["annual"]]
    assert years == [2025, 2024, 2023, 2022, 2021, 2020, 2019]


def test_us_merge_partial_source_does_not_clear_quarterly():
    """來源只回半套（有 annual、沒 quarterly）時，既有的季度不得被清空。"""
    existing = {"historical": {"annual": [{"year": 2024, "revenue": 1}],
                               "quarterly": [{"year": 2024, "quarter": 4, "revenue": 9}]}}
    fresh = {"historical": {"annual": [{"year": 2025, "revenue": 2}], "quarterly": []}}
    merged = _us().merge_us_output(existing, fresh)
    assert merged["historical"]["quarterly"] == [{"year": 2024, "quarter": 4, "revenue": 9}]


def test_us_merge_same_period_takes_the_new_value():
    existing = {"historical": {"annual": [{"year": 2025, "revenue": 1}], "quarterly": []}}
    fresh = {"historical": {"annual": [{"year": 2025, "revenue": 999}], "quarterly": []}}
    merged = _us().merge_us_output(existing, fresh)
    assert merged["historical"]["annual"] == [{"year": 2025, "revenue": 999}]


# ── 期別改用會計年度（2026-10-12）────────────────────────────────────

def _ts(s):
    import pandas as pd
    return pd.Timestamp(s)


@pytest.mark.parametrize("period_end, fye_month, want", [
    ("2026-07-31", 1, (2027, 2)),   # NVDA：FY 在 1 月結束，2026/7 那季是 FY27 Q2
    ("2026-01-31", 1, (2026, 4)),   # NVDA 年度最後一季
    ("2026-06-30", 9, (2026, 3)),   # AAPL：FY 在 9 月結束
    ("2025-09-30", 9, (2025, 4)),
    ("2026-03-31", 12, (2026, 1)),  # 曆年制不受影響
    ("2026-12-31", 12, (2026, 4)),
    ("2026-06-30", 6, (2026, 4)),   # MSFT
    ("2026-08-02", 10, (2026, 3)),  # AVGO：52／53 週制落在月初，算前一個月
])
def test_us_fiscal_period(period_end, fye_month, want):
    assert _us().fiscal_period(_ts(period_end), fye_month) == want


def test_us_fiscal_year_end_month_handles_53_week_years():
    import datetime as dt
    # AVGO 的 FY2025 在 2025-11-02 結束，實際是 10 月底那一年
    ts = int(dt.datetime(2025, 11, 2, tzinfo=dt.timezone.utc).timestamp())
    assert _us().fiscal_year_end_month({"lastFiscalYearEnd": ts}) == 10
    assert _us().fiscal_year_end_month({}) == 12


def test_us_quarterly_yoy_compares_same_fiscal_quarter():
    rows = [{"year": 2027, "quarter": 2, "native": {"revenue": 150}},
            {"year": 2027, "quarter": 1, "native": {"revenue": 140}},
            {"year": 2026, "quarter": 2, "native": {"revenue": 100}}]
    _us().fill_quarterly_yoy(rows)
    assert rows[0]["revenueYoY"] == 50.0
    assert "revenueYoY" not in rows[1], "缺去年同季就不算，不填 0"


def test_us_merge_rebuilds_history_from_calendar_labelled_file():
    """舊檔是西元季（沒有 periodBasis）：不能跟會計季度併在一起，否則同一季會出現兩筆。"""
    existing = {"historical": {"annual": [{"year": 2025, "revenue": 1}],
                               "quarterly": [{"year": 2026, "quarter": 3, "revenue": 9}]}}
    fresh = {"periodBasis": "fiscal", "historical": {
        "annual": [{"year": 2026, "revenue": 2}],
        "quarterly": [{"year": 2027, "quarter": 2, "revenue": 9}]}}
    merged = _us().merge_us_output(existing, fresh)
    assert [(q["year"], q["quarter"]) for q in merged["historical"]["quarterly"]] == [(2027, 2)]
    assert [a["year"] for a in merged["historical"]["annual"]] == [2026]
    assert merged["periodBasis"] == "fiscal"


def test_us_merge_on_a_brand_new_ticker_is_just_the_fresh_data():
    fresh = {"companyCode": "NEW", "historical": {"annual": [], "quarterly": []}}
    assert _us().merge_us_output({}, fresh) == fresh


# ── F-12：0 不是缺值 ──────────────────────────────────────────────────

def test_zero_is_a_real_value_not_a_missing_one():
    first = _us()._first_present
    assert first(0, 5) == 0, "0 是有效值，不得往下一個來源掉"
    assert first(None, 5) == 5
    assert first(None, None) is None
    assert first(0.0, 1.0) == 0.0


def test_us_merge_refuses_to_mix_reporting_currencies():
    """報表幣別換了就不併——舊年度是用舊幣別換算的，併在一起看不出來。

    這是**累積合併**才有的風險：2026-09-10 之前是整檔覆寫，每次都只有一種幣別。
    """
    existing = {"financialCurrency": "EUR",
                "historical": {"annual": [{"year": 2020, "revenue": 100}], "quarterly": []}}
    fresh = {"financialCurrency": "USD",
             "historical": {"annual": [{"year": 2025, "revenue": 200}], "quarterly": []}}

    merged = _us().merge_us_output(existing, fresh)
    assert merged is fresh, "幣別不同時整份重建，不得把兩種幣別併在一起"


def test_us_merge_proceeds_when_currency_is_unchanged():
    existing = {"financialCurrency": "USD",
                "historical": {"annual": [{"year": 2020, "revenue": 100}], "quarterly": []}}
    fresh = {"financialCurrency": "USD",
             "historical": {"annual": [{"year": 2025, "revenue": 200}], "quarterly": []}}
    merged = _us().merge_us_output(existing, fresh)
    assert [a["year"] for a in merged["historical"]["annual"]] == [2025, 2020]


def test_us_merge_still_works_on_files_written_before_currency_was_recorded():
    """舊檔沒有 financialCurrency 欄位——不能因此就拒絕合併。"""
    existing = {"historical": {"annual": [{"year": 2020, "revenue": 100}], "quarterly": []}}
    fresh = {"financialCurrency": "USD",
             "historical": {"annual": [{"year": 2025, "revenue": 200}], "quarterly": []}}
    merged = _us().merge_us_output(existing, fresh)
    assert [a["year"] for a in merged["historical"]["annual"]] == [2025, 2020]


# ── D9：美股匯率——原幣留著，美元每次從原幣重算 ─────────────────────────

def _eur_year(year, native_revenue, fx):
    us = _us()
    return us.apply_fx([{"year": year, "revenue": None, "grossProfit": None,
                         "operatingIncome": None, "netIncome": None,
                         "native": {"revenue": native_revenue, "grossProfit": None,
                                    "operatingIncome": None, "netIncome": None}}], fx)[0]


def test_old_years_are_recomputed_with_the_current_rate():
    """營收每年都是 100 億歐元：掉出 yfinance 視窗的 2021 年不得停在舊匯率。

    回歸測試：累積合併之後，2021 年停在 1.05、其餘年度用 1.17，
    圖上 2021→2022 就出現 11% 的「成長」——全部是匯率造成的。
    """
    E = 10_000_000_000  # 100 億
    existing = {"financialCurrency": "EUR", "fxRateToUSD": 1.05,
                "historical": {"annual": [_eur_year(2021, E, 1.05)], "quarterly": []}}
    fresh = {"financialCurrency": "EUR", "fxRateToUSD": 1.17,
             "historical": {"annual": [_eur_year(y, E, 1.17) for y in (2025, 2024, 2023, 2022)],
                            "quarterly": []}}

    merged = _us().merge_us_output(existing, fresh)
    usd = {a["year"]: a["revenue"] for a in merged["historical"]["annual"]}

    assert len(set(usd.values())) == 1, f"所有年度要用同一個匯率，實際 {usd}"
    assert usd[2021] == round(E * 1.17)
    assert merged["historical"]["annual"][-1]["native"]["revenue"] == E, "原幣永遠不動"


def test_records_written_before_native_existed_are_left_as_is():
    """舊紀錄沒有原幣可以重算——原樣保留，不得清成 None。"""
    legacy = {"year": 2020, "revenue": 123}
    fresh = {"fxRateToUSD": 1.17, "historical": {"annual": [_eur_year(2025, 100, 1.17)], "quarterly": []}}
    merged = _us().merge_us_output({"historical": {"annual": [dict(legacy)], "quarterly": []}}, fresh)
    assert merged["historical"]["annual"][-1] == legacy


class _FakeTicker:
    """最小的 yfinance.Ticker 替身：兩年年報、一季季報。"""

    def __init__(self, currency, info_extra=None):
        import pandas as pd
        cols = [pd.Timestamp("2025-12-31"), pd.Timestamp("2024-12-31")]
        rows = ["Total Revenue", "Gross Profit", "Operating Income", "Net Income", "Diluted EPS"]
        self.income_stmt = pd.DataFrame(
            [[200.0, 100.0], [80.0, 40.0], [40.0, 20.0], [20.0, 10.0], [2.0, 1.0]],
            index=rows, columns=cols)
        self.quarterly_income_stmt = self.income_stmt[[cols[0]]].rename(
            columns={cols[0]: pd.Timestamp("2025-09-30")})
        self.info = {"financialCurrency": currency, "longName": "Test Co", **(info_extra or {})}


def test_build_output_keeps_native_and_converts_to_usd(monkeypatch):
    us = _us()
    monkeypatch.setattr(us, "_fx_cache", {"JPY": 0.0067})
    result, _ = us.build_output("SONY", _FakeTicker("JPY"))
    a25 = result["historical"]["annual"][0]
    assert a25["native"]["revenue"] == 200 and a25["revenue"] == round(200 * 0.0067)
    assert a25["revenueYoY"] == 100.0, "年增率用原幣算，與匯率無關"
    assert a25["eps"] == 2.0, "EPS 維持原幣"
    assert result["fxRateToUSD"] == 0.0067 and result["financialCurrency"] == "JPY"
    assert result["latest"]["revenue"] == round(200 * 0.0067)


def test_missing_fx_rate_skips_the_company_instead_of_using_1(monkeypatch):
    """匯率取不到不得當成 1——日圓會被當成美元，錯 150 倍，而且畫面上看不出來。"""
    us = _us()
    monkeypatch.setattr(us, "_fx_cache", {})

    class _NoRate:
        info = {}
    monkeypatch.setattr(us.yf, "Ticker", lambda symbol: _NoRate())

    with pytest.raises(RuntimeError, match="匯率取不到"):
        us.build_output("SONY", _FakeTicker("JPY"))


def test_usd_reporter_never_asks_for_a_rate(monkeypatch):
    us = _us()
    monkeypatch.setattr(us.yf, "Ticker", lambda symbol: (_ for _ in ()).throw(AssertionError("不該查匯率")))
    result, _ = us.build_output("NVDA", _FakeTicker("USD"))
    assert result["historical"]["annual"][0]["revenue"] == 200


def test_oldest_fresh_year_gets_its_yoy_once_the_previous_year_is_on_file():
    """yfinance 給 2022–2025；2021 已經累積在檔裡 → 2022 的年增率補得出來。"""
    existing = {"fxRateToUSD": 1.0,
                "historical": {"annual": [_eur_year(2021, 100, 1.0)], "quarterly": []}}
    fresh = {"fxRateToUSD": 1.0, "historical": {"annual": [
        _eur_year(2023, 150, 1.0), _eur_year(2022, 120, 1.0)], "quarterly": []}}
    merged = _us().merge_us_output(existing, fresh)
    yoy = {a["year"]: a.get("revenueYoY") for a in merged["historical"]["annual"]}
    assert yoy == {2023: 25.0, 2022: 20.0, 2021: None}


def test_yoy_is_not_computed_across_a_missing_year():
    rows = [_eur_year(2025, 200, 1.0), _eur_year(2023, 100, 1.0)]
    assert [r.get("revenueYoY") for r in _us().fill_revenue_yoy(rows)] == [None, None]
