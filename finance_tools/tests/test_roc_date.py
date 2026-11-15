"""民國年 ↔ 西元：規則本身，以及「其他檔案不准自己換算民國年」的守門。

規則與 stock_map 的 scripts/lib/roc_date.py、src/shared/lib/roc-date.mjs 相同，三份由 stock_map 的
test_methods/market-events/roc-date.test.mjs 用同一組案例對帳。

以前約 22 處各寫 ± 1911：有的只切字串不檢查（1151340 → 2026-13-40），格式不對時有的回 None、
有的回空字串、有的原樣回傳民國字串寫進 D1。
"""
import os
import re
from pathlib import Path

import pytest

from finance_tools.core.roc_date import (
    ad_year_to_roc, iso_to_roc, roc_month_to_iso, roc_to_iso, roc_year_to_ad, to_ad_year,
)

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("value,want", [
    ("1150629", "2026-06-29"), ("115/06/29", "2026-06-29"), ("115.6.29", "2026-06-29"),
    ("115年12月21日", "2026-12-21"), ("99/1/5", "2010-01-05"), ("0990105", "2010-01-05"),
    ("1130229", "2024-02-29"), ("1140229", None), ("1151340", None), ("11506290", None),
    ("", None), (None, None), ("2026-06-29", None),
])
def test_roc_to_iso(value, want):
    assert roc_to_iso(value) == want


def test_other_conversions():
    assert roc_month_to_iso("11507") == "2026-07"
    assert roc_month_to_iso("11513") is None
    assert roc_year_to_ad("115") == 2026
    assert roc_year_to_ad(115) == 2026
    assert roc_year_to_ad("2026") is None
    assert iso_to_roc("2026-09-02") == "1150902"
    assert iso_to_roc("2026-08-11", "/") == "115/08/11"
    assert iso_to_roc("2010-12-31") == "0991231"
    assert to_ad_year(115) == 2026 and to_ad_year(2026) == 2026
    assert ad_year_to_roc(2026) == 115


def test_no_hand_written_roc_arithmetic():
    pattern = re.compile(r"[-+<>=]\s*1911\b|\b1911\s*[-+]")
    skip_dirs = {".git", ".venv", "node_modules", "__pycache__", "src", "tests"}
    allowed = {"finance_tools/core/roc_date.py"}
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
    assert offenders == [], f"改用 finance_tools.core.roc_date：{offenders}"
