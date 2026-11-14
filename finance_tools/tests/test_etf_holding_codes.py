"""ETF 持股代號分類（etf_utils.classify_holding_code）。

2026-11-14：「4～6 碼數字就是台股」讓 009826（資料來源不標國家）的日、港、韓股撞到台股代號，
網站把 Advantest 顯示成「宏碁智醫」、Sony 顯示成「冠亞」。混合市場 ETF 改列 MIXED_MARKET_ETFS。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "etf_Crawler"))
_spec = importlib.util.spec_from_file_location("etf_utils", ROOT / "etf_Crawler" / "etf_utils.py")
u = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(u)


@pytest.mark.parametrize("raw,name,mixed,expected", [
    # 一般 ETF：數字代號就是台股，英文名也一樣（國泰給「Lotes Co Ltd」）
    ("2330", "台積電", False, ("2330", None)),
    ("3533", "Lotes Co Ltd", False, ("3533", None)),
    ("00679B", "元大美債20年", False, ("00679B", None)),
    ("00636", "CFA50", False, ("00636", None)),
    # 混合市場 ETF：數字代號分不出國家，中文名才算台股，其餘不連結
    ("6857", "ADVANTEST", True, (None, "6857")),
    ("2330", "TAIWAN SEMICONDUCTOR MANUFACTURING", True, (None, "2330")),
    ("2330", "台積電", True, ("2330", None)),
    # 帶交易所的照交易所（台新以前把「6857 JP」只看前半段，當成台股）
    ("2330 TT", "TSMC", False, ("2330", None)),
    ("6857 JP", "ADVANTEST", False, (None, "6857 JP")),
    ("NVDA US", "NVIDIA", False, (None, "NVDA US")),
    # 期貨不是台股
    ("202701S", "黃豆期貨", False, (None, "202701S")),
    # 空值
    ("", "x", False, (None, None)),
    ("nan", "x", False, (None, None)),
])
def test_classify_holding_code(raw, name, mixed, expected):
    assert u.classify_holding_code(raw, name, mixed_market=mixed) == expected


def test_mixed_market_list_has_009826():
    assert "009826" in u.MIXED_MARKET_ETFS


def test_warns_when_many_numeric_codes_have_english_names(capsys):
    holdings = [{"code": str(6800 + i), "name": f"FOREIGN CO {i}"} for i in range(12)]
    u.warn_if_mixed_market("009999", holdings)
    assert "::warning::009999" in capsys.readouterr().out
    u.warn_if_mixed_market("0050", [{"code": "2330", "name": "台積電"}])
    assert capsys.readouterr().out == ""
