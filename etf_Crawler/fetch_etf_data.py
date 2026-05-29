"""
fetch_etf_data.py

舊版手動同步腳本，現已不在 ETF GitHub Actions 主流程中使用。

從玩股網一次抓取全市場 ETF 資料，更新：
  1. src/data/etf/index.json         — 費用/規模/發行商/殖利率/持有人數/上市日期
  2. src/data/etf/dividends/{code}.json — 各 ETF 配息歷史

資料來源：
  排行頁 https://www.wantgoo.com/stock/etf/ranking/volume
    etfBasic      → managementFee, custodyFee, manager, tracing, category
    etfDailyValue → fundSize, last4SeasonYR, fee, name(短名), people
  配息頁 https://www.wantgoo.com/stock/etf/dividend
    dividend-data → stockNo, listingDate, dividends[{date, cashDividend, cashYield, price, period}]

注意：
  - categoryId / assetClass / trackingIndex / dividendFrequency 由人工維護，此腳本不修改。
  - 新增 ETF 靜態欄位填 null，待人工補齊。

用法：
    uv run etf_Crawler/fetch_etf_data.py
"""

import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Dict, List

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("缺少 playwright，請先執行：pip install playwright && playwright install chromium")
    sys.exit(1)

REPO_ROOT    = Path(__file__).parent.parent
INDEX_PATH   = REPO_ROOT / "src/data/etf/index.json"
DIV_DIR      = REPO_ROOT / "src/data/etf/dividends"
RANKING_URL  = "https://www.wantgoo.com/stock/etf/ranking/volume"
DIVIDEND_URL = "https://www.wantgoo.com/stock/etf/dividend"

_API_BASIC    = "wantgoo.com/stock/etf/basic-data"
_API_VALUE    = "wantgoo.com/stock/etf/daily-value-data"
_API_DIVIDEND = "wantgoo.com/stock/etf/dividend-data"

# 投信名稱正規化
_STRIP = re.compile(r"證券投資信託股份有限公司|投資信託股份有限公司|證券投資信託")
_ALIAS = {
    "元大": "元大投信", "富邦": "富邦投信", "國泰": "國泰投信",
    "中國信託": "中國信託投信", "永豐": "永豐投信", "群益": "群益投信",
    "台新": "台新投信", "統一": "統一投信", "野村": "野村投信",
    "安聯": "安聯投信", "復華": "復華投信", "凱基": "凱基投信",
    "第一金": "第一金投信", "新光": "新光投信", "華南": "華南投信",
    "合庫": "合庫投信", "玉山": "玉山投信", "日盛": "日盛投信",
    "聯邦": "聯邦投信", "兆豐": "兆豐投信", "台灣工銀": "台灣工銀投信",
    "富蘭克林": "富蘭克林投信", "施羅德": "施羅德投信",
    "摩根": "摩根投信", "貝萊德": "貝萊德投信",
    "聯博": "聯博投信", "霸菱": "霸菱投信", "柏瑞": "柏瑞投信",
}

def _issuer(manager: str) -> str:
    short = _STRIP.sub("", manager).strip()
    for key, val in _ALIAS.items():
        if short.startswith(key):
            return val
    return short + ("投信" if short and not short.endswith("投信") else "")


# ──────────────────────────────────────────────
# Playwright 擷取（一次開兩頁）
# ──────────────────────────────────────────────
def _fetch_all() -> tuple[list, list, list]:
    """回傳 (basic_list, value_list, dividend_list)"""
    max_attempts = 3
    last_err = None

    with sync_playwright() as p:
        for attempt in range(1, max_attempts + 1):
            captured: dict = {}
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage"],
            )
            try:
                ctx = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    locale="zh-TW",
                )
                page = ctx.new_page()
                page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )

                def on_response(response):
                    url = response.url
                    if _API_BASIC in url:
                        captured["basic"] = response.json()
                    elif _API_VALUE in url:
                        captured["value"] = response.json()
                    elif _API_DIVIDEND in url:
                        captured["dividend"] = response.json()

                page.on("response", on_response)
                deadline = 30

                # 排行頁
                print(f"  載入 {RANKING_URL} ...", flush=True)
                page.goto(RANKING_URL, wait_until="load", timeout=90000)
                for _ in range(deadline * 2):
                    if "basic" in captured and "value" in captured:
                        break
                    time.sleep(0.5)
                else:
                    snippet = page.evaluate("() => document.body?.innerText.slice(0,400) ?? ''")
                    raise TimeoutError(
                        f"排行頁 API 未在 {deadline}s 內到達 "
                        f"(captured={list(captured)}) body={snippet!r}"
                    )

                # 配息頁
                print(f"  載入 {DIVIDEND_URL} ...", flush=True)
                page.goto(DIVIDEND_URL, wait_until="load", timeout=90000)
                for _ in range(deadline * 2):
                    if "dividend" in captured:
                        break
                    time.sleep(0.5)
                else:
                    print("  [warn] dividend-data 未取得，inceptionDate/配息歷史 將略過", flush=True)

            except Exception as e:
                last_err = e
                print(f"  [attempt {attempt}/{max_attempts}] 失敗: {e}", flush=True)
                if attempt < max_attempts:
                    time.sleep(10)
                continue
            finally:
                browser.close()

            basic    = captured.get("basic", [])
            value    = captured.get("value", [])
            dividend = captured.get("dividend", [])
            print(f"  basic={len(basic)} value={len(value)} dividend={len(dividend)}")
            return basic, value, dividend

    raise RuntimeError(f"所有嘗試均失敗: {last_err}") from last_err


