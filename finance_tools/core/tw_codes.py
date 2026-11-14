"""台股代號種類——stock-data 的實作。

規則與 stock_map 的 `src/shared/lib/company.mjs`（`isTwStock`）、`scripts/lib/company_names.py`（`is_tw_stock`）
相同；兩個 repo 無法共用程式碼，由 stock_map 的 `npm run check:market` 拿整份名冊逐檔對帳。
不要在別處另寫 `len(code) == 4`、`code.isdigit()` 之類的判斷（`finance_tools/tests/test_tw_codes.py` 會擋）。
只用標準函式庫。
"""
import re

_TW_STOCK_RE = re.compile(r"[1-9]\d{3}")


def is_tw_stock(code: str) -> bool:
    """台股個股：四碼數字、首碼不是 0（2330）。ETF（00 開頭）、特別股（2881A）、權證、可轉債都不算。"""
    return bool(_TW_STOCK_RE.fullmatch(code or ""))
