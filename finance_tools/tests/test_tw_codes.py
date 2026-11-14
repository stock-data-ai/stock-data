"""台股個股判斷：規則本身，以及「其他檔案不准自寫 4 碼判斷」的守門。"""
import os
import re
from pathlib import Path

import pytest

from finance_tools.core.tw_codes import is_tw_stock

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("code,want", [
    ("2330", True), ("1101", True), ("9958", True),
    ("0050", False), ("0056", False), ("00878", False), ("006208", False), ("00981A", False),
    ("2881A", False), ("24553", False), ("030001", False), ("AAPL", False), ("8035.JP", False),
    ("", False), ("2330\n", False),
])
def test_is_tw_stock(code, want):
    assert is_tw_stock(code) is want


def test_no_hand_written_four_digit_checks():
    """以前各處寫 `len(c) == 4 and c[0].isdigit()`、`isdigit() and len(code) == 4`，
    有的把 0050 算進來、有的沒有。一律改用 finance_tools/core/tw_codes.py。"""
    pattern = re.compile(r"len\([^)]*\)\s*==\s*4\b|isdigit\(\)\s+and\s+len\(|\[0\]\.isdigit\(\)")
    skip_dirs = {".git", ".venv", "node_modules", "__pycache__", "src", "tests"}
    allowed = {"finance_tools/core/tw_codes.py"}
    offenders = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = Path(root) / name
            if path.relative_to(REPO).as_posix() in allowed:
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(REPO).as_posix()}:{n}")
    assert offenders == [], f"改用 finance_tools.core.tw_codes.is_tw_stock：{offenders}"
