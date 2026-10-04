"""全市場基本面快照（橫斷面），供 stock_map 的基本面選股使用。

與 build_chip_history.py 同一個理由：`company-financials/{code}.json` 是逐檔的完整歷史，
選股要問的是「全市場誰的月營收創新高」，那是橫斷面問題，不該讓 stock_map 去 clone 這個 repo。
這支把每家擠成兩條短序列：

  rev  最近 REV_MONTHS 個月的月營收 `[yyyymm, 千元]`（算年增、連 N 月年增、近 12 月新高）
  q    歷季 `[yyyyq, EPS, 毛利率, 營益率, 淨利率]`（算 EPS 年增、創新高、轉虧為盈、三率三升）

**只收原始數字，不在這裡判斷條件**——條件的定義（門檻、幾個月）全在 stock_map 的
`scripts/fundamentals/strategies.py`，一個地方改就好，不會兩個 repo 各寫一份。

    uv run finance_tools/scripts/build_fundamentals_snapshot.py
    uv run finance_tools/scripts/build_fundamentals_snapshot.py --dry-run

輸出：src/data/market/fundamentals-snapshot.json（隨 repo 發到 GitHub Pages）

> [!NOTE]
> 月營收與季報由 weekly-financials-update（週日＋自動重跑）寫進財報檔；這支只讀磁碟上
> 已有的檔案，排在平日 margin-trading-update 裡跑，所以一週內任何一次財報更新都會在
> 當晚被收進快照。

> [!IMPORTANT]
> **上游的 `yoy` 欄位大多是 null**（2026-09-11 實測最新一個月 2,339 檔裡 2,200 檔是 null），
> 所以年增率一律由 stock_map 用序列自己算（本月 ÷ 去年同月），不讀 `yoy`。
"""

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent.parent.parent / "src/data"
FINANCIALS_DIR = BASE / "layer3/company-financials"
OUTPUT_FILE = BASE / "market/fundamentals-snapshot.json"

# 月營收保留幾個月。「連 3 個月年增」要 3 + 12 = 15 個月；多留到 24 讓之後加條件
# （例如連 6 個月年增）不必改這邊。
REV_MONTHS = 24


def num(v, digits=2):
    """數字才留，其餘一律 None（NaN 也當 None——Node 讀不回 NaN）。"""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    f = float(v)
    if f != f:                       # NaN
        return None
    return int(f) if f.is_integer() else round(f, digits)


def collect():
    files = sorted(FINANCIALS_DIR.glob("*.json"))
    print(f"掃描 {len(files)} 個檔案…")
    codes = {}
    latest_month, latest_quarter = Counter(), Counter()
    skipped = {"parse": 0, "empty": 0}
    t0 = time.time()

    for i, fp in enumerate(files, 1):
        if i % 500 == 0:
            print(f"  …{i}/{len(files)}（{time.time() - t0:.0f}s）", flush=True)
        try:
            with open(fp, encoding="utf-8") as f:
                doc = json.load(f)
        except Exception:                                   # noqa: BLE001
            skipped["parse"] += 1
            continue

        hist = doc.get("historical") or {}
        code = doc.get("companyCode") or fp.stem
        entry = {}

        months = {}
        for r in hist.get("monthlyRevenue") or []:
            y, m, v = r.get("year"), r.get("month"), num(r.get("revenue"))
            if isinstance(y, int) and isinstance(m, int) and v is not None:
                # 千元：原值是元，12 位數的整數每筆多吃 3 個字元，2,000 檔 × 24 個月就是 140KB
                months[y * 100 + m] = round(v / 1000)
        if months:
            keys = sorted(months)[-REV_MONTHS:]
            entry["rev"] = [[k, months[k]] for k in keys]
            latest_month[keys[-1]] += 1

        quarters = {}
        for r in hist.get("quarterly") or []:
            y, q = r.get("year"), r.get("quarter")
            if not (isinstance(y, int) and isinstance(q, int)):
                continue
            row = [num(r.get("eps")), num(r.get("grossMargin")),
                   num(r.get("operatingMargin")), num(r.get("netMargin"))]
            if any(v is not None for v in row):
                quarters[y * 10 + q] = row
        if quarters:
            keys = sorted(quarters)
            entry["q"] = [[k, *quarters[k]] for k in keys]
            latest_quarter[keys[-1]] += 1

        if entry:
            codes[code] = entry
        else:
            skipped["empty"] += 1

    if not codes:
        raise SystemExit("[fundamentals] 一檔都沒讀到，中止")

    # 「最新一期」取多數公司所在的那一期，不取最大值：少數公司提早公布（或上游誤植未來月份）
    # 會讓全市場的「最新」往前跳一格，其餘兩千家都被判成資料落後。
    rev_month = latest_month.most_common(1)[0][0] if latest_month else None
    eps_quarter = latest_quarter.most_common(1)[0][0] if latest_quarter else None

    print(f"\n讀檔耗時 {time.time() - t0:.0f}s")
    print(f"略過：解析失敗 {skipped['parse']}、無營收也無季報 {skipped['empty']}")
    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "source": "company-financials（月營收、季報）",
        "units": {"rev": "千元", "eps": "元", "margins": "%"},
        "revenueMonth": rev_month,        # yyyymm
        "epsQuarter": eps_quarter,        # yyyyq
        "revenueMonthCoverage": latest_month.get(rev_month, 0),
        "epsQuarterCoverage": latest_quarter.get(eps_quarter, 0),
        "codes": codes,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    out = collect()
    n = len(out["codes"])
    print(f"\n結果：{n} 檔；最新月營收 {out['revenueMonth']}（{out['revenueMonthCoverage']} 檔）、"
          f"最新季報 {out['epsQuarter']}（{out['epsQuarterCoverage']} 檔）")

    if args.dry_run:
        print("\n--dry-run，不寫檔")
        return

    # 護欄：檔數比上一版掉超過三成就不寫，疑似上游殘缺（比照 chip-history）。
    try:
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            prev = len(json.load(f).get("codes", {}))
    except (FileNotFoundError, ValueError):
        prev = 0
    if prev and n < prev * 0.7:
        raise SystemExit(f"[fundamentals] 檔數從 {prev} 掉到 {n}（-{(1 - n / prev) * 100:.0f}%），不寫檔")

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    print(f"\n→ {OUTPUT_FILE}（{OUTPUT_FILE.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
