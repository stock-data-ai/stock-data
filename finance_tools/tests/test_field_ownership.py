"""階段 1：財報檔的欄位歸屬與保留政策 —— **只記錄現況，不改任何行為**。

`company-financials/{code}.json` 由 11 支程式各自 load → 改 → save，
沒有一個地方寫著「哪個欄位歸誰管」。這支測試把現況變成可執行的事實：

  跑真正的寫入者 → 比對它實際動到哪些 key path → 釘住

未來有人讓某支寫入者碰了不歸它管的欄位，這裡就會紅。
文件版對照表在 stock_map `docs/features/platform/財報檔欄位歸屬.md`。

⚠️ 這裡釘住的**包含目前互相矛盾的規則**（見 `test_monthly_revenue_*`）。
釘住不代表認可，只代表「改動時要有意識」。政策選擇見該文件的未決清單。
"""

import json

import pandas as pd
import pytest

from finance_tools.core.data_processor import DataProcessor
from finance_tools.orchestration.data_assembler import DataAssembler


# ── 觀測工具 ──────────────────────────────────────────────────────────────

def key_paths(obj, prefix=""):
    """把巢狀 dict 攤平成 key path 集合；list 視為葉節點（整批取代才有意義）。"""
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            out.add(path)
            out |= key_paths(v, path)
    return out


def changed_paths(before, after):
    """回傳「值真的變了」的 key path（新增、刪除、改值都算）。"""
    paths = key_paths(before) | key_paths(after)
    changed = set()
    for p in paths:
        a, b = _dig(before, p), _dig(after, p)
        if a != b:
            changed.add(p)
    return changed


_MISSING = object()


def _dig(obj, path):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


SEED = {
    "companyCode": "9999",
    "companyName": "測試公司",
    "latest": {"marketCap": 111, "pe": 22.2, "marginBalance": 5, "sblBalance": 7},
    "historical": {
        "annual": [{"year": 2025, "netIncome": 60.0, "eps": 4.0,
                    "roe": 10.0, "roa": 6.0, "ocf": 300, "fcf": 200, "debtRatio": 40.0}],
        "quarterly": [{"year": 2025, "quarter": 4, "revenue": 100.0, "eps": 1.0,
                       "currentRatio": 250.0, "debtRatio": 40.0}],
        "monthlyRevenue": [{"year": 2025, "month": 12, "revenue": 9.0, "yoy": None}],
        "dividends": [{"year": 2024, "cashDividend": 3.0}],
        "institutionalInvestors": {"2025-12-01": {"foreign_ratio": 50.0}},
        "marginTrading": {"2025-12-01": {"marginBalance": 5}},
        "securitiesLending": {"2025-12-01": {"sblBalance": 7}},
    },
    "insiderHoldingsRecent": {"month": "202512", "insiders": [], "totals": {}, "boardTotals": {}},
    "insiderHoldingsHistory": {"202512": {"totals": {}, "boardTotals": {}}},
    "shareholderDataRecent": [{"level": 15}],
    "shareholderDataHistory": {"20251201": [{"level": 15}]},
    "lastUpdated": "2026-01-01",
    "dataQuality": "high",
}


@pytest.fixture
def seeded(isolated_file_manager):
    isolated_file_manager.save_financial_data("9999", json.loads(json.dumps(SEED)))
    return isolated_file_manager


def run_and_diff(file_mgr, writer):
    """跑一支寫入者，回傳它實際動到的 key path。"""
    before = file_mgr.load_financial_data("9999")
    writer(file_mgr)
    after = file_mgr.load_financial_data("9999")
    return changed_paths(before, after), after


# `lastUpdated` 幾乎每支都會寫，單獨列出以免淹沒真正的歸屬差異。
STAMP = {"lastUpdated"}


# ── 歸屬：每支寫入者實際碰到的欄位 ────────────────────────────────────────

