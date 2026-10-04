"""美股名單：一律讀 stock_map 的 companies-all.json，不讀手寫清單。

回歸背景（2026-09-11）：美股爬蟲讀的是 5/27 手寫的 `us-tickers.json`（198 檔），
之後新增分析的 432 家美股**從來沒抓過財報**——公司頁、AI 分析、持股健檢全部無資料，零錯誤訊息。
"""

import json
from pathlib import Path

import pytest

from finance_tools.us_financials import fetch_us_financials as us


def _roster(tmp_path, monkeypatch, codes):
    path = tmp_path / "companies-all.json"
    path.write_text(json.dumps(codes), encoding="utf-8")
    monkeypatch.setattr(us, "resolve_companies_all_path", lambda: str(path))


def _big_us(n=us.ROSTER_MIN_US):
    return {f"U{chr(65 + i // 26 % 26)}{chr(65 + i % 26)}{chr(65 + i // 676)}": {"name": "x"} for i in range(n)}


def test_roster_takes_us_codes_only(tmp_path, monkeypatch):
    codes = {**_big_us(), "AAPL": {}, "BRK-B": {}, "JPM": {},
             "2330": {}, "0050": {"isETF": True}, "7203.JP": {}, "005930.KS": {}, "SPY": {"isETF": True}}
    _roster(tmp_path, monkeypatch, codes)
    got = set(us.load_us_roster())
    assert {"AAPL", "BRK-B", "JPM"} <= got, "美股（含 BRK-B 這種帶連字號的）都要在"
    assert not got & {"2330", "0050", "7203.JP", "005930.KS", "SPY"}, "台股、日韓、ETF 不在美股名單"


def test_truncated_roster_refuses_to_run(tmp_path, monkeypatch):
    """名單殘缺時整批不跑——拿半份名單去跑，後面的清孤兒會把一半的檔刪掉。"""
    _roster(tmp_path, monkeypatch, _big_us(us.ROSTER_MIN_US - 1))
    with pytest.raises(RuntimeError, match="殘缺"):
        us.load_us_roster()


def test_missing_roster_refuses_to_run(monkeypatch):
    monkeypatch.setattr(us, "resolve_companies_all_path", lambda: None)
    with pytest.raises(RuntimeError, match="companies-all.json"):
        us.load_us_roster()


def test_prune_removes_files_not_on_the_roster(tmp_path, monkeypatch):
    """5/27 塞進來的測試檔（ASML、TSM）不在名單上，要清掉；名單上的一檔都不能碰。"""
    monkeypatch.setattr(us, "OUTPUT_DIR", tmp_path)
    for code in ("AAPL", "JPM", "ASML", "TSM"):
        (tmp_path / f"{code}.json").write_text("{}")
    (tmp_path / ".JPM_x.json.tmp").write_text("{}")

    assert us.prune_orphans(["AAPL", "JPM"]) == ["ASML", "TSM"]
    assert sorted(p.name for p in tmp_path.glob("*.json")) == ["AAPL.json", "JPM.json"]


def test_prune_refuses_when_too_many_would_go(tmp_path, monkeypatch):
    """一次要刪超過 max(20, 2%) → 視為名單殘缺，整批不刪。"""
    monkeypatch.setattr(us, "OUTPUT_DIR", tmp_path)
    for i in range(30):
        (tmp_path / f"X{i}.json").write_text("{}")
    assert us.prune_orphans([]) == []
    assert len(list(tmp_path.glob("*.json"))) == 30


def test_prune_dry_run_deletes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(us, "OUTPUT_DIR", tmp_path)
    (tmp_path / "TSM.json").write_text("{}")
    assert us.prune_orphans([], dry_run=True) == ["TSM"]
    assert (tmp_path / "TSM.json").exists()


def test_the_hand_written_list_is_gone_from_the_code_path():
    """不得再有任何程式讀 us-tickers.json——那份清單就是 432 家沒財報的病根。"""
    src = Path(us.__file__).read_text(encoding="utf-8")
    assert "US_TICKERS_FILE" not in src and "load_us_topic_codes" not in src


def test_us_workflow_fetches_the_roster_before_running():
    root = Path(__file__).resolve().parents[2]
    wf = (root / ".github/workflows/update-us-financials.yml").read_text(encoding="utf-8")
    assert wf.index("fetch-roster") < wf.index("fetch_us_financials.py"), \
        "沒先抓名單，load_us_roster 會直接拋出（CI 上沒有隔壁的 stock_map）"
