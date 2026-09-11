#!/usr/bin/env python3
"""
Fetch US stock financial data from Yahoo Finance (yfinance).
Outputs to src/data/layer3/company-financials-us/{CODE}.json
Same schema as company-financials/{code}.json. Monetary fields are raw USD.

Non-USD companies (e.g. ASML/EUR): the reporting-currency figures are kept in each
record's `native`, and the USD fields are recomputed from them on every run with
that run's rate (`fxRateToUSD`) — all years at once, so old years never keep a stale rate.
If the rate can't be fetched, that company is skipped and its file left untouched.
Companies whose financialCurrency is TWD are skipped (already in company-financials/).

Usage:
    uv run finance_tools/us_financials/fetch_us_financials.py ARM NVDA AAPL
    uv run finance_tools/us_financials/fetch_us_financials.py --all-topics
    uv run finance_tools/us_financials/fetch_us_financials.py --delay 2.0 ARM NVDA
"""

import argparse
import json
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

PROJECT_ROOT = Path(__file__).parent.parent.parent
# 這支是用 `uv run finance_tools/us_financials/fetch_us_financials.py` 直接執行的，
# sys.path[0] 是它自己的目錄，不是 repo root——不補這行就 import 不到 finance_tools。
sys.path.insert(0, str(PROJECT_ROOT))

from finance_tools.core import merge_policy  # noqa: E402
OUTPUT_DIR = PROJECT_ROOT / "src" / "data" / "layer3" / "company-financials-us"
US_TICKERS_FILE = PROJECT_ROOT / "src" / "data" / "layer3" / "us-tickers.json"

# Companies that report in TWD are already covered by the Taiwan stock pipeline
SKIP_FINANCIAL_CURRENCIES = {"TWD"}

# Exchange rate cache: { "EUR": 1.08, ... }
_fx_cache: dict = {}


def write_json_atomic(path: Path, data) -> None:
    """先寫暫存檔再 os.replace，與台股 `FileManager.save_financial_data` 同一個保證：
    寫到一半失敗時，原本那份已發布的 JSON 不會被截成半個檔。
    這些檔案會發到 GitHub Pages 給 App 讀，截斷等於下游直接解析失敗。"""
    fd, temp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}_", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def get_fx_rate(from_currency: str) -> float:
    """1 單位 from_currency 等於多少 USD。取不到就 **raise**，不可以當成 1。

    舊版取不到時回 1.0：日圓就被當成美元存進去（Sony 營收會變成 13 兆美元），
    而且畫面上看不出來。現在所有年度每次都用這個匯率重算，當成 1 的傷害會擴及整份檔，
    所以寧可這一家這次不更新（舊檔原封不動，下一次再試）。
    """
    if from_currency == "USD":
        return 1.0
    if from_currency in _fx_cache:
        return _fx_cache[from_currency]
    rate = yf.Ticker(f"{from_currency}USD=X").info.get("regularMarketPrice")
    if not rate or rate <= 0:
        raise RuntimeError(f"{from_currency}→USD 匯率取不到")
    _fx_cache[from_currency] = float(rate)
    return float(rate)


def _v(col, row: str):
    """Get scalar from a DataFrame column (Series), None if missing/NaN."""
    if row not in col.index:
        return None
    val = col.loc[row]
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    return float(val)


def _pct(val):
    """Fraction → percentage (0.66 → 66.0), 2 dp."""
    if val is None:
        return None
    return round(val * 100, 2)


def _r2(val):
    if val is None:
        return None
    return round(val, 2)


def _first_present(*values):
    """第一個「不是 None」的值。與 `or` 的差別在於 0 會被當成有效值留下來。"""
    for v in values:
        if v is not None:
            return v
    return None


def _to_usd(val, fx: float = 1.0):
    """Raw value × fx_rate → raw USD (same unit as TW financials use for NTD)."""
    if val is None:
        return None
    return round(val * fx)