def test_ownership_balance_sheet_task(seeded):
    """`update-balance-sheet` 只該碰 annual 的健全度指標與 quarterly 的季度快照。"""
    from finance_tools.domains.balance_sheet.tasks import _update_one

    bs = pd.DataFrame([
        {"date": "2025-12-30", "origin_name": n, "value": v}
        for n, v in [("資產總額", 2000.0), ("負債總額", 500.0), ("權益總額", 1500.0),
                     ("流動資產合計", 800.0), ("流動負債合計", 200.0)]
    ])
    cf = pd.DataFrame([
        {"date": "2025-12-31", "origin_name": "營業活動之淨現金流入（流出）", "value": 900.0},
        {"date": "2025-12-31", "origin_name": "取得不動產、廠房及設備", "value": -100.0},
    ])

    class Client:
        def fetch_balance_sheet(self, code, start): return (bs, True)
        def fetch_cash_flows_statement(self, code, start): return (cf, True)

    fm = seeded
    changed, after = run_and_diff(
        fm, lambda m: _update_one(Client(), DataProcessor(), m, "9999", "測試公司", "2019-01-01"))

    assert changed <= {"historical", "historical.annual", "historical.quarterly"}, \
        f"資產負債表任務碰到不歸它管的欄位: {sorted(changed)}"
    # 沒動到的鄰居仍原封不動
    assert after["historical"]["monthlyRevenue"] == SEED["historical"]["monthlyRevenue"]
    assert after["historical"]["dividends"] == SEED["historical"]["dividends"]
    assert after["insiderHoldingsRecent"] == SEED["insiderHoldingsRecent"]
    assert after["latest"] == SEED["latest"]


def test_ownership_margin_trading_task(seeded):
    """`update-margin` 只該碰 historical.marginTrading 與 latest 的兩個餘額。"""
    from finance_tools.domains.margin_trading.tasks import _process_one_date

    df = pd.DataFrame([{
        "stock_id": "9999", "margin_buy": 1, "margin_sell": 2, "margin_balance": 33,
        "short_buy": 4, "short_sell": 5, "short_balance": 66,
    }])

    class Fetcher:
        def fetch_all(self, date): return df

    changed, after = run_and_diff(
        seeded, lambda m: _process_one_date("20251202", Fetcher(), DataProcessor(), m, {"9999"}))

    allowed = ("latest", "latest.marginBalance", "latest.shortBalance",
               "historical", "historical.marginTrading",
               "historical.marginTrading.2025-12-02")
    assert all(p in allowed or p.startswith("historical.marginTrading.2025-12-02.")
               for p in changed), sorted(changed)
    assert {"latest.marginBalance", "latest.shortBalance"} <= changed
    assert not any(p.startswith("latest.") for p in changed
                   if p not in ("latest.marginBalance", "latest.shortBalance")), sorted(changed)
    assert "lastUpdated" not in changed, "現況：融資融券寫入不更新 lastUpdated"
    assert after["historical"]["marginTrading"]["2025-12-01"] == {"marginBalance": 5}, "舊日期保留"


def test_ownership_securities_lending_task(seeded):
    """`update-lending` 只該碰 historical.securitiesLending 與 latest.sblBalance。"""
    from finance_tools.domains.securities_lending.tasks import _process_one_date

    df = pd.DataFrame([{
        "stock_id": "9999", "sbl_balance": 1000.0, "sbl_short_sales": 2000.0,
        "sbl_returns": 3000.0, "sbl_adjustments": 4000.0, "sbl_available": 5000.0,
    }])

    class Fetcher:
        def fetch_all(self, date): return df

    changed, after = run_and_diff(
        seeded, lambda m: _process_one_date("20251202", Fetcher(), DataProcessor(), m, {"9999"}))

    allowed = ("latest", "latest.sblBalance", "historical",
               "historical.securitiesLending", "historical.securitiesLending.2025-12-02")
    assert all(p in allowed or p.startswith("historical.securitiesLending.2025-12-02.")
               for p in changed), sorted(changed)
    assert "latest.sblBalance" in changed
    assert after["historical"]["securitiesLending"]["2025-12-01"] == {"sblBalance": 7}, "舊日期保留"


def test_ownership_insider_merge():
    """內部人持股只碰兩個 top-level 欄位（明細只留當期、歷史只留合計）。"""
    before = json.loads(json.dumps(SEED))
    after = DataAssembler.merge_insider_holdings(
        json.loads(json.dumps(SEED)),
        {"month": "202601", "insiders": [{"name": "A"}], "totals": {"x": 1}, "boardTotals": {"y": 2}})
    changed = changed_paths(before, after)
    tops = {p.split(".")[0] for p in changed}
    assert tops <= {"insiderHoldingsRecent", "insiderHoldingsHistory", "lastUpdated"}, sorted(changed)
    assert "202512" in after["insiderHoldingsHistory"], "舊月份合計保留"
    assert after["insiderHoldingsRecent"]["month"] == "202601", "明細只留當期"


