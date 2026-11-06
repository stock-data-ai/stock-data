"""ETF 動作彙總（etf-by-stock/_moves.json）：給 stock_map「我的持股」健檢用。

鎖住三件事：主動式扣掉規模變化才算加碼減碼、對不到代號的快照不比、被動式只看成分股納入剔除。
"""
import importlib.util
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("generate_stock_etf_index", ROOT / "etf_Crawler" / "generate_stock_etf_index.py")
g = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(g)

TODAY = date(2026, 11, 6)
DAYS = ["2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]


def _rows(holdings: dict[str, int], filler: int = 25, with_code: bool = True) -> list[dict]:
    """一個快照：指定的持股＋不動的填充股（湊滿 MIN_FULL_HOLDINGS，才判斷新進出清）"""
    rows = [{"name": f"N{c}", "shares": s, **({"code": c} if with_code else {})} for c, s in holdings.items()]
    rows += [{"name": f"F{i}", "shares": 100_000, "code": f"F{i}"} for i in range(filler)]
    return rows


def _etf(code: str, first: dict, last: dict, scale: float = 1.0, **kw) -> dict:
    history = {d: _rows(first, **kw) for d in DAYS[:-1]}
    last_rows = _rows(last, **kw)
    for r in last_rows:
        if r["name"].startswith("F"):
            r["shares"] = int(r["shares"] * scale)
    history[DAYS[-1]] = last_rows
    return {"code": code, "name": code, "lastUpdated": DAYS[-1], "holdingsHistory": history}


def _moves(*etfs):
    etfs = list(etfs)
    return g.build_moves(etfs, g.build_name_map(etfs), TODAY)


def test_active_new_add_trim_exit():
    etf = _etf("00999A", {"1111": 100_000, "2222": 100_000, "3333": 50_000}, {"1111": 300_000, "2222": 40_000, "4444": 20_000})
    kinds = {code: [m["kind"] for m in rows] for code, rows in _moves(etf).items()}
    assert kinds == {"1111": ["add"], "2222": ["trim"], "3333": ["exit"], "4444": ["new"]}


def test_active_proportional_flow_is_not_add():
    """整檔規模 +10%：每檔都照比例多買，不是經理人看好哪一檔"""
    etf = _etf("00998A", {"1111": 1_000_000}, {"1111": 1_100_000}, scale=1.1)
    assert _moves(etf) == {}
    # 同樣規模 +10%，但這檔多了 50%：扣掉規模後仍明顯，算加碼
    etf = _etf("00998A", {"1111": 1_000_000}, {"1111": 1_500_000}, scale=1.1)
    assert [m["kind"] for m in _moves(etf)["1111"]] == ["add"]


def test_small_changes_ignored():
    etf = _etf("00997A", {"1111": 1_000_000}, {"1111": 1_000_500})
    assert _moves(etf) == {}


def test_low_code_coverage_skips_etf():
    """歷史只有名稱、又對不回代號：整檔略過，不把對不到的列當成出清"""
    etf = _etf("00996A", {"1111": 100_000}, {"1111": 300_000}, filler=0, with_code=False)
    assert _moves(etf) == {}


def test_name_map_fills_missing_codes():
    """歷史沒代號，但別處有同名帶代號的紀錄：對得回去"""
    etf = _etf("00995A", {"1111": 100_000}, {"1111": 300_000}, with_code=False)
    etf["topHoldings"] = [{"name": "N1111", "code": "1111"}]
    assert [m["kind"] for m in _moves(etf)["1111"]] == ["add"]


def test_passive_only_join_leave():
    """被動式：股數增減多半是申購贖回，只列成分股納入／剔除"""
    etf = _etf("0056", {"1111": 100_000, "3333": 50_000}, {"1111": 900_000, "4444": 20_000})
    kinds = {code: [m["kind"] for m in rows] for code, rows in _moves(etf).items()}
    assert kinds == {"3333": ["leave"], "4444": ["join"]}
    # 只揭露前幾大的被動式（槓桿、期貨）：不判斷納入剔除
    small = _etf("00631L", {"3333": 50_000}, {"4444": 20_000}, filler=3)
    assert _moves(small) == {}


def test_stale_etf_skipped():
    etf = _etf("00994A", {"1111": 100_000}, {"1111": 300_000})
    etf["lastUpdated"] = "2026-10-01"
    assert _moves(etf) == {}
