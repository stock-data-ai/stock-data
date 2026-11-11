"""月營收公布紀錄：表格解析、公布日判定、更正、月中核對（離線）。"""
import json
from datetime import datetime

import pytest

from finance_tools.core.timezone import TW_TZ
from finance_tools.domains.revenue_announcements import fetcher, tasks

HEAD = ("<html><b>上市公司115年10月份(累計與當月)營業收入統計表</b>"
        "<div class=tt>出表日期：115/11/11<!--08:56:43--></div>")
ROW = ("<tr align=right><td align=center>{code}</td><td align=left>{name}</td>"
       "<td nowrap> {rev}</td><td nowrap> 100</td><td nowrap> 80</td><td nowrap> 1.50</Td>"
       "<Td nowrap> 25.00</td><td nowrap> 1,000</td><td nowrap> 900</td><td nowrap> 11.11</td>"
       "<td align=center>{note}</td></tr>")
TOTAL = ("<tr align=right><th class=tt nowrap colspan=2 align=center>合計</th>"
         "<td nowrap> 0</td></tr>")


def page(rows, industry="半導體業"):
    body = "".join(ROW.format(**r) for r in rows)
    return f"{HEAD}<table><tr><th class=tt align=left >產業別：{industry}</th></tr>{body}{TOTAL}</table></html>"


def fetched(*rows, market="sii"):
    parsed = fetcher.parse_mops_table(page(rows), 2026, 10)["rows"]
    for r in parsed:
        r.update(market=market, foreign=False)
    out = {m: {"generatedAt": "2026-11-11 08:56:43", "rows": []} for m in fetcher.MARKETS}
    out[market]["rows"] = parsed
    return out


def at(day, hour):
    return datetime(2026, 11, day, hour, 30, tzinfo=TW_TZ)


def test_parse_row_units_and_note():
    rows = fetcher.parse_mops_table(page([{"code": "2330", "name": "台積電", "rev": "1,234", "note": "需求增加"}]), 2026, 10)
    r = rows["rows"][0]
    assert rows["generatedAt"] == "2026-11-11 08:56:43"
    assert r["revenue"] == 1_234_000            # 千元 → 元
    assert r["mom"] == 1.5 and r["yoy"] == 25.0  # <Td> 大寫也要吃到
    assert r["industry"] == "半導體業" and r["note"] == "需求增加"


def test_parse_dash_note_is_none_and_total_row_skipped():
    rows = fetcher.parse_mops_table(page([{"code": "1101", "name": "台泥", "rev": "5", "note": "-"}]), 2026, 10)["rows"]
    assert len(rows) == 1 and rows[0]["note"] is None


def test_wrong_month_and_empty_page():
    with pytest.raises(fetcher.SourceError):
        fetcher.parse_mops_table(page([{"code": "1101", "name": "台泥", "rev": "5", "note": "-"}]), 2026, 9)
    empty = "<b>上市公司115年11月份(累計與當月)營業收入統計表</b><h3 align=center>查無資料</h3>"
    assert fetcher.parse_mops_table(empty, 2026, 11)["rows"] == []


def test_broken_format_raises():
    with pytest.raises(fetcher.SourceError):
        fetcher.parse_mops_table(HEAD + "<table>格式變了</table>", 2026, 10)


def test_announce_date_day_overnight_and_revision():
    a = {"code": "2330", "name": "台積電", "rev": "100", "note": "-"}
    b = {"code": "3017", "name": "奇鋐", "rev": "50", "note": "-"}
    data, s = tasks.merge_announcements(None, fetched(a), at(1, 20), 2026, 10, None)
    assert s["new"] == 1 and data["companies"][0]["date"] == "2026-11-01"
    assert data["companies"][0]["window"] == "day"

    data, s = tasks.merge_announcements(data, fetched(a, b), at(2, 7), 2026, 10, None)
    c = {x["code"]: x for x in data["companies"]}
    assert s["new"] == 1 and c["3017"]["date"] == "2026-11-01" and c["3017"]["window"] == "overnight"

    a2 = {**a, "rev": "120"}
    data, s = tasks.merge_announcements(data, fetched(a2, b), at(2, 20), 2026, 10, None)
    c = {x["code"]: x for x in data["companies"]}
    assert s["revised"] == 1 and c["2330"]["revenue"] == 120_000
    assert c["2330"]["date"] == "2026-11-01" and "revisedAt" in c["2330"]   # 更正不改公布日


def test_first_run_late_in_month_is_backfill():
    a = {"code": "2330", "name": "台積電", "rev": "100", "note": "-"}
    data, _ = tasks.merge_announcements(None, fetched(a), at(11, 20), 2026, 10, None)
    assert data["companies"][0]["date"] is None and data["companies"][0]["window"] == "backfill"


def test_previous_high(isolated_file_manager):
    isolated_file_manager.save_financial_data("2330", {"historical": {"monthlyRevenue": [
        {"year": 2026, "month": 10, "revenue": 999},   # 目標月份本身不算
        {"year": 2026, "month": 9, "revenue": 500},
        {"year": 2025, "month": 12, "revenue": 700},
    ]}})
    assert tasks.previous_high("2330", 2026, 10, isolated_file_manager) == {"prevHigh": 700, "historyMonths": 2}
    assert tasks.previous_high("9999", 2026, 10, isolated_file_manager) == {}


def test_dump_is_valid_json_one_company_per_line(tmp_path):
    a = {"code": "2330", "name": "台積電", "rev": "100", "note": "-"}
    data, _ = tasks.merge_announcements(None, fetched(a), at(1, 20), 2026, 10, None)
    text = tasks.dump_month(data)
    assert json.loads(text) == data
    assert '\n    {"code":"2330"' in text