#: 金額欄位。**原幣**存在每筆紀錄的 `native` 裡、永遠不動；同名的美元欄位每次執行都用
#: 當次匯率從原幣重算（`apply_fx`），**所有年度一起算**。
#:
#: 為什麼不直接只存原幣：App 顯示金額時只有「億／兆」，沒有幣別；已上架的舊版 App 又改不到，
#: 日圓直接存進去會被讀成美元（Sony 營收 13 兆日圓 → 畫面上的「13 兆」）。
#: 為什麼不只存美元：累積合併後，掉出 yfinance 4 年視窗的舊年度會停在當時的匯率，
#: 跟新年度的匯率不同，長期圖表就會出現「匯率造成的假成長」。兩份都存，兩個問題都沒有。
#: EPS 與各項 margin 不在這裡：EPS 一直是原幣（每股）、margin 是比率，都跟匯率無關。
MONEY_FIELDS = ("revenue", "grossProfit", "operatingIncome", "netIncome")


def _round(val):
    return None if val is None else round(val)


def _income_row(col):
    """一個期別的損益 → 紀錄（美元欄位先留空，由 `apply_fx` 填）。沒有營收就回 None。"""
    revenue = _v(col, "Total Revenue")
    if revenue is None:
        return None
    gross_profit = _v(col, "Gross Profit")
    op_income = _v(col, "Operating Income")
    net_income = _v(col, "Net Income")
    eps = _first_present(_v(col, "Diluted EPS"), _v(col, "Basic EPS"))
    native = {"revenue": revenue, "grossProfit": gross_profit,
              "operatingIncome": op_income, "netIncome": net_income}
    return {
        **{f: None for f in MONEY_FIELDS},   # 先佔位，維持既有檔案的欄位順序
        "eps": _r2(eps),                     # 原幣（每股），幣別見檔案頂層 financialCurrency
        "grossMargin": _r2(gross_profit / revenue * 100) if gross_profit and revenue else None,
        "operatingMargin": _r2(op_income / revenue * 100) if op_income and revenue else None,
        "netMargin": _r2(net_income / revenue * 100) if net_income and revenue else None,
        "native": {f: _round(v) for f, v in native.items()},
    }


def apply_fx(rows, fx: float):
    """用同一個匯率把每筆的原幣金額換成美元，寫進同名欄位。回傳 rows 本身。

    沒有 `native` 的紀錄（2026-09-11 以前寫入的）無從重算，原樣保留。
    """
    for row in rows or []:
        native = row.get("native")
        if not native:
            continue
        for field in MONEY_FIELDS:
            row[field] = _to_usd(native.get(field), fx)
    return rows


def build_annual(ticker_obj):
    df = ticker_obj.income_stmt
    if df is None or df.empty:
        return []

    rows = []
    for col in df.columns:
        row = _income_row(df[col])
        if row is not None:
            rows.append({"year": col.year, **row})

    rows.sort(key=lambda x: x["year"], reverse=True)
    return fill_revenue_yoy(rows)


def fill_revenue_yoy(annual):
    """年營收年增率：用**原幣**、只跟**前一個年度**比（中間缺年就不算）。

    合併後也要再跑一次：yfinance 只給 4 年，最舊那年在抓取當下沒有前一年可比；
    累積下來之後前一年就在檔裡了，這時才補得出來。沒有原幣的舊紀錄維持原值。
    """
    by_year = {a["year"]: a for a in annual or []}
    for row in annual or []:
        prev = by_year.get(row["year"] - 1)
        cur_rev = (row.get("native") or {}).get("revenue")
        prev_rev = ((prev or {}).get("native") or {}).get("revenue")
        if cur_rev and prev_rev:
            row["revenueYoY"] = round((cur_rev / prev_rev - 1) * 100, 1)
    return annual


