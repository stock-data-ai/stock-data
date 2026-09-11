"""壞檔自我修復（D7）、美股累積（D8）、0/null 不混同（F-12）。

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


# ── D7：財報更新看到壞檔會用完整視窗把歷史重建回來 ────────────────────

class _Fetcher:
    """記下實際被要求的 start_date，並回放固定資料。"""

    def __init__(self, result):
        self.result, self.seen = result, []

    def fetch_and_process(self, code, start_date):
        self.seen.append(start_date)
        return self.result


def _processor(file_mgr, quarters, months, details=None):
    from finance_tools.orchestration.company_processor import CompanyProcessor
    return CompanyProcessor(
        processor=DataProcessor(), file_mgr=file_mgr, finmind_client=object(),
        financials_fetcher=_Fetcher(([], quarters, True)),
        revenue_fetcher=_Fetcher((months, True)),
        all_companies_details=details or {},
        institutional_investors_shares_fetcher=lambda *a: (pd.DataFrame(), False),
    )


def _q(year, quarter):
    return {"year": year, "quarter": quarter, "revenue": 100.0, "grossProfit": 50.0,
            "operatingIncome": 30.0, "netIncome": 20.0, "eps": 1.0,
            "grossMargin": 50.0, "operatingMargin": 30.0, "netMargin": 20.0}


def test_corrupt_file_triggers_full_window_rebuild(isolated_file_manager):
    """壞檔 → 這一輪就用完整視窗重建，不是拿一年份蓋上去。"""
    from datetime import timedelta
    import finance_tools.config as config
    from finance_tools.core.timezone import now_tw

    corrupt(isolated_file_manager)
    quarters = [_q(2019 + i // 4, i % 4 + 1) for i in range(28)]
    proc = _processor(isolated_file_manager, quarters, [])
    one_year = (now_tw() - timedelta(days=config.FULL_UPDATE_DAYS)).strftime("%Y-%m-%d")

    ok, status = proc.process_company("9999", "測試公司", one_year, force_update=True)

    assert ok and status["rebuilt"] is True
    asked = proc.fetch_orchestrator.financials_fetcher.seen[0]
    assert asked < one_year, f"必須放寬視窗；實際用了 {asked}"
    expected = (now_tw() - timedelta(days=config.FULL_HISTORY_DAYS)).strftime("%Y-%m-%d")
    assert asked == expected

    rebuilt = isolated_file_manager.load_financial_data("9999")
    assert len(rebuilt["historical"]["quarterly"]) == 28, "歷史一次長回來"


def test_healthy_file_keeps_the_normal_window(isolated_file_manager):
    """沒壞就不要浪費 API 配額——視窗維持呼叫端給的那個。"""
    isolated_file_manager.save_financial_data("9999", json.loads(json.dumps(GOOD)))
    proc = _processor(isolated_file_manager, [_q(2026, 1)], [])
    ok, status = proc.process_company("9999", "測試公司", "2025-09-10", force_update=True)
    assert ok and status["rebuilt"] is False
    assert proc.fetch_orchestrator.financials_fetcher.seen == ["2025-09-10"]


def test_daily_update_backs_off_on_corrupt_file(isolated_file_manager):
    """日更只有市值／法人這種片段，看到壞檔必須退開，不得建一份沒有歷史的新檔。"""
    import os
    path = corrupt(isolated_file_manager)
    # 要有發行股數，市值才算得出來，才會真的走到「載入既有檔」那一步
    details = {"9999": {"gov": {"capital": {"issuedCommonShares": 1000}}}}
    proc = _processor(isolated_file_manager, [], [], details)

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