# ──────────────────────────────────────────────
# 更新 index.json
# ──────────────────────────────────────────────
def _update_index(basic_list: list, value_list: list, dividend_list: list) -> None:
    basic_map:    Dict[str, dict] = {e["stockNo"]: e for e in basic_list}
    value_map:    Dict[str, dict] = {e["stockNo"]: e for e in value_list}
    dividend_map: Dict[str, dict] = {e["stockNo"]: e for e in dividend_list}

    with open(INDEX_PATH, encoding="utf-8") as f:
        existing: List[dict] = json.load(f)
    existing_map: Dict[str, dict] = {e["code"]: e for e in existing}

    all_codes = set(basic_map) | set(value_map)
    result: List[dict] = []
    added = updated = unchanged = 0

    for code in sorted(all_codes):
        b  = basic_map.get(code, {})
        v  = value_map.get(code, {})
        d  = dividend_map.get(code, {})
        ex = existing_map.get(code)

        mgmt_fee  = v.get("fee") or ((b.get("managementFee") or 0) + (b.get("custodyFee") or 0)) or None
        inception = d.get("listingDate", "")[:10] if d.get("listingDate") else None

        dynamic = {
            "fundSize":        round(v["fundSize"] / 100, 2) if v.get("fundSize") else None,
            "managementFee":   round(mgmt_fee, 4) if mgmt_fee else None,
            "trailingYield":   v.get("last4SeasonYR"),
            "issuer":          _issuer(b["manager"]) if b.get("manager") else None,
            "beneficiaryCount": v.get("people") or None,
            "inceptionDate":   inception,
        }
        short_name = v.get("name") or None

        if ex:
            entry   = dict(ex)
            changed = False
            for field, val in dynamic.items():
                if val is not None and entry.get(field) != val:
                    entry[field] = val
                    changed = True
            if short_name and entry.get("name") != short_name:
                entry["name"] = short_name
                changed = True
            result.append(entry)
            updated   += changed
            unchanged += not changed
        else:
            # 新 ETF：靜態欄位留 null，待人工補齊
            entry = {
                "code":             code,
                "name":             short_name or b.get("name", code),
                "assetClass":       None,
                "categoryId":       None,
                "trackingIndex":    b.get("tracing") or None,
                "managementFee":    dynamic["managementFee"],
                "dividendFrequency": None,
                "inceptionDate":    dynamic["inceptionDate"],
                "fundSize":         dynamic["fundSize"],
                "issuer":           dynamic["issuer"],
                "trailingYield":    dynamic["trailingYield"],
                "beneficiaryCount": dynamic["beneficiaryCount"],
            }
            result.append(entry)
            added += 1

    result.sort(key=lambda e: e["code"])
    print(f"  index.json — 新增={added}  更新={updated}  無變化={unchanged}  合計={len(result)}")

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ──────────────────────────────────────────────
# 更新 dividends/{code}.json
# ──────────────────────────────────────────────
def _update_dividends(dividend_list: list) -> None:
    DIV_DIR.mkdir(parents=True, exist_ok=True)
    success = skipped = 0

    for etf in sorted(dividend_list, key=lambda e: e["stockNo"]):
        code      = etf["stockNo"]
        dividends = etf.get("dividends", [])
        if not dividends:
            skipped += 1
            continue

        entry = {
            "stockNo":     code,
            "listingDate": etf.get("listingDate", "")[:10] if etf.get("listingDate") else None,
            "lastUpdated": date.today().isoformat(),
            "dividends": [
                {
                    "date":         d.get("date", "")[:10] if d.get("date") else None,
                    "cashDividend": d.get("cashDividend"),
                    "cashYield":    d.get("cashYield"),
                    "price":        d.get("price"),
                    "period":       d.get("period"),
                }
                for d in dividends
            ],
        }
        out = DIV_DIR / f"{code}.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, indent=2)
        success += 1

    print(f"  dividends/ — 成功={success}  略過(無配息)={skipped}")


# ──────────────────────────────────────────────
def main() -> None:
    print("Step 1: 從玩股網抓取資料...")
    basic_list, value_list, dividend_list = _fetch_all()

    print("Step 2: 更新 index.json...")
    _update_index(basic_list, value_list, dividend_list)

    print("Step 3: 更新配息歷史...")
    _update_dividends(dividend_list)

    print("完成。")


if __name__ == "__main__":
    main()