def official(rows):
    out = {m: {"month": "2026-10", "generatedAt": "2026-11-17", "rows": []} for m in fetcher.MARKETS}
    out["sii"]["rows"] = rows
    return out


def off_row(code, name, revenue, industry="半導體業"):
    return {"code": code, "name": name, "industry": industry, "market": "sii",
            "revenue": revenue, "prevMonth": 100_000, "lastYear": 80_000, "mom": 1.5, "yoy": 25.0,
            "cumulative": 1_000_000, "cumLastYear": 900_000, "cumYoy": 11.11}


def test_verify_all_match():
    a = {"code": "2330", "name": "台積電", "rev": "100", "note": "-"}
    data, _ = tasks.merge_announcements(None, fetched(a), at(1, 20), 2026, 10, None)
    report = tasks.verify_against_official(data, official([off_row("2330", "台積電", 100_000)]), at(17, 9), None, 2026, 10)
    assert report["ok"] and report["matched"] == 1
    subject, _ = tasks.report_mail(report, 2026, 10)
    assert subject.startswith("✅")


def test_verify_mismatch_missing_and_financials():
    a = {"code": "2330", "name": "台積電", "rev": "100", "note": "-"}
    x = {"code": "9999", "name": "神秘", "rev": "1", "note": "-"}
    data, _ = tasks.merge_announcements(None, fetched(a, x), at(1, 20), 2026, 10, None)
    report = tasks.verify_against_official(data, official([
        off_row("2330", "台積電", 101_000),
        off_row("2881", "富邦金", 5_000, industry="金融保險業"),
        off_row("3017", "奇鋐", 7_000),
    ]), at(17, 9), None, 2026, 10)
    c = {x["code"]: x for x in data["companies"]}
    assert [r["code"] for r in report["corrected"]] == ["2330"] and c["2330"]["revenue"] == 101_000
    assert [r["code"] for r in report["addedExpected"]] == ["2881"]
    assert [r["code"] for r in report["addedUnexpected"]] == ["3017"]
    assert [r["code"] for r in report["notInOfficial"]] == ["9999"]
    assert c["2881"]["window"] == "official" and c["2881"]["date"] is None
    assert not report["ok"]
    subject, body = tasks.report_mail(report, 2026, 10)
    assert subject.startswith("⚠️") and "1 家數字不符" in subject and "富邦金" in body


def test_target_month():
    assert tasks.target_month(at(1, 7)) == (2026, 10)
    assert tasks.target_month(datetime(2027, 1, 5, tzinfo=TW_TZ)) == (2026, 12)


def test_index_counts_by_date(tmp_path):
    a = {"code": "2330", "name": "台積電", "rev": "100", "note": "-"}
    b = {"code": "3017", "name": "奇鋐", "rev": "50", "note": "-"}
    data, _ = tasks.merge_announcements(None, fetched(a), at(1, 20), 2026, 10, None)
    data, _ = tasks.merge_announcements(data, fetched(a, b), at(3, 20), 2026, 10, None)
    tasks.save_month(tmp_path / "2026-10.json", data)
    idx = tasks.write_index(tmp_path)
    m = idx["months"]["2026-10"]
    assert m["byDate"] == {"2026-11-01": 1, "2026-11-03": 1} and m["total"] == 2 and m["verified"] is None
    assert json.loads((tmp_path / "index.json").read_text())["months"]["2026-10"]["total"] == 2


def test_apply_to_financials(isolated_file_manager):
    fm = isolated_file_manager
    fm.save_financial_data("2330", {"historical": {"monthlyRevenue": [
        {"year": 2026, "month": 9, "revenue": 500.0, "yoy": None},
        {"year": 2025, "month": 10, "revenue": 800.0, "yoy": None},
    ]}})
    fm.save_financial_data("2317", {"historical": {"monthlyRevenue": [
        {"year": 2026, "month": 10, "revenue": 300.0, "yoy": None},
    ]}})
    companies = [{"code": "2330", "revenue": 1000}, {"code": "2317", "revenue": 300},
                 {"code": "9999", "revenue": 1}, {"code": "1101", "revenue": None}]
    stats = tasks.apply_to_financials(companies, 2026, 10, fm)
    assert stats == {"updated": 1, "same": 1, "noFile": 1, "failed": 0}
    m = fm.load_financial_data("2330")["historical"]["monthlyRevenue"]
    assert m[0] == {"year": 2026, "month": 10, "revenue": 1000.0, "yoy": 25.0}   # 新→舊、年增重算
    assert len(m) == 3
    # 冪等：再跑一次不寫檔
    assert tasks.apply_to_financials(companies, 2026, 10, fm)["updated"] == 0


@pytest.mark.parametrize("day,expected", [(1, "update"), (13, "update"), (14, None), (15, "verify"), (20, "verify"), (21, None)])
def test_revenue_monthly_dispatch_by_day(monkeypatch, day, expected):
    called = []
    monkeypatch.setattr(tasks, "now_tw", lambda: datetime(2026, 11, day, 20, 30, tzinfo=TW_TZ))
    monkeypatch.setattr(tasks, "run_update_revenue_announcements", lambda a: called.append("update"))
    monkeypatch.setattr(tasks, "run_verify_revenue_announcements", lambda a: called.append("verify"))
    tasks.run_revenue_monthly(None)
    assert called == ([expected] if expected else [])
