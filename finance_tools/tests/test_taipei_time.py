"""台北時間：規則本身，以及「其他檔案不准用機器時區或自訂台北時區」的守門。

以前有四份「台北今天」（core/timezone.py 的 now_tw／today_str、trading_day.taipei_today、
etf_utils.today_tw），ETF 爬蟲還直接用 date.today()：CI 跑在 UTC，台北早上 8 點前會差一天。
"""
import os
import re
from datetime import timedelta
from pathlib import Path

from finance_tools.core.timezone import TAIPEI, taipei_now, taipei_today

REPO = Path(__file__).resolve().parents[2]


def test_taipei_now_is_utc_plus_8():
    now = taipei_now()
    assert now.utcoffset() == timedelta(hours=8)
    assert now.isoformat().endswith("+08:00")
    assert TAIPEI.utcoffset(None) == timedelta(hours=8)


def test_taipei_today_matches_taipei_now():
    before = taipei_now().date()
    assert taipei_today() in {before, taipei_now().date()}   # 剛好跨午夜時兩邊都算對


def test_no_machine_timezone_or_hand_written_taipei():
    pattern = re.compile("|".join([
        r"\bdate\.today\(\)",                  # 機器時區的今天
        r"\bdatetime\.(?:datetime\.)?(?:now|today)\(\)",  # 不帶時區的現在
        r"\butcnow\(",
        r"\.astimezone\(\)",                   # 轉成機器時區
        r"\bfromtimestamp\([^,()]+\)",         # 沒給時區的 epoch → datetime
        r"timedelta\(hours=\+?8\)",            # 自訂 UTC+8
        r"['\"]Asia/Taipei['\"]",              # ZoneInfo('Asia/Taipei')（Windows 沒 tzdata 會拋錯）
    ]))
    skip_dirs = {".git", ".venv", "node_modules", "__pycache__", "src", "tests"}
    allowed = {"finance_tools/core/timezone.py"}
    offenders = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = Path(root) / name
            rel = path.relative_to(REPO).as_posix()
            if rel in allowed:
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if pattern.search(line.split("#", 1)[0]):
                    offenders.append(f"{rel}:{n}")
    assert offenders == [], f"改用 finance_tools.core.timezone（taipei_now／taipei_today／TAIPEI）：{offenders}"
