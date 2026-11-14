"""台股交易日：規則、日曆合併，以及「只有 trading_day.py 能碰休市表」的守門。"""
import json
import os
import re
from datetime import date
from pathlib import Path

import pytest

from finance_tools.core import trading_day as td

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    def use(data):
        path = tmp_path / "tw-trading-calendar.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr(td, "CALENDAR_PATH", path)
        td._calendar.cache_clear()
    yield use
    td._calendar.cache_clear()


def test_weekends_holidays_typhoon_and_uncovered_years(calendar):
    calendar({"years": [2026], "closedDates": ["2026-12-25"], "unscheduled": {"2026-10-23": "颱風"}})
    assert td.is_tw_trading_day(date(2026, 12, 24))
    assert not td.is_tw_trading_day("2026-12-25")          # 國定假日
    assert not td.is_tw_trading_day("20261023")            # 只寫在 unscheduled 也算休市
    assert not td.is_tw_trading_day(date(2026, 12, 26))    # 週六
    assert td.is_tw_trading_day(date(2027, 1, 1))          # 沒涵蓋的年份：平日當交易日（保守）


def test_prev_and_between_skip_closed_days(calendar):
    calendar({"years": [2026], "closedDates": ["2026-12-25"], "unscheduled": {}})
    assert td.prev_trading_day("2026-12-28") == "2026-12-24"
    assert td.trading_days_between("20261223", "20261229") == [
        date(2026, 12, 23), date(2026, 12, 24), date(2026, 12, 28), date(2026, 12, 29)]


def test_parse_keeps_only_real_closures():
    rows = [
        {"Date": "1160101", "Name": "中華民國開國紀念日", "Description": "依規定放假1日。"},
        {"Date": "1160104", "Name": "國曆新年開始交易日", "Description": "國曆新年開始交易。"},
        {"Date": "1160202", "Name": "市場無交易，僅辦理結算交割作業。", "Description": ""},
        {"Date": "1160201", "Name": "農曆春節前最後交易日", "Description": "2月2日及2月3日市場無交易"},
    ]
    years, closed = td.parse_twse_holidays(rows)
    assert years == {2027}
    assert closed == {"2027-01-01", "2027-02-02"}
    with pytest.raises(ValueError):
        td.parse_twse_holidays([])


def test_merge_never_drops_other_years_and_keeps_typhoon():
    # 2026-11 證交所休市表換成 2027：以前各程式即時抓，2026 的休市日就整批消失
    existing = {"years": [2026], "closedDates": ["2026-10-23", "2026-12-25"],
                "unscheduled": {"2026-10-23": "颱風"}}
    rows = [{"Date": "1160101", "Name": "開國紀念日", "Description": "依規定放假1日。"}]
    out = td.merge_calendar(existing, rows, date(2026, 11, 14))
    assert out["years"] == [2026, 2027]
    assert out["closedDates"] == ["2026-10-23", "2026-12-25", "2027-01-01"]
    assert out["unscheduled"] == {"2026-10-23": "颱風"}


def test_merge_takes_official_table_for_its_own_year():
    existing = {"years": [2027], "closedDates": ["2027-01-01", "2027-03-01"], "unscheduled": {}}
    rows = [{"Date": "1160101", "Name": "開國紀念日", "Description": "依規定放假1日。"}]
    assert td.merge_calendar(existing, rows, date(2027, 1, 2))["closedDates"] == ["2027-01-01"]


def test_coverage_problem(calendar):
    calendar({"years": [2026], "closedDates": [], "unscheduled": {}})
    assert td.coverage_problem(date(2026, 11, 30)) is None
    assert "2027" in td.coverage_problem(date(2026, 12, 1))
    calendar({"years": [2026, 2027], "closedDates": [], "unscheduled": {}})
    assert td.coverage_problem(date(2026, 12, 1)) is None


def test_committed_calendar_is_consistent():
    raw = json.loads(td.CALENDAR_PATH.read_text(encoding="utf-8"))
    assert raw["years"] == sorted(set(raw["years"]))
    assert raw["closedDates"] == sorted(set(raw["closedDates"]))
    for d in raw["closedDates"]:
        date.fromisoformat(d)
        assert int(d[:4]) in raw["years"], f"{d} 不在涵蓋年份內"
    assert set(raw["unscheduled"]) <= set(raw["closedDates"])


def test_only_trading_day_module_touches_holiday_table():
    """證交所休市表、手寫的停市日只准出現在 trading_day.py；其他地方各寫一份就會像 2026-11 那樣換年失效。"""
    skip_dirs = {".git", ".venv", "node_modules", "__pycache__", "src"}
    allowed = {"finance_tools/core/trading_day.py", "finance_tools/tests/test_trading_day.py"}
    pattern = re.compile(r"holidaySchedule|UNSCHEDULED|_MARKET_HOLIDAYS|_tw_holidays")
    offenders = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in files:
            path = Path(root) / name
            rel = path.relative_to(REPO).as_posix()
            if path.suffix in {".py", ".yml", ".yaml", ".mjs", ".js", ".ts", ".sh"} and rel not in allowed \
                    and pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                offenders.append(rel)
    assert offenders == [], f"改用 finance_tools/core/trading_day.py：{offenders}"
