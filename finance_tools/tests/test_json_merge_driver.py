"""財報檔的 git 合併驅動：兩個排程同時改同一檔時，兩邊的新資料都要留住。"""

import json
from pathlib import Path

from finance_tools.scripts.json_merge_driver import main, merge3


BASE = {
    "companyCode": "2330",
    "latest": {"marketCap": 100, "marginBalance": 10},
    "historical": {
        "institutionalInvestors": {"2024-12-31": {"net": 1}, "2026-09-10": {"net": 2}},
        "marginTrading": {"2026-09-10": {"marginBalance": 10}},
        "quarterly": [{"year": 2026, "quarter": 1, "eps": 1.0}],
    },
    "lastUpdated": "2026-09-10",
}


def _copy(d):
    return json.loads(json.dumps(d))


def test_two_jobs_adding_different_days_both_survive():
    """日更加三大法人、融資融券排程加融資融券——同一天、同一檔，兩筆都要在。"""
    daily = _copy(BASE)
    daily["historical"]["institutionalInvestors"]["2026-09-11"] = {"net": 3}
    daily["latest"]["marketCap"] = 120
    margin = _copy(BASE)
    margin["historical"]["marginTrading"]["2026-09-11"] = {"marginBalance": 11}
    margin["latest"]["marginBalance"] = 11

    out = merge3(BASE, margin, daily)

    assert out["historical"]["institutionalInvestors"]["2026-09-11"] == {"net": 3}
    assert out["historical"]["marginTrading"]["2026-09-11"] == {"marginBalance": 11}
    assert out["latest"] == {"marketCap": 120, "marginBalance": 11}


def test_archive_deletion_is_kept_while_the_other_side_appends():
    """封存把舊年度移出主檔（刪除）＋另一邊加今天的資料 → 舊的刪掉、新的留下。"""
    archived = _copy(BASE)
    del archived["historical"]["institutionalInvestors"]["2024-12-31"]
    daily = _copy(BASE)
    daily["historical"]["institutionalInvestors"]["2026-09-11"] = {"net": 3}

    out = merge3(BASE, archived, daily)

    assert set(out["historical"]["institutionalInvestors"]) == {"2026-09-10", "2026-09-11"}


def test_delete_vs_modify_keeps_the_modified_value():
    """一邊刪、一邊改同一筆：寧可多留一筆，也不要丟資料（封存檔裡本來就有舊版）。"""
    deleted = _copy(BASE)
    del deleted["historical"]["institutionalInvestors"]["2024-12-31"]
    modified = _copy(BASE)
    modified["historical"]["institutionalInvestors"]["2024-12-31"] = {"net": 1, "foreign_shares": 9}

    assert merge3(BASE, deleted, modified)["historical"]["institutionalInvestors"]["2024-12-31"] \
        == {"net": 1, "foreign_shares": 9}
    assert merge3(BASE, modified, deleted)["historical"]["institutionalInvestors"]["2024-12-31"] \
        == {"net": 1, "foreign_shares": 9}


def test_same_leaf_changed_on_both_sides_takes_the_incoming_push():
    current = _copy(BASE)
    current["lastUpdated"] = "2026-09-11"
    current["historical"]["quarterly"] = [{"year": 2026, "quarter": 1, "eps": 1.1}]
    incoming = _copy(BASE)
    incoming["lastUpdated"] = "2026-09-12"
    incoming["historical"]["quarterly"] = [{"year": 2026, "quarter": 1, "eps": 1.2}]

    out = merge3(BASE, current, incoming)

    assert out["lastUpdated"] == "2026-09-12"
    assert out["historical"]["quarterly"] == [{"year": 2026, "quarter": 1, "eps": 1.2}], \
        "list 是葉節點，整份取 incoming，不逐元素拼接"


def _files(tmp_path: Path, base, current, incoming):
    paths = []
    for name, value in (("O", base), ("A", current), ("B", incoming)):
        p = tmp_path / name
        p.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
        paths.append(str(p))
    return paths


def test_driver_writes_result_into_current_in_the_repo_format(tmp_path):
    incoming = _copy(BASE)
    incoming["latest"]["marketCap"] = 120
    o, a, b = _files(tmp_path, BASE, BASE, incoming)

    assert main(["driver", o, a, b]) == 0
    text = Path(a).read_text(encoding="utf-8")
    assert json.loads(text)["latest"]["marketCap"] == 120
    assert text == json.dumps(json.loads(text), ensure_ascii=False, indent=2), \
        "格式要與 FileManager.save_financial_data 一致，否則每次合併都是整檔 diff"


def test_driver_prefers_the_valid_side_when_one_side_is_corrupt(tmp_path):
    o, a, b = _files(tmp_path, BASE, '{"companyCode": "23', BASE)
    assert main(["driver", o, a, b]) == 0
    assert json.loads(Path(a).read_text(encoding="utf-8")) == BASE


def test_driver_reports_conflict_when_both_sides_are_corrupt(tmp_path):
    o, a, b = _files(tmp_path, BASE, '{"x', '{"y')
    assert main(["driver", o, a, b]) == 1


def test_driver_handles_empty_base_when_both_sides_added_the_file(tmp_path):
    current = {"companyCode": "9999", "latest": {"marketCap": 1}}
    incoming = {"companyCode": "9999", "shareholderDataHistory": {"20260911": []}}
    o, a, b = _files(tmp_path, "", current, incoming)
    assert main(["driver", o, a, b]) == 0
    merged = json.loads(Path(a).read_text(encoding="utf-8"))
    assert merged["latest"] == {"marketCap": 1} and "20260911" in merged["shareholderDataHistory"]


def test_gitattributes_routes_financial_json_to_the_driver():
    root = Path(__file__).resolve().parents[2]
    assert "src/data/layer3/company-financials/*.json merge=company-json" \
        in (root / ".gitattributes").read_text(encoding="utf-8")


def test_every_workflow_that_pushes_financial_json_enables_the_driver():
    """漏一個 workflow，那個排程推送時就退回逐行合併——而且不會有任何錯誤訊息。"""
    root = Path(__file__).resolve().parents[2]
    missing = []
    for wf in sorted((root / ".github" / "workflows").glob("*.yml")):
        text = wf.read_text(encoding="utf-8")
        if "git add src/data/layer3/company-financials/" in text and "pull --rebase" in text:
            if "merge.company-json.driver" not in text:
                missing.append(wf.name)
    assert not missing, f"這些 workflow 會推財報檔但沒啟用 JSON 合併驅動：{missing}"