def test_ownership_valuation_merge():
    """估值只碰 latest 的四個數字，不碰 historical。"""
    before = json.loads(json.dumps(SEED))
    after = DataAssembler.merge_valuation(json.loads(json.dumps(SEED)),
                                          {"marketCap": 999, "trailingPE": 8.0,
                                           "priceToBook": 1.5, "dividendYield": 0.02})
    changed = changed_paths(before, after)
    assert changed == {"latest", "latest.marketCap", "latest.pe", "latest.pb",
                       "latest.dividendYield", "lastUpdated"}, sorted(changed)
    assert after["latest"]["marginBalance"] == 5, "不歸它管的 latest 欄位保留"


def test_ownership_financials_update(seeded):
    """週日財報更新碰的範圍最大：latest 的損益欄位 + historical 五個集合 + 品質戳記。

    **不該**碰 marginTrading／securitiesLending／內部人／大戶。"""
    def writer(m):
        existing = m.load_financial_data("9999")
        out = DataAssembler.build_final_data(
            existing, "9999", "測試公司", {"revenue": 1.0}, [],
            [{"year": 2026, "quarter": 1, "revenue": 5.0, "grossProfit": 1.0,
              "operatingIncome": 1.0, "netIncome": 1.0, "eps": 0.5,
              "grossMargin": 20.0, "operatingMargin": 20.0, "netMargin": 20.0}],
            [], [], "high", {})
        m.save_financial_data("9999", DataProcessor().clean_nan(out))

    changed, after = run_and_diff(seeded, writer)
    tops = {p.split(".")[0] for p in changed}
    assert tops <= {"latest", "historical", "lastUpdated", "dataQuality"}, sorted(changed)
    touched_hist = {p.split(".")[1] for p in changed if p.startswith("historical.")}
    assert touched_hist <= {"annual", "quarterly", "monthlyRevenue", "dividends",
                            "institutionalInvestors"}, sorted(touched_hist)
    assert after["historical"]["marginTrading"] == SEED["historical"]["marginTrading"]
    assert after["historical"]["securitiesLending"] == SEED["historical"]["securitiesLending"]
    assert after["shareholderDataHistory"] == SEED["shareholderDataHistory"]


# ── 保留政策：目前散落且互相矛盾的規則 ────────────────────────────────────

