"""ETF 配息（FinMind）與基本資料（開放資料）的純函式測試。不連網。"""
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

from finance_tools.domains.dividends.etf_dividends import merge_dividends, to_entry

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("update_etf_basics", ROOT / "etf_Crawler" / "update_etf_basics.py")
basics = importlib.util.module_from_spec(_spec)
sys.modules["update_etf_basics"] = basics
_spec.loader.exec_module(basics)


def test_to_entry_matches_old_format():
    # 0056 2026-04-23：舊檔（玩股網）是 cashYield 2.346867、price 42.61
    e = to_entry({"date": "2026-04-23", "stock_and_cache_dividend": 1.0, "stock_or_cache_dividend": "息", "before_price": 42.61})
    assert e == {"date": "2026-04-23", "cashDividend": 1, "cashYield": 2.346867, "price": 42.61, "period": 2026}


def test_to_entry_skips_stock_only_and_empty():
    assert to_entry({"date": "2026-01-01", "stock_and_cache_dividend": 1, "stock_or_cache_dividend": "權", "before_price": 10}) is None
    assert to_entry({"date": "2026-01-01", "stock_and_cache_dividend": 0, "stock_or_cache_dividend": "息", "before_price": 10}) is None
    assert to_entry({"date": "2026-01-01", "stock_and_cache_dividend": 0.5, "stock_or_cache_dividend": "息", "before_price": None})["cashYield"] is None


def test_merge_keeps_old_entries_verbatim_and_adds_new():
    old = [{"date": "2026-01-22", "cashDividend": 0.866, "cashYield": 2.266423, "price": 38.21, "period": 2026}]
    fresh = [
        {"date": "2026-01-22", "cashDividend": 0.866, "cashYield": 2.266422, "price": 38.21, "period": 2026},  # 只差殖利率尾數
        {"date": "2026-07-21", "cashDividend": 1.35, "cashYield": 2.702703, "price": 49.95, "period": 2026},
    ]
    merged = merge_dividends(old, fresh)
    assert [d["date"] for d in merged] == ["2026-07-21", "2026-01-22"]
    assert merged[1]["cashYield"] == 2.266423, "金額與價格相同就保留舊紀錄，不製造改動"
    corrected = merge_dividends(old, [{**fresh[0], "cashDividend": 0.9}])
    assert corrected[0]["cashDividend"] == 0.9, "金額不同以新資料為準"


def test_roc_to_iso():
    assert basics.roc_to_iso("1150409") == "2026-04-09"
    assert basics.roc_to_iso("") is None
    assert basics.roc_to_iso("115049") is None


def test_infer_frequency_by_gap_not_count():
    quarterly = ["2026-10-22", "2026-07-21", "2026-04-23", "2026-01-22", "2025-10-23", "2025-07-21"]
    assert basics.infer_frequency(quarterly) == "季配", "季配近 400 天會數到 5 次，用間隔判斷才對"
    monthly = [f"2026-{m:02d}-15" for m in range(10, 4, -1)]
    assert basics.infer_frequency(monthly) == "月配"
    assert basics.infer_frequency(["2026-09-16", "2026-01-20", "2025-01-17", "2024-01-17"]) == "年配"
    assert basics.infer_frequency(["2026-09-16", "2026-03-16"]) is None, "只有 2 次不判斷"


def test_compute_size_holders_yield(monkeypatch):
    monkeypatch.setattr(basics, "recent_dividends", lambda code, today, days: [
        {"date": "2026-10-22", "cashDividend": 1.72}, {"date": "2026-07-21", "cashDividend": 1.35},
        {"date": "2026-04-23", "cashDividend": 1}, {"date": "2026-01-22", "cashDividend": 0.866},
    ])
    out = basics.compute(
        "0056", date(2026, 11, 4),
        funds={"0056": {"listed": "2007-12-26", "units": 2_000_000_000}},
        closes={"0056": {"close": 53.55}},
        tdcc={"0056": {"holders": 1_500_000, "units": 1_990_000_000}},
        current={},
    )
    assert out["fundSize"] == round(2_000_000_000 * 53.55 / 1e8, 2), "單位數優先用證交所發行單位數"
    assert out["beneficiaryCount"] == 1_500_000
    assert out["trailingYield"] == round(4.936 / 53.55 * 100, 2)
    assert out["dividendFrequency"] == "季配"
    assert out["inceptionDate"] == "2007-12-26"


def test_compute_falls_back_to_tdcc_units_for_tpex():
    out = basics.compute("00679B", date(2026, 11, 4), funds={}, closes={"00679B": {"close": 26.54}},
                         tdcc={"00679B": {"holders": 227909, "units": 6_120_692_000}}, current={"inceptionDate": "2017-01-17"})
    assert out["fundSize"] == round(6_120_692_000 * 26.54 / 1e8, 2), "上櫃沒有證交所單位數，用集保合計股數"
    assert out["inceptionDate"] is None, "沒有官方上市日期就不動原值"


