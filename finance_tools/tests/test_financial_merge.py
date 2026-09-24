"""WS-07 財報管線 characterization / regression。

跑的是**正式的 merge 與寫檔路徑**（`DataAssembler.build_final_data`、
`balance_sheet.tasks._update_one`、`FileManager.save_financial_data`），
只有 FinMind 的兩支網路呼叫被替換成回放 fixture 的假 client；
`DataProcessor.process_balance_sheet` / `process_cash_flows` 仍是真的。

離線保護與資料隔離見 `conftest.py`。
"""

import json

import pandas as pd
import pytest

from finance_tools.core.data_processor import DataProcessor
from finance_tools.domains.balance_sheet.tasks import _update_one
from finance_tools.orchestration.data_assembler import DataAssembler


# ── fixtures ──────────────────────────────────────────────────────────────

def quarter(year, q, revenue=100.0, **extra):
    """一筆損益表季度，欄位與 `DataProcessor.process_financials` 的產出一致。"""
    row = {
        "year": year, "quarter": q, "revenue": revenue, "grossProfit": 50.0,
        "operatingIncome": 30.0, "netIncome": 20.0, "eps": 1.0,
        "grossMargin": 50.0, "operatingMargin": 30.0, "netMargin": 20.0,
    }
    row.update(extra)
    return row


def build(existing, quarterly=(), monthly=(), dividends=(), quality="high", inst=None):
    """呼叫正式 merge，回傳 historical 區塊。"""
    out = DataAssembler.build_final_data(
        existing, "9999", "測試公司", {}, [], list(quarterly),
        list(monthly), list(dividends), quality, inst or {},
    )
    return out["historical"]


def periods(rows):
    return [(r["year"], r["quarter"]) for r in rows]


def _bs_rows(year, q):
    month = {1: 3, 2: 6, 3: 9, 4: 12}[q]
    date = f"{year}-{month:02d}-30"
    return [
        {"date": date, "origin_name": "資產總額", "value": 1000.0},
        {"date": date, "origin_name": "負債總額", "value": 400.0},
        {"date": date, "origin_name": "權益總額", "value": 600.0},
        {"date": date, "origin_name": "流動資產合計", "value": 500.0},
        {"date": date, "origin_name": "流動負債合計", "value": 200.0},
    ]


def _cf_rows(year):
    return [
        {"date": f"{year}-12-31", "origin_name": "營業活動之淨現金流入（流出）", "value": 300.0},
        {"date": f"{year}-12-31", "origin_name": "取得不動產、廠房及設備", "value": -100.0},
    ]


class ReplayClient:
    """只替換兩支 FinMind 網路呼叫；`(df, ok)` 語意照 `FinMindClient._fetch`。"""

    def __init__(self, balance_sheet, cash_flows):
        self._bs, self._cf = balance_sheet, cash_flows

    def fetch_balance_sheet(self, code, start_date):
        return self._bs

    def fetch_cash_flows_statement(self, code, start_date):
        return self._cf


OK_BS = (pd.DataFrame(_bs_rows(2025, 4)), True)
OK_CF = (pd.DataFrame(_cf_rows(2025)), True)
FAILED = (pd.DataFrame(), False)          # 網路／API 錯誤
NO_DATA = (pd.DataFrame(), True)          # FinMind 回 {"data": []}


SEED = {
    "companyCode": "9999",
    "companyName": "測試公司",
    "historical": {
        "annual": [
            {"year": 2025, "netIncome": 60.0, "eps": 4.0,
             "roe": 10.0, "roa": 6.0, "ocf": 300, "fcf": 200, "debtRatio": 40.0},
            {"year": 2016, "netIncome": 50.0, "eps": 3.0,
             "roe": 9.9, "roa": 5.5, "ocf": 250, "fcf": 150, "debtRatio": 41.0},
        ],
        "quarterly": [
            {"year": 2025, "quarter": 4, "revenue": 100.0, "eps": 1.0,
             "currentRatio": 250.0, "debtRatio": 40.0},
        ],
    },
    "lastUpdated": "2026-01-01",
}


@pytest.fixture
def seeded(isolated_file_manager):
    isolated_file_manager.save_financial_data("9999", json.loads(json.dumps(SEED)))
    return isolated_file_manager


def annual_metrics(file_mgr):
    data = file_mgr.load_financial_data("9999")
    return {
        a["year"]: {k: a.get(k) for k in ("roe", "roa", "ocf", "fcf", "debtRatio")}
        for a in data["historical"]["annual"]
    }