def test_monthly_revenue_policy_in_financials_update():
    """週日財報更新：以 (年,月) **合併**，上限 **72** 筆。"""
    old = [{"year": 2020 + i // 12, "month": i % 12 + 1, "revenue": float(i), "yoy": None}
           for i in range(80)]
    out = DataAssembler.build_final_data(
        {"historical": {"quarterly": [], "annual": [], "monthlyRevenue": old}},
        "9999", "測試公司", {}, [], [], [{"year": 2027, "month": 1, "revenue": 1.0, "yoy": None}],
        [], "high", {})
    assert len(out["historical"]["monthlyRevenue"]) == 72


def test_monthly_revenue_policy_is_shared_by_both_writers(isolated_file_manager, monkeypatch, tmp_path):
    """`update-revenue` 與週日財報走**同一條**合併規則（`merge_policy`）。

    2026-09-10 之前不是這樣：它是「整批取代、上限 36」，而抓取視窗只有
    `REVENUE_DAYS = 365`（約 13 個月），跑一次就把既有的 72 個月砍成 13 個月，
    而且無聲。stock-data `CLAUDE.md` 還把它列為「for quick testing」的範例指令。
    現在兩邊都用 `merge_by_key` + `MONTHLY_REVENUE_LIMIT`，視窗外的舊月份保留。
    """
    import finance_tools.domains.revenue.tasks as rev
    import finance_tools.utils.rerun_manager as rm

    monkeypatch.setattr(rm, "RERUN_DIR", str(tmp_path))

    old = [{"year": 2020 + i // 12, "month": i % 12 + 1, "revenue": float(i), "yoy": None}
           for i in range(80)]
    isolated_file_manager.save_financial_data(
        "9999", {"companyCode": "9999", "historical": {"monthlyRevenue": old}})

    fetched = [{"year": 2027, "month": m, "revenue": 1.0, "yoy": None} for m in range(1, 14)]

    class FakeClient:
        def check_api_usage(self): return (0, 10000)

    class FakeFetcher:
        def __init__(self, *a, **k): pass
        def fetch_and_process(self, code, start_date): return fetched, True

    monkeypatch.setattr(rev, "FinMindClient", lambda *a, **k: FakeClient())
    monkeypatch.setattr(rev, "RevenueFetcher", FakeFetcher)
    monkeypatch.setattr(rev, "load_companies_for_processing", lambda *a, **k: [{"code": "9999", "name": "測試公司"}])
    monkeypatch.setattr(rev, "filter_already_updated", lambda companies, *a, **k: companies)
    monkeypatch.setattr(rev.time, "sleep", lambda *a: None)

    rev.run_update_revenue(type("Args", (), {"force": True, "batch": None})())

    kept = isolated_file_manager.load_financial_data("9999")["historical"]["monthlyRevenue"]
    from finance_tools.core import merge_policy
    assert len(kept) == merge_policy.MONTHLY_REVENUE_LIMIT, "合併後受同一個上限約束"
    assert kept[0] == {"year": 2027, "month": 13 - 1 + 1, "revenue": 1.0, "yoy": None} or kept[0]["year"] == 2027
    assert any(m["year"] < 2027 for m in kept), "視窗外的舊月份必須保留下來"


def test_date_keyed_histories_accumulate_and_are_only_bounded_by_archiving(seeded):
    """以日期為 key 的歷史在**寫入路徑**不做任何修剪——只會一直累積。

    收斂統一交給 `archive-history`：主檔留當年＋前一年，更舊的按整年**搬進**封存。
    這裡釘住的是「寫入者不得自己刪」：曾經有第二套規則（寫入時按筆數砍掉舊紀錄），
    造成同一類資料一半封存、一半被刪，2026-09-11 移除。
    """
    from finance_tools.core import merge_policy
    from finance_tools.domains.margin_trading.tasks import _process_one_date

    assert not hasattr(merge_policy, "trim_date_map"), \
        "寫入路徑不該再有按筆數刪除的第二套規則"
    assert not hasattr(merge_policy, "DAILY_HISTORY_LIMIT")

    class Fetcher:
        def fetch_all(self, d):
            return pd.DataFrame([{"stock_id": "9999", "margin_buy": 1, "margin_sell": 2,
                                  "margin_balance": 3, "short_buy": 4, "short_sell": 5,
                                  "short_balance": 6}])

    for day in ("20251202", "20251203", "20251204"):
        _process_one_date(day, Fetcher(), DataProcessor(), seeded, {"9999"})

    hist = seeded.load_financial_data("9999")["historical"]["marginTrading"]
    assert len(hist) == 4, "照常累積（1 筆種子 + 3 天），不自行刪除"
    assert "2025-12-01" in hist, "舊日期必須留著，收斂是封存的事"
def test_no_duplicate_merge_helper_for_margin_trading():
    """`DataAssembler.merge_margin_trading` 曾經是**沒有呼叫者的死碼**，
    而且與正式路徑（`margin_trading/tasks.py` 內嵌邏輯）語意不同：
    死碼版日期預設 `today_str()`、缺欄位補 0、會更新 `lastUpdated`，正式版都不是。
    2026-09-10 已刪除。這條護欄擋它再長回來——同一個欄位不要有兩份實作。"""
    assert not hasattr(DataAssembler, "merge_margin_trading"), (
        "merge_margin_trading 又出現了；融資融券的寫入只應該有 "
        "margin_trading/tasks.py 一份實作"
    )


def test_isolation_is_fail_closed_without_asking_for_the_fixture(tmp_path):
    """護欄：**沒有**取用 `isolated_file_manager` 也不能寫到正式資料。

    `config.py` 的路徑是相對的，在 repo root 跑測試時，任何自己 `FileManager()`
    的測試都會直接寫進 `src/data/layer3/company-financials/`。
    2026-09-10 本測試檔就發生過一次（留下 `9999.json`），修法是把路徑隔離
    改成 conftest 的 autouse fixture。這條測試確保它不會退回 opt-in。
    """
    from finance_tools.core.file_manager import FileManager

    fm = FileManager()
    assert str(tmp_path) in fm.financials_dir, (
        f"路徑隔離失效，FileManager 指向 {fm.financials_dir}——"
        "conftest 的 _isolate_data_paths 必須是 autouse"
    )
