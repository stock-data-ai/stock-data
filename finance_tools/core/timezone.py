"""台北時間——stock-data 的唯一實作（stock_map 對應 scripts/lib/taipei_time.py 與 src/shared/lib/calendar-date.mjs）。

CI 跑在 UTC：`date.today()`／`datetime.now()` 在台北早上 8 點前會差一天，在本機又跟著本機時區走。
一律用這裡的 taipei_now()／taipei_today()。守門：finance_tools/tests/test_taipei_time.py。

台灣沒有夏令時間，用固定 UTC+8，不用 ZoneInfo('Asia/Taipei')：Windows 沒裝 tzdata 時 ZoneInfo 會直接拋錯。
"""

from datetime import date, datetime, timedelta, timezone

TAIPEI = timezone(timedelta(hours=8))


def taipei_now() -> datetime:
    """台北現在（帶 +08:00 時區）。"""
    return datetime.now(TAIPEI)


def taipei_today() -> date:
    """台北今天。"""
    return taipei_now().date()
