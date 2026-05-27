#!/usr/bin/env python3
"""
Fetch US stock financial data from Yahoo Finance (yfinance).
Outputs to src/data/layer3/company-financials-us/{CODE}.json
Same schema as company-financials/{code}.json. All monetary values in USD 億.

Non-USD companies (e.g. ASML/EUR) are converted to USD at fetch time.
Companies whose financialCurrency is TWD are skipped (already in company-financials/).

Usage:
    uv run finance_tools/us_financials/fetch_us_financials.py ARM NVDA AAPL
    uv run finance_tools/us_financials/fetch_us_financials.py --all-topics
    uv run finance_tools/us_financials/fetch_us_financials.py --delay 2.0 ARM NVDA
"""

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

PROJECT_ROOT = Path(__file__).parent.parent.parent
OUTPUT_DIR = PROJECT_ROOT / "src" / "data" / "layer3" / "company-financials-us"
US_TICKERS_FILE = PROJECT_ROOT / "src" / "data" / "layer3" / "us-tickers.json"

# Companies that report in TWD are already covered by the Taiwan stock pipeline
SKIP_FINANCIAL_CURRENCIES = {"TWD"}

# Exchange rate cache: { "EUR": 1.08, ... }
_fx_cache: dict = {}


def get_fx_rate(from_currency: str) -> float:
    """Return 1 unit of from_currency in USD. Returns 1.0 if already USD."""
    if from_currency == "USD":
        return 1.0
    if from_currency in _fx_cache:
        return _fx_cache[from_currency]
    ticker = f"{from_currency}USD=X"
    try:
        rate = yf.Ticker(ticker).info.get("regularMarketPrice") or 1.0
        _fx_cache[from_currency] = float(rate)
        return float(rate)
    except Exception:
        return 1.0


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


def _to_usd(val, fx: float = 1.0):
    """Raw value × fx_rate → raw USD (same unit as TW financials use for NTD)."""
    if val is None:
        return None
    return round(val * fx)


def build_annual(ticker_obj, fx: float):
    df = ticker_obj.income_stmt
    if df is None or df.empty:
        return []

    rows = []
    for col in df.columns:
        revenue = _v(df[col], "Total Revenue")
        if revenue is None:
            continue
        gross_profit = _v(df[col], "Gross Profit")
        op_income = _v(df[col], "Operating Income")
        net_income = _v(df[col], "Net Income")
        eps = _v(df[col], "Diluted EPS") or _v(df[col], "Basic EPS")

        gm = _r2(gross_profit / revenue * 100) if gross_profit and revenue else None
        om = _r2(op_income / revenue * 100) if op_income and revenue else None
        nm = _r2(net_income / revenue * 100) if net_income and revenue else None

        rows.append({
            "year": col.year,
            "revenue": _to_usd(revenue, fx),
            "grossProfit": _to_usd(gross_profit, fx),
            "operatingIncome": _to_usd(op_income, fx),
            "netIncome": _to_usd(net_income, fx),
            "eps": _r2(eps),         # EPS stays in original currency (per-share)
            "grossMargin": gm,
            "operatingMargin": om,
            "netMargin": nm,
        })

    rows.sort(key=lambda x: x["year"], reverse=True)
    for i, row in enumerate(rows):
        if i + 1 < len(rows):
            prev = rows[i + 1].get("revenue")
            if row["revenue"] and prev:
                row["revenueYoY"] = round((row["revenue"] / prev - 1) * 100, 1)
    return rows


def build_quarterly(ticker_obj, fx: float):
    df = ticker_obj.quarterly_income_stmt
    if df is None or df.empty:
        return []

    rows = []
    for col in df.columns:
        revenue = _v(df[col], "Total Revenue")
        if revenue is None:
            continue
        gross_profit = _v(df[col], "Gross Profit")
        op_income = _v(df[col], "Operating Income")
        net_income = _v(df[col], "Net Income")
        eps = _v(df[col], "Diluted EPS") or _v(df[col], "Basic EPS")

        gm = _r2(gross_profit / revenue * 100) if gross_profit and revenue else None
        om = _r2(op_income / revenue * 100) if op_income and revenue else None
        nm = _r2(net_income / revenue * 100) if net_income and revenue else None

        rows.append({
            "year": col.year,
            "quarter": (col.month - 1) // 3 + 1,
            "revenue": _to_usd(revenue, fx),
            "grossProfit": _to_usd(gross_profit, fx),
            "operatingIncome": _to_usd(op_income, fx),
            "netIncome": _to_usd(net_income, fx),
            "eps": _r2(eps),
            "grossMargin": gm,
            "operatingMargin": om,
            "netMargin": nm,
        })

    rows.sort(key=lambda x: (x["year"], x["quarter"]), reverse=True)
    return rows


def build_output(code: str, ticker_obj):
    info = ticker_obj.info or {}
    fin_currency = info.get("financialCurrency") or "USD"

    if fin_currency in SKIP_FINANCIAL_CURRENCIES:
        return None, f"skip ({fin_currency} covered by TW pipeline)"

    fx = get_fx_rate(fin_currency)
    fx_note = f" [fx {fin_currency}→USD={fx:.4f}]" if fin_currency != "USD" else ""

    annual = build_annual(ticker_obj, fx)
    quarterly = build_quarterly(ticker_obj, fx)

    if not annual and not quarterly:
        return None, "no financial data"

    latest_q = quarterly[0] if quarterly else {}
    latest_a = annual[0] if annual else {}

    yoy = None
    if len(annual) >= 2 and annual[0].get("revenue") and annual[1].get("revenue"):
        yoy = round((annual[0]["revenue"] / annual[1]["revenue"] - 1) * 100, 1)

    # Margins from info are fractions; fall back to computed values from statements
    gross_margin = _pct(info.get("grossMargins")) or latest_q.get("grossMargin") or latest_a.get("grossMargin")
    op_margin = _pct(info.get("operatingMargins")) or latest_q.get("operatingMargin") or latest_a.get("operatingMargin")
    net_margin = _pct(info.get("profitMargins")) or latest_q.get("netMargin") or latest_a.get("netMargin")

    # marketCap from info is always in the trading currency (USD for US-listed)
    market_cap = _to_usd(info.get("marketCap"))  # no fx — already USD

    latest = {
        "year": latest_q.get("year") or latest_a.get("year"),
        "quarter": latest_q.get("quarter"),
        "revenue": latest_q.get("revenue") or latest_a.get("revenue"),
        "yoy": yoy,
        "grossMargin": gross_margin,
        "operatingMargin": op_margin,
        "netMargin": net_margin,
        "eps": latest_q.get("eps") or latest_a.get("eps"),
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
    }
    return result, fx_note


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
                with open(out, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
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


if __name__ == "__main__":
    main()