def test_compute_marks_old_non_paying_etf(monkeypatch):
    monkeypatch.setattr(basics, "recent_dividends", lambda code, today, days: [])
    old = basics.compute("00631L", date(2026, 11, 4), funds={"00631L": {"listed": "2014-10-31", "units": 1e9}},
                         closes={"00631L": {"close": 30.0}}, tdcc={}, current={})
    assert old["dividendFrequency"] == "不配息"
    assert old["trailingYield"] == 0.0
    new = basics.compute("00410A", date(2026, 11, 4), funds={"00410A": {"listed": "2026-08-01", "units": 1e9}},
                         closes={"00410A": {"close": 10.0}}, tdcc={}, current={})
    assert new["dividendFrequency"] is None and new["trailingYield"] == basics.CLEAR, "新上市還沒配過息：不猜、不算殖利率"
    policy = basics.compute("0057", date(2026, 11, 4), funds={"0057": {"listed": "2008-02-27", "units": 1e8}},
                            closes={"0057": {"close": 100.0}}, tdcc={}, current={"dividendFrequency": "年配"})
    assert policy["dividendFrequency"] is None, "原本有配息政策（年配）就不改成不配息"


def test_yield_annual_payer_not_zero_just_past_365_days(monkeypatch):
    # 00739 年配：上次 2025-10-23，到 2026-11-04 已超過 365 天但不到 13 個月 → 仍算這一次
    monkeypatch.setattr(basics, "recent_dividends", lambda code, today, days: [
        {"date": "2025-10-23", "cashDividend": 0.9}, {"date": "2024-10-17", "cashDividend": 0.5}, {"date": "2023-10-19", "cashDividend": 0.3}])
    out = basics.compute("00739", date(2026, 11, 4), funds={"00739": {"listed": "2018-05-01", "units": 1e8}},
                         closes={"00739": {"close": 30.0}}, tdcc={}, current={})
    assert out["dividendFrequency"] == "年配"
    assert out["trailingYield"] == round(0.9 / 30 * 100, 2), "年配只取最近一次"


def test_yield_counts_extra_payment_when_frequency_changes(monkeypatch):
    # 00770 過去年配，2026 年配了兩次：近 365 天兩次都要算
    monkeypatch.setattr(basics, "recent_dividends", lambda code, today, days: [
        {"date": "2026-09-16", "cashDividend": 6}, {"date": "2026-01-20", "cashDividend": 4.2},
        {"date": "2025-01-17", "cashDividend": 4}, {"date": "2024-01-17", "cashDividend": 4}])
    out = basics.compute("00770", date(2026, 11, 4), funds={"00770": {"listed": "2018-01-01", "units": 1e8}},
                         closes={"00770": {"close": 59.75}}, tdcc={}, current={})
    assert out["trailingYield"] == round(10.2 / 59.75 * 100, 2)


def test_future_dated_entry_dropped_and_not_used_as_start(tmp_path, monkeypatch):
    from finance_tools.domains.dividends import etf_dividends as mod
    monkeypatch.setattr(mod, "DIV_DIR", tmp_path)
    (tmp_path / "00921.json").write_text(json.dumps({"stockNo": "00921", "listingDate": "2023-01-01", "lastUpdated": "2026-05-07",
        "dividends": [{"date": "2031-03-10", "cashDividend": 0.195, "cashYield": 1, "price": 20, "period": 2031},
                      {"date": "2026-03-17", "cashDividend": 0.27, "cashYield": 1, "price": 20, "period": 2026}]}), encoding="utf-8")
    seen = {}
    def fake_fetch(code, start, end, retries=3):
        seen["start"] = start
        return [{"date": "2026-09-16", "stock_and_cache_dividend": 1.0, "stock_or_cache_dividend": "息", "before_price": 25.0}]
    monkeypatch.setattr(mod, "fetch_rows", fake_fetch)
    assert mod.update_one("00921", "2026-11-04") == "updated"
    assert seen["start"] < "2026-03-17", "查詢起點不可被未來日期推遠"
    dates = [d["date"] for d in json.loads((tmp_path / "00921.json").read_text(encoding="utf-8"))["dividends"]]
    assert dates == ["2026-09-16", "2026-03-17"]


def test_ensure_skeletons_creates_only_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(basics, "ETF_DIR", tmp_path)
    (tmp_path / "index.json").write_text(json.dumps([
        {"code": "0050", "name": "元大台灣50", "categoryId": "etf-tw-broad"},
        {"code": "00416A", "name": "主動台新臺灣中小", "categoryId": "etf-active", "issuer": "台新投信"},
    ]), encoding="utf-8")
    (tmp_path / "0050.json").write_text('{"code": "0050", "topHoldings": [1]}', encoding="utf-8")
    assert basics.ensure_skeletons() == ["00416A"]
    sk = json.loads((tmp_path / "00416A.json").read_text(encoding="utf-8"))
    assert sk["categoryId"] == "etf-active" and sk["topHoldings"] == [] and sk["lastUpdated"] is None
    assert json.loads((tmp_path / "0050.json").read_text(encoding="utf-8"))["topHoldings"] == [1], "已有的不動"


def test_no_yield_for_etf_listed_under_one_year(monkeypatch):
    monkeypatch.setattr(basics, "recent_dividends", lambda code, today, days: [{"date": "2026-10-20", "cashDividend": 0.067}])
    out = basics.compute("00991B", date(2026, 11, 4), funds={"00991B": {"listed": "2026-08-03", "units": 1e9}},
                         closes={"00991B": {"close": 15.2}}, tdcc={}, current={})
    assert out["trailingYield"] == basics.CLEAR, "上市 3 個月只配一次，近一年殖利率不算，而且要清掉舊值"
