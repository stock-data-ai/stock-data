"""台股交易日——兩個 repo 的**唯一實作**：休市日曆的維護、證交所休市表的解析、交易日判斷都在這裡。

日曆檔 `src/data/market/tw-trading-calendar.json`（GitHub Pages 公開；stock_map 的
`scripts/finance/short_cover_events.py` 讀同一份，再經 `/live/short-cover-events.json` 給前端）：

    years        日曆涵蓋的年份。**只增不減**：證交所休市表只列「當年」（11 月起換成隔年），
                 以前每支程式各自即時抓，一換年 2026 年的休市日就全部消失（2026-11-13 起）。
    closedDates  休市日 YYYY-MM-DD（含 unscheduled）。
    unscheduled  臨時停市（颱風等），證交所休市表不列，**只在這裡手動補**：{"YYYY-MM-DD": "原因"}。

判斷規則：
- 週六、週日休市。
- 日曆涵蓋的年份：closedDates ∪ unscheduled 的日子休市，其餘平日是交易日。
- 沒涵蓋的年份：平日一律當交易日（寧可誤報「交易日卻沒資料」，不要漏報假成功）。
  health-check 用 `check` 在日曆沒涵蓋今年（12 月起還要涵蓋明年）時寄信。

每日任務的休市處理（stock_map docs/features/platform/自動化邏輯.md「台股休市日」）：
有今天的資料 → 更新；沒有、且這裡說休市 → 跳過算成功；沒有、但這裡說交易日 → 失敗。

命令列（只用標準函式庫，workflow 不裝套件也能跑）：
    python3 finance_tools/core/trading_day.py update             抓證交所休市表併入日曆（daily-update）
    python3 finance_tools/core/trading_day.py check              日曆涵蓋不足就 exit 1（health-check）
    python3 finance_tools/core/trading_day.py is-closed [日期]   休市 exit 0、交易日 exit 1（workflow gate）
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Union

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from finance_tools.core.timezone import taipei_today  # noqa: E402
from finance_tools.core.roc_date import roc_to_iso  # noqa: E402

CALENDAR_PATH = Path(__file__).resolve().parents[2] / "src/data/market/tw-trading-calendar.json"
TWSE_HOLIDAY_API = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"

DateLike = Union[date, str]


def _as_date(d: DateLike) -> date:
    """date、'2026-07-12' 或 '20260712' → date。"""
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return datetime.strptime(d.replace("-", ""), "%Y%m%d").date()


def parse_yyyymmdd(date_str: str) -> date:
    """'20260712' 或 '2026-07-12' → date"""
    return _as_date(date_str)


# ── 日曆檔 ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _calendar() -> tuple[frozenset[int], frozenset[str]]:
    """(涵蓋年份, 休市日)。檔案缺失或壞掉直接拋錯：日曆是進版控的資料，不該有退路。"""
    raw = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    return frozenset(raw["years"]), frozenset(raw["closedDates"]) | frozenset(raw.get("unscheduled", {}))


def is_tw_trading_day(d: DateLike) -> bool:
    d = _as_date(d)
    if d.weekday() >= 5:
        return False
    years, closed = _calendar()
    if d.year not in years:
        return True
    return d.isoformat() not in closed


def prev_trading_day(d: DateLike) -> str:
    """d 之前（不含）最近的交易日，YYYY-MM-DD。"""
    cur = _as_date(d) - timedelta(days=1)
    while not is_tw_trading_day(cur):
        cur -= timedelta(days=1)
    return cur.isoformat()


def trading_days_between(start: DateLike, end: DateLike) -> list[date]:
    """start～end（兩端都含）之間的交易日。"""
    cur, last = _as_date(start), _as_date(end)
    out = []
    while cur <= last:
        if is_tw_trading_day(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


# ── 證交所休市表 → 日曆 ──────────────────────────────────────────────────────

def parse_twse_holidays(rows: Iterable[dict]) -> tuple[set[int], set[str]]:
    """證交所休市表 → (表上的年份, 休市日)。

    表上混有「開始交易日」「最後交易日」等有開盤的說明列，只收真的休市的列：
    名稱含「市場無交易」，或說明含放假／補假／休市。
    """
    rows = list(rows)
    if not rows:
        raise ValueError("證交所休市表是空的")
    years: set[int] = set()
    closed: set[str] = set()
    for r in rows:
        roc = str(r.get("Date", ""))
        iso = roc_to_iso(roc)
        if iso is None:
            raise ValueError(f"休市表日期格式不對：{r}")
        years.add(int(iso[:4]))
        name, desc = r.get("Name", ""), r.get("Description", "")
        if "市場無交易" in name or any(k in desc for k in ("放假", "補假", "休市")):
            closed.add(iso)
    return years, closed


def merge_calendar(existing: dict, rows: Iterable[dict], today: date) -> dict:
    """把證交所休市表併進日曆：表上有的年份以表為準，其他年份原樣保留。"""
    years, closed = parse_twse_holidays(rows)
    unscheduled = dict(existing.get("unscheduled", {}))
    kept = {d for d in existing.get("closedDates", []) if int(d[:4]) not in years}
    return {
        "updatedAt": today.isoformat(),
        "years": sorted(set(existing.get("years", [])) | years),
        "closedDates": sorted(kept | closed | set(unscheduled)),
        "unscheduled": dict(sorted(unscheduled.items())),
    }


def coverage_problem(today: date) -> str | None:
    """日曆該涵蓋今年；12 月起也該涵蓋明年（證交所通常 11 月公布）。不足回說明，足夠回 None。"""
    years, _ = _calendar()
    need = [today.year] + ([today.year + 1] if today.month == 12 else [])
    missing = [y for y in need if y not in years]
    if not missing:
        return None
    return (f"台股休市日曆沒有 {'、'.join(map(str, missing))} 年（目前涵蓋 {sorted(years)}）。"
            f"沒涵蓋的年份國定假日會被當成交易日，休市日各排程會誤報「交易日卻沒資料」。")


def _update() -> int:
    with urllib.request.urlopen(TWSE_HOLIDAY_API, timeout=20) as resp:
        rows = json.loads(resp.read())
    existing = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    merged = merge_calendar(existing, rows, taipei_today())
    if merged["years"] == existing.get("years") and merged["closedDates"] == existing.get("closedDates"):
        print(f"台股休市日曆無變更（涵蓋 {merged['years']}）")
        return 0
    tmp = CALENDAR_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CALENDAR_PATH)
    print(f"台股休市日曆已更新：涵蓋 {merged['years']}，休市日 {len(merged['closedDates'])} 天")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "update":
        return _update()
    if cmd == "check":
        problem = coverage_problem(taipei_today())
        print(problem or "台股休市日曆涵蓋足夠")
        return 1 if problem else 0
    if cmd == "is-closed":
        d = _as_date(argv[1]) if len(argv) > 1 else taipei_today()
        closed = not is_tw_trading_day(d)
        print(f"{d.isoformat()} {'休市' if closed else '交易日'}")
        return 0 if closed else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