# ── 1. 新期間加入，舊期間完整保留 ─────────────────────────────────────────

def test_new_quarter_added_keeps_older_quarters():
    hist = build({"historical": {"quarterly": [quarter(2025, 1), quarter(2025, 2)], "annual": []}},
                 quarterly=[quarter(2025, 3)])
    assert periods(hist["quarterly"]) == [(2025, 3), (2025, 2), (2025, 1)]


def test_quarter_outside_fetch_window_survives():
    """抓取視窗只有近一年；視窗外的舊季度不得因為沒被重抓就消失。"""
    hist = build({"historical": {"quarterly": [quarter(2019, 1)], "annual": []}},
                 quarterly=[quarter(2026, 2)])
    assert (2019, 1) in periods(hist["quarterly"])


def test_monthly_revenue_outside_window_preserved_up_to_cap():
    """月營收保留上限 72 筆（六年）——是刻意的保留政策，不是遺失。"""
    old = [{"year": 2020 + i // 12, "month": i % 12 + 1, "revenue": float(i), "yoy": None}
           for i in range(80)]
    hist = build({"historical": {"quarterly": [], "annual": [], "monthlyRevenue": old}},
                 monthly=[{"year": 2027, "month": 1, "revenue": 1.0, "yoy": None}])
    assert len(hist["monthlyRevenue"]) == 72
    assert hist["monthlyRevenue"][0] == {"year": 2027, "month": 1, "revenue": 1.0, "yoy": None}


# ── 2. 同期間重跑不產生重複或非預期差異 ───────────────────────────────────

def test_rerunning_same_quarter_is_idempotent():
    existing = {"historical": {"quarterly": [quarter(2025, 1), quarter(2025, 2)], "annual": []}}
    once = build(json.loads(json.dumps(existing)), quarterly=[quarter(2025, 2)])
    twice = build({"historical": json.loads(json.dumps(once))}, quarterly=[quarter(2025, 2)])
    assert periods(once["quarterly"]) == periods(twice["quarterly"]) == [(2025, 2), (2025, 1)]
    assert once["quarterly"] == twice["quarterly"]


def test_same_period_revision_replaces_not_duplicates():
    """同一期間的修訂值覆蓋舊值，不長出第二筆。"""
    hist = build({"historical": {"quarterly": [quarter(2025, 1, revenue=100.0)], "annual": []}},
                 quarterly=[quarter(2025, 1, revenue=777.0)])
    assert periods(hist["quarterly"]) == [(2025, 1)]
    assert hist["quarterly"][0]["revenue"] == 777.0


def test_out_of_order_input_is_sorted_newest_first():
    hist = build({"historical": {"quarterly": [quarter(2024, 4)], "annual": []}},
                 quarterly=[quarter(2024, 2), quarter(2025, 1), quarter(2024, 3)])
    assert periods(hist["quarterly"]) == [(2025, 1), (2024, 4), (2024, 3), (2024, 2)]


# ── 3. 部分更新／部分來源失敗，不清空既有歷史 ─────────────────────────────

def test_failed_financials_fetch_keeps_stored_history():
    """損益表整批抓失敗（quarterly 空）時，既有季度／月營收／股利不得被清掉。

    年度是刻意「從合併後的季度重算」而不是沿用舊 annual（見 `build_final_data` 註解），
    所以這裡驗的是：重算的來源是被保留下來的季度，且 `_PRESERVE` 欄位跟著留下。"""
    existing = {"historical": {
        "quarterly": [quarter(2025, 1, revenue=100.0)],
        "annual": [{"year": 2025, "eps": 1.0, "roe": 10.0}],
        "monthlyRevenue": [{"year": 2025, "month": 1, "revenue": 5.0, "yoy": None}],
        "dividends": [{"year": 2024, "cashDividend": 3.0}],
    }}
    hist = build(existing, quarterly=[], monthly=[], quality="low")
    assert periods(hist["quarterly"]) == [(2025, 1)]
    assert hist["annual"][0]["year"] == 2025
    assert hist["annual"][0]["revenue"] == 100.0, "年度由保留下來的季度重算"
    assert hist["annual"][0]["roe"] == 10.0, "_PRESERVE 欄位不因重算而消失"
    assert hist["monthlyRevenue"] == [{"year": 2025, "month": 1, "revenue": 5.0, "yoy": None}]
    assert hist["dividends"] == [{"year": 2024, "cashDividend": 3.0}]


def test_annual_balance_sheet_fields_survive_income_statement_refetch():
    """`_PRESERVE`：年度重算時，資產負債表／現金流量補上的欄位要留著。"""
    existing = {"historical": {
        "quarterly": [quarter(2025, q) for q in (1, 2, 3, 4)],
        "annual": [{"year": 2025, "roe": 11.1, "roa": 7.7, "ocf": 999, "fcf": 888, "debtRatio": 33.3}],
    }}
    hist = build(existing, quarterly=[quarter(2025, 1)])
    got = hist["annual"][0]
    assert (got["roe"], got["roa"], got["ocf"], got["fcf"], got["debtRatio"]) == (11.1, 7.7, 999, 888, 33.3)


def test_quarterly_balance_sheet_snapshot_survives_income_statement_refetch():
    """季度的 currentRatio / debtRatio 只由資產負債表任務寫入，
    損益表重抓同一季不得把它們沖掉——`financialHealth.ts` 取「最近一季有值的快照」，
    沖掉會讓個股健檢的期別倒退一整季。"""
    existing = {"historical": {
        "quarterly": [quarter(2025, 2, currentRatio=245.76, debtRatio=30.94)],
        "annual": [],
    }}
    hist = build(existing, quarterly=[quarter(2025, 2, revenue=777.0)])
    got = hist["quarterly"][0]
    assert got["revenue"] == 777.0, "損益數字要換成新抓到的"
    assert got["currentRatio"] == 245.76
    assert got["debtRatio"] == 30.94


def test_balance_sheet_partial_source_failure_keeps_other_domain(seeded):
    """資產負債表抓失敗、現金流量成功：roe/roa/debtRatio 是既有值，不得被寫成 null。"""
    _update_one(ReplayClient(FAILED, OK_CF), DataProcessor(), seeded, "9999", "測試公司", "2019-01-01")
    got = annual_metrics(seeded)[2025]
    assert got["ocf"] == 300.0 and got["fcf"] == 200.0, "現金流量成功，應更新"
    assert got["roe"] == 10.0 and got["roa"] == 6.0 and got["debtRatio"] == 40.0


def test_balance_sheet_partial_source_failure_keeps_cash_flow(seeded):
    """反向：現金流量抓失敗、資產負債表成功，ocf/fcf 不得被寫成 null。"""
    _update_one(ReplayClient(OK_BS, FAILED), DataProcessor(), seeded, "9999", "測試公司", "2019-01-01")
    got = annual_metrics(seeded)[2025]
    assert got["roe"] == 10.0 and got["roa"] == 6.0
    assert got["ocf"] == 300 and got["fcf"] == 200


def test_balance_sheet_year_outside_source_window_keeps_stored_values(seeded):
    """視窗外的年度（2016）來源本來就不會回資料，既有 roe/ocf 不得被逐年抹成 null。

    正式資料上的痕跡：2022–2025 年幾乎每檔都有 ocf，2021 年卻有 800 檔只剩 roe、
    ocf 已成 null——就是這條路徑逐年往前吃歷史。"""
    _update_one(ReplayClient(OK_BS, OK_CF), DataProcessor(), seeded, "9999", "測試公司", "2019-01-01")
    got = annual_metrics(seeded)[2016]
    assert got == {"roe": 9.9, "roa": 5.5, "ocf": 250, "fcf": 150, "debtRatio": 41.0}


def test_balance_sheet_both_sources_empty_touches_nothing(seeded):
    """既有守門：兩邊都沒資料就整筆跳過。"""
    before = seeded.load_financial_data("9999")
    _update_one(ReplayClient(FAILED, FAILED), DataProcessor(), seeded, "9999", "測試公司", "2019-01-01")
    assert seeded.load_financial_data("9999") == before


def test_balance_sheet_no_data_response_is_not_a_wipe(seeded):
    """FinMind 回 `{"data": []}` 是 ok=True 但空 df——與抓失敗一樣不得清空既有值。"""
    _update_one(ReplayClient(NO_DATA, OK_CF), DataProcessor(), seeded, "9999", "測試公司", "2019-01-01")
    got = annual_metrics(seeded)[2025]
    assert got["roe"] == 10.0 and got["roa"] == 6.0


def test_balance_sheet_writes_real_null_when_source_says_zero_equity(seeded):
    """來源有回這一季、但權益為 0 → roe 算不出來，寫 null 是正確的（與「沒回資料」不同）。"""
    rows = [r for r in _bs_rows(2025, 4) if r["origin_name"] != "權益總額"]
    rows.append({"date": "2025-12-30", "origin_name": "權益總額", "value": 0.0})
    _update_one(ReplayClient((pd.DataFrame(rows), True), OK_CF), DataProcessor(),
                seeded, "9999", "測試公司", "2019-01-01")
    assert annual_metrics(seeded)[2025]["roe"] is None


# ── 4. null / 0 / 缺欄位 / 空資料不被混同 ─────────────────────────────────

def test_zero_revenue_month_is_kept_not_dropped():
    hist = build({"historical": {"quarterly": [], "annual": [],
                                 "monthlyRevenue": [{"year": 2025, "month": 1, "revenue": 0, "yoy": None}]}})
    assert hist["monthlyRevenue"] == [{"year": 2025, "month": 1, "revenue": 0, "yoy": None}]


def test_empty_new_data_is_not_the_same_as_zero():
    """新資料為空 → 保留舊值；新資料為 0 → 覆蓋成 0。"""
    existing = {"historical": {"quarterly": [quarter(2025, 1, revenue=100.0)], "annual": []}}
    kept = build(json.loads(json.dumps(existing)), quarterly=[])
    assert kept["quarterly"][0]["revenue"] == 100.0
    zeroed = build(json.loads(json.dumps(existing)), quarterly=[quarter(2025, 1, revenue=0.0)])
    assert zeroed["quarterly"][0]["revenue"] == 0.0


def test_dividends_none_is_read_back_as_empty_not_crash():
    """已發布資料裡 dividends 有 554 檔是 null；再跑一次不能炸。"""
    hist = build({"historical": {"quarterly": [], "annual": [], "dividends": None}},
                 dividends=[{"year": 2025, "cashDividend": 2.0}])
    assert hist["dividends"] == [{"year": 2025, "cashDividend": 2.0}]


# ── 6. 季報／年報、單季／累計不被錯誤混併 ────────────────────────────────

def test_partial_year_is_marked_cumulative():
    hist = build({"historical": {"quarterly": [], "annual": []}},
                 quarterly=[quarter(2026, 1), quarter(2026, 2)])
    assert hist["annual"][0]["year"] == 2026
    assert hist["annual"][0]["note"] == "累季", "未滿四季一定要標記，下游靠它判斷可不可以跟整年比"


def test_full_year_has_no_cumulative_note():
    hist = build({"historical": {"quarterly": [], "annual": []}},
                 quarterly=[quarter(2025, q) for q in (1, 2, 3, 4)])
    assert "note" not in hist["annual"][0]


def test_annual_is_recomputed_from_full_quarterly_history():
    """年度一律從合併後的季度重算，不讓「累季」蓋掉已完整的年度。"""
    existing = {"historical": {
        "quarterly": [quarter(2025, q, revenue=100.0) for q in (1, 2, 3, 4)],
        "annual": [{"year": 2025, "revenue": 400.0}],
    }}
    hist = build(existing, quarterly=[quarter(2025, 1, revenue=100.0)])
    assert hist["annual"][0]["revenue"] == 400.0
    assert "note" not in hist["annual"][0]


def test_cash_flow_is_cumulative_only_q4_counted(seeded):
    """現金流量表是累計制：只能取 Q4，不能把各季加總。"""
    rows = _cf_rows(2025) + [
        {"date": "2025-09-30", "origin_name": "營業活動之淨現金流入（流出）", "value": 200.0},
        {"date": "2025-06-30", "origin_name": "營業活動之淨現金流入（流出）", "value": 100.0},
    ]
    _update_one(ReplayClient(OK_BS, (pd.DataFrame(rows), True)), DataProcessor(),
                seeded, "9999", "測試公司", "2019-01-01")
    assert annual_metrics(seeded)[2025]["ocf"] == 300.0


def test_balance_sheet_snapshot_lands_on_quarterly_not_annual(seeded):
    """currentRatio 是季度快照；年度只放 roe/roa/ocf/fcf/debtRatio。"""
    _update_one(ReplayClient(OK_BS, OK_CF), DataProcessor(), seeded, "9999", "測試公司", "2019-01-01")
    data = seeded.load_financial_data("9999")
    q4 = next(q for q in data["historical"]["quarterly"] if (q["year"], q["quarter"]) == (2025, 4))
    assert q4["currentRatio"] == 250.0
    assert all("currentRatio" not in a for a in data["historical"]["annual"])


# ── 7. malformed input／寫入失敗可見，既有檔案不被截斷 ────────────────────

def test_write_failure_is_reported_and_leaves_previous_file_intact(isolated_file_manager, monkeypatch):
    good = {"companyCode": "9999", "historical": {"annual": [{"year": 2025, "eps": 4.0}]}}
    assert isolated_file_manager.save_financial_data("9999", good) is True

    import finance_tools.core.file_manager as file_manager_module

    def explode(obj, fp, **kwargs):
        fp.write('{"companyCode": "9999", "histor')
        raise OSError("simulated writer failure")

    monkeypatch.setattr(file_manager_module.json, "dump", explode)
    assert isolated_file_manager.save_financial_data("9999", {"companyCode": "9999"}) is False
    monkeypatch.undo()

    assert isolated_file_manager.load_financial_data("9999") == good
    leftovers = [f for f in __import__("os").listdir(isolated_file_manager.financials_dir)
                 if f.endswith(".tmp")]
    assert leftovers == [], f"暫存檔沒清乾淨: {leftovers}"


def test_us_writer_failure_leaves_published_file_intact(tmp_path, monkeypatch):
    """美股檔直接發到 GitHub Pages 給 App 讀，寫到一半失敗不得留下半個 JSON。"""
    from finance_tools.us_financials import fetch_us_financials as us

    target = tmp_path / "NVDA.json"
    good = {"companyCode": "NVDA", "historical": {"annual": [{"year": 2025}]}}
    us.write_json_atomic(target, good)
    assert json.loads(target.read_text()) == good

    def explode(obj, fp, **kwargs):
        fp.write('{"companyCode": "NVDA", "histor')
        raise OSError("simulated writer failure")

    monkeypatch.setattr(us.json, "dump", explode)
    with pytest.raises(OSError):
        us.write_json_atomic(target, {"companyCode": "NVDA", "new": True})
    monkeypatch.undo()

    assert json.loads(target.read_text()) == good
    assert [f for f in __import__("os").listdir(tmp_path) if f.endswith(".tmp")] == []


# ── 現況記錄（不是本次修正引入的行為，也不在此決定政策）─────────────────

def test_duplicate_quarter_within_one_batch_is_not_deduplicated():
    """**現況**：同一批新資料裡有兩筆相同 `(year, quarter)` 時，merge 不去重，兩筆都會留下，
    年度加總因此翻倍。

    這與「跨次更新同一個 key 會取代」是兩回事——後者由
    `test_same_period_revision_replaces_not_duplicates` 涵蓋，且確實會取代。
    去重只發生在上游 `DataProcessor.process_financials`（`drop_duplicates(["date","metric"])`），
    `build_final_data` 自己沒有這一層。

    目前**沒有證據**顯示正式 caller 會送出這種形狀；這裡只釘住現行行為，
    不代表批次內重複是可接受的輸入，也不在此選定去重政策（見 handoff F-08）。"""
    hist = build({"historical": {"quarterly": [], "annual": []}},
                 quarterly=[quarter(2025, 1, revenue=100.0), quarter(2025, 1, revenue=100.0)])
    assert periods(hist["quarterly"]) == [(2025, 1), (2025, 1)]
    assert hist["annual"][0]["revenue"] == 200.0


def test_us_main_exits_nonzero_when_nothing_was_written(tmp_path, monkeypatch, capsys):
    """美股 CLI：寫檔失敗會被記錄，而且**一檔都沒寫成就回非零**。

    2026-09-11 之前它永遠回 0——`update-us-financials.yml` 有 `set -o pipefail`，
    但腳本永遠成功，所以 200 檔全部抓失敗 CI 照樣綠燈。那是 WS-06 修過的
    同一類 bug（「推送失敗不再判成綠燈」），先前把它列為「另案」是錯的判斷。

    同時證明 `main()` 走的確實是 `write_json_atomic`，而不是別的寫檔路徑。"""
    from finance_tools.us_financials import fetch_us_financials as us

    monkeypatch.setattr(us, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(us, "yf", type("FakeYF", (), {"Ticker": staticmethod(lambda code: object())}))
    monkeypatch.setattr(us, "build_output", lambda code, ticker: (
        {"companyCode": code, "latest": {"revenue": 1e9, "grossMargin": 50.0},
         "historical": {"annual": [{"year": 2025}], "quarterly": []}}, ""))
    monkeypatch.setattr("sys.argv", ["fetch_us_financials.py", "NVDA"])

    calls = []
    real_writer = us.write_json_atomic

    def spy(path, data):
        calls.append(path)
        return real_writer(path, data)

    monkeypatch.setattr(us, "write_json_atomic", spy)

    # 成功路徑：main 確實呼叫 atomic writer，檔案寫成
    assert us.main() == 0, "有寫成就回 0"
    assert calls == [tmp_path / "NVDA.json"], "main 必須走 write_json_atomic"
    assert json.loads((tmp_path / "NVDA.json").read_text())["companyCode"] == "NVDA"
    assert "1 saved" in capsys.readouterr().out

    # 失敗路徑：真的 writer 內部失敗
    def explode(obj, fp, **kwargs):
        fp.write('{"companyCode": "NVDA", "histor')
        raise OSError("simulated writer failure")

    monkeypatch.setattr(us.json, "dump", explode)
    returned = us.main()          # 不得拋出
    monkeypatch.undo()

    out = capsys.readouterr().out
    assert returned == 1, "一檔都沒寫成必須回非零，否則 CI 永遠綠燈"
    assert "ERROR" in out and "1 errors" in out, "失敗有被記錄下來"
    assert "FAILED" in out
    # 原子性仍成立：先前那份好的檔案沒有被截斷
    assert json.loads((tmp_path / "NVDA.json").read_text())["companyCode"] == "NVDA"
    assert [f for f in __import__("os").listdir(tmp_path) if f.endswith(".tmp")] == []


# ── 科目名稱的半形／全形括號 ───────────────────────────────────────────

def test_cash_flow_matches_half_width_parentheses():
    """FinMind 現金流量表 2020～2023 年的科目名稱混用半形括號。

    只認全形時，「營業活動之淨現金流入(流出)」整年對不上 → ocf=None → fcf 算不出來。
    2026-09-11 直接打 FinMind 實測：823 家公司的 2021 年就是這樣缺的
    （1101 台泥 2021 年回的是半形；1102 亞泥回全形所以一直正常）。
    先前（handoff §41）誤判成「來源沒給」，其實來源一直都有。"""
    df = pd.DataFrame([
        {"date": "2021-12-31", "origin_name": "營業活動之淨現金流入(流出)", "value": 18_970_000_000.0},
        {"date": "2021-12-31", "origin_name": "取得不動產、廠房及設備", "value": -16_552_788_000.0},
    ])
    parsed = DataProcessor.process_cash_flows("1101", df)
    assert parsed[2021]["ocf"] == 18_970_000_000.0, "半形括號的營業活動現金流必須認得"
    assert parsed[2021]["capex"] == -16_552_788_000.0


def test_full_width_parentheses_still_work():
    df = pd.DataFrame([
        {"date": "2021-12-31", "origin_name": "營業活動之淨現金流入（流出）", "value": 8_596_708_000.0},
        {"date": "2021-12-31", "origin_name": "取得不動產、廠房及設備", "value": -3_250_124_000.0},
    ])
    assert DataProcessor.process_cash_flows("1102", df)[2021]["ocf"] == 8_596_708_000.0


def test_income_statement_half_width_does_not_silently_become_zero():
    """損益表對不上的科目會變成 **0**（不是 None），比現金流量表更危險。
    目前 FinMind 損益表全是全形，這條是預防：半形也必須認得。"""
    df = pd.DataFrame([
        {"date": "2021-12-31", "origin_name": "營業收入", "value": 1000.0},
        {"date": "2021-12-31", "origin_name": "營業毛利(毛損)", "value": 400.0},
        {"date": "2021-12-31", "origin_name": "營業利益(損失)", "value": 200.0},
        {"date": "2021-12-31", "origin_name": "本期淨利(淨損)", "value": 150.0},
        {"date": "2021-12-31", "origin_name": "基本每股盈餘(元)", "value": 1.5},
    ])
    _, quarterly = DataProcessor.process_financials("9999", df)
    q = quarterly[0]
    assert (q["grossProfit"], q["operatingIncome"], q["netIncome"], q["eps"]) == (400.0, 200.0, 150.0, 1.5)