def build_quarterly(ticker_obj):
    df = ticker_obj.quarterly_income_stmt
    if df is None or df.empty:
        return []

    rows = []
    for col in df.columns:
        row = _income_row(df[col])
        if row is not None:
            rows.append({"year": col.year, "quarter": (col.month - 1) // 3 + 1, **row})

    rows.sort(key=lambda x: (x["year"], x["quarter"]), reverse=True)
    return rows


def build_output(code: str, ticker_obj):
    info = ticker_obj.info or {}
    fin_currency = info.get("financialCurrency") or "USD"

    if fin_currency in SKIP_FINANCIAL_CURRENCIES:
        return None, f"skip ({fin_currency} covered by TW pipeline)"

    fx = get_fx_rate(fin_currency)
    fx_note = f" [fx {fin_currency}→USD={fx:.4f}]" if fin_currency != "USD" else ""

    annual = apply_fx(build_annual(ticker_obj), fx)
    quarterly = apply_fx(build_quarterly(ticker_obj), fx)

    if not annual and not quarterly:
        return None, "no financial data"

    latest_q = quarterly[0] if quarterly else {}
    latest_a = annual[0] if annual else {}

    yoy = None
    if len(annual) >= 2 and annual[0].get("revenue") and annual[1].get("revenue"):
        yoy = round((annual[0]["revenue"] / annual[1]["revenue"] - 1) * 100, 1)

    # Margins from info are fractions; fall back to computed values from statements.
    # 用 `is not None` 而不是 `or`：毛利率真的是 0 的時候，`or` 會把它當成缺值
    # 往下一個來源掉，畫面上就變成別的期別的數字。
    gross_margin = _first_present(_pct(info.get("grossMargins")), latest_q.get("grossMargin"), latest_a.get("grossMargin"))
    op_margin = _first_present(_pct(info.get("operatingMargins")), latest_q.get("operatingMargin"), latest_a.get("operatingMargin"))
    net_margin = _first_present(_pct(info.get("profitMargins")), latest_q.get("netMargin"), latest_a.get("netMargin"))

    # marketCap from info is always in the trading currency (USD for US-listed)
    market_cap = _to_usd(info.get("marketCap"))  # no fx — already USD

    latest = {
        "year": _first_present(latest_q.get("year"), latest_a.get("year")),
        "quarter": latest_q.get("quarter"),
        "revenue": _first_present(latest_q.get("revenue"), latest_a.get("revenue")),
        "yoy": yoy,
        "grossMargin": gross_margin,
        "operatingMargin": op_margin,
        "netMargin": net_margin,
        "eps": _first_present(latest_q.get("eps"), latest_a.get("eps")),
        "pe": _r2(info.get("trailingPE")),
        "pb": _r2(info.get("priceToBook")),
        "marketCap": market_cap,
    }

    result = {
        "companyCode": code,
        "companyName": info.get("longName") or info.get("shortName") or code,
        "latest": latest,
        "historical": {
            "annual": annual,
            "quarterly": quarterly,
        },
        "lastUpdated": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"),
        "dataQuality": "medium",
        "dataSource": "yahoo-finance",
        # 報表幣別要存下來。累積合併之後，若某公司改了報表幣別（例如 EUR→USD），
        # 舊年度是用舊匯率換算的，跟新年度併在一起就是兩種幣別混在同一個檔裡，
        # 而且看不出來。存了才擋得掉——見 `merge_us_output`。
        "financialCurrency": fin_currency,
        # 這次換算用的匯率（1 單位 financialCurrency = ? USD）。合併時用它把舊年度一起重算。
        "fxRateToUSD": fx,
    }
    return result, fx_note


def merge_us_output(existing, fresh):
    """把這次抓到的併進既有檔案，而不是整檔覆寫。

    2026-09-10 之前是覆寫：yfinance 給幾年就是幾年（實測 201 檔**全部**恰好
    4 年年報、5 個季度），而且來源只回半套（有 annual、沒 quarterly）時，
    既有的 quarterly 會被清空。改成與台股同一套 `merge_by_key`：
    同期間取新、視窗外的舊期間保留、這次沒抓到就原樣留著。

    合併完再用這次的匯率把**所有年度**從原幣重算成美元（`apply_fx`），
    所以留下來的舊年度不會停在當年抓取時的匯率。
    """
    if not existing:
        return fresh

    old_currency = existing.get("financialCurrency")
    new_currency = fresh.get("financialCurrency")
    if old_currency and new_currency and old_currency != new_currency:
        # 幣別換了就不能併——舊年度的原幣是另一種貨幣，用新幣別的匯率重算會錯。
        # 整份重建（就是 2026-09-10 以前的行為），讓所有年度回到同一個幣別。
        print(f" [報表幣別 {old_currency}→{new_currency}，整份重建]", end="")
        return fresh

    merged = dict(existing)
    merged.update({k: v for k, v in fresh.items() if k != "historical"})

    old_hist = existing.get("historical") or {}
    new_hist = fresh.get("historical") or {}
    merged["historical"] = {
        "annual": merge_policy.merge_by_key(
            old_hist.get("annual"), new_hist.get("annual"), key=lambda a: a["year"]),
        "quarterly": merge_policy.merge_by_key(
            old_hist.get("quarterly"), new_hist.get("quarterly"),
            key=lambda q: (q["year"], q["quarter"])),
    }
    fx = fresh.get("fxRateToUSD")
    if fx:
        apply_fx(merged["historical"]["annual"], fx)
        apply_fx(merged["historical"]["quarterly"], fx)
    fill_revenue_yoy(merged["historical"]["annual"])
    return merged


def load_existing(path: Path):
    """讀既有檔；不存在或讀不回來（損壞）都回 `{}`，也就是整檔重建。

    與台股不同，這裡損壞就直接重建：美股只有這一支寫入者，沒有「其他人要退開」的問題；
    代價是視窗外累積的舊年度會跟著壞檔一起丟失（要救就從 git 拿上一版）。
    """
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f" [既有檔損壞 {e}；改為整檔重建]", end="")
        return {}


def load_us_topic_codes():
    with open(US_TICKERS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return data["tickers"]


def main():
    parser = argparse.ArgumentParser(description="Fetch US stock financials → 億 USD JSON")
    parser.add_argument("codes", nargs="*", help="US tickers (e.g. ARM NVDA AAPL)")
    parser.add_argument("--all-topics", action="store_true", help="All US companies with topics")
    parser.add_argument("--delay", type=float, default=1.5, help="Delay between requests (s)")
    args = parser.parse_args()

    if args.all_topics:
        codes = load_us_topic_codes()
        print(f"Found {len(codes)} US companies with topics")
    elif args.codes:
        codes = [c.upper() for c in args.codes]
    else:
        parser.print_help()
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    ok = skipped = errors = 0
    for i, code in enumerate(codes):
        print(f"[{i+1}/{len(codes)}] {code} ...", end="", flush=True)
        try:
            t = yf.Ticker(code)
            result, note = build_output(code, t)

            if result is None:
                print(f" SKIP {note}")
                skipped += 1
            else:
                out = OUTPUT_DIR / f"{code}.json"
                write_json_atomic(out, merge_us_output(load_existing(out), result))
                rev = result["latest"].get("revenue")
                gm = result["latest"].get("grossMargin")
                n = len(result["historical"]["annual"])
                rev_b = round(rev / 1e9, 2) if rev else None
                print(f" OK  rev={rev_b}B USD  gm={gm}%  {n}yr{note}")
                ok += 1

        except Exception as e:
            print(f" ERROR: {e}")
            errors += 1

        if i + 1 < len(codes):
            time.sleep(args.delay)

    print(f"\nDone: {ok} saved, {skipped} skipped, {errors} errors → {OUTPUT_DIR}")

    # 一檔都沒寫成卻回報成功，等於 CI 永遠綠燈、沒有人會知道美股資料停更了。
    # `skipped` 是正常的（TWD 報表由台股管線負責、或該檔真的沒有財報），
    # 所以判準是「有東西要處理，卻一個都沒成功」。
    if codes and ok == 0:
        print(f"FAILED: {len(codes)} 檔都沒有寫入（skipped {skipped} / errors {errors}）")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
