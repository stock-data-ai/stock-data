"""trading_day.py 必須「只用標準函式庫」就能跑。

workflow 用沒裝套件的 python3 直接執行它（daily-update 的休市日曆、處置預測的 is-closed 閘門、
health-check 的 import trading_day）。2026-11-15 它改 import finance_tools.core.timezone，連帶載入
finance_tools/core/__init__.py → api_client → requests，沒裝就崩；is-closed 閘門因此把休市日當成交易日照跑。

conftest 禁止開 subprocess，所以在同一個程序裡重現：第三方套件設成 import 就失敗，再以腳本模式
（模組不屬於任何套件、sys.path 有 core 資料夾）載入 trading_day.py。
"""
import importlib.util
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "core"


def test_script_mode_loads_without_third_party_packages(monkeypatch):
    for name in ("requests", "pandas", "FinMind", "tenacity", "certifi"):
        monkeypatch.setitem(sys.modules, name, None)          # import 時 ImportError
    for name in [m for m in sys.modules if m == "finance_tools" or m.startswith("finance_tools.")]:
        monkeypatch.delitem(sys.modules, name)
    for name in ("timezone", "roc_date", "trading_day_script"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.syspath_prepend(str(CORE))

    spec = importlib.util.spec_from_file_location("trading_day_script", CORE / "trading_day.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.is_tw_trading_day("2026-11-16") is True     # 週一
    assert mod.is_tw_trading_day("2026-11-15") is False    # 週日
    assert "finance_tools.core" not in sys.modules         # 沒有經過套件 __init__
