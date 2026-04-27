"""
計算「大戶加碼排行」：比較各公司最近兩期 TDCC 股權分散表，
找出大戶（400張以上）持股比例增加最多的個股。

輸出: src/data/market/weekly_big_holders.json
"""

import json
import glob
from pathlib import Path
from datetime import datetime, timedelta

BASE = Path(__file__).parent.parent.parent / "src/data"
FINANCIALS_DIR = BASE / "layer3/company-financials"
COMPANIES_ALL = BASE / "layer3/companies/companies-all.json"
OUTPUT_FILE = BASE / "market/weekly_big_holders.json"

# 大戶門檻：持股 400 張 (400,000股) 以上
BIG_HOLDER_MIN_SHARES = 400_000

# 只使用最新資料在 N 天內的公司
FRESHNESS_DAYS = 45

TOP_N = 20


def big_holder_ratio(snapshot: list) -> tuple[float, int]:
    """計算大戶持股比例(%)與總持股股數."""
    total_ratio = 0.0
    total_shares = 0
    for entry in snapshot:
        rng = entry.get("holding_range", "")
        if rng in ("合計", ""):
            continue
        low_str = rng.replace(",", "").split("-")[0].replace("以上", "")
        try:
            low_val = int(low_str)
        except ValueError:
            continue
        if low_val >= BIG_HOLDER_MIN_SHARES:
            total_ratio += entry.get("ratio_pct", 0.0)
            total_shares += int(entry.get("shares", 0))
    return round(total_ratio, 2), total_shares


def load_display_names() -> dict[str, str]:
    """從 companies-all.json 載入簡稱 (shortName)."""
    if not COMPANIES_ALL.exists():
        return {}
    with open(COMPANIES_ALL, encoding="utf-8") as f:
        data = json.load(f)
    return {code: info.get("shortName") or info.get("name", code)
            for code, info in data.items()}


def compute(today: datetime = None) -> dict:
    if today is None:
        today = datetime.today()

    display_names = load_display_names()
    files = sorted(FINANCIALS_DIR.glob("*.json"))

    results = []
    skipped_stale = 0
    skipped_no_history = 0

    for fp in files:
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue

        history = d.get("shareholderDataHistory", {})
        if len(history) < 2:
            skipped_no_history += 1
            continue

        dates = sorted(history.keys())
        latest_date = datetime.strptime(dates[-1], "%Y%m%d")

        if (today - latest_date).days > FRESHNESS_DAYS:
            skipped_stale += 1
            continue

        d1, d2 = dates[-2], dates[-1]
        ratio_from, shares_from = big_holder_ratio(history[d1])
        ratio_to, shares_to = big_holder_ratio(history[d2])

        # 跳過沒有大戶資料的公司
        if ratio_from == 0 and ratio_to == 0:
            continue

        code = d.get("companyCode", fp.stem)
        results.append({
            "code": code,
            "name": display_names.get(code, d.get("companyName", code)),
            "fromDate": d1,
            "toDate": d2,
            "ratioFrom": ratio_from,
            "ratioTo": ratio_to,
            "ratioChange": round(ratio_to - ratio_from, 2),
            "sharesChange": shares_to - shares_from,
        })

    results.sort(key=lambda x: x["ratioChange"], reverse=True)

    # 決定最常見的 from/to 日期（供 UI 顯示）
    from collections import Counter
    period_counter = Counter((r["fromDate"], r["toDate"]) for r in results)
    main_from, main_to = period_counter.most_common(1)[0][0] if period_counter else ("", "")

    output = {
        "generatedAt": today.strftime("%Y-%m-%dT%H:%M:%S"),
        "periodFrom": main_from,
        "periodTo": main_to,
        "freshnessWindowDays": FRESHNESS_DAYS,
        "totalAnalyzed": len(results),
        "topGainers": results[:TOP_N],
        "topLosers": results[-TOP_N:][::-1],
    }

    print(f"[big_holders] analyzed={len(results)}, stale_skipped={skipped_stale}, "
          f"no_history_skipped={skipped_no_history}")
    print(f"[big_holders] period: {main_from} → {main_to}")
    print(f"[big_holders] top gainer: {results[0]['code']} {results[0]['name']} "
          f"{results[0]['ratioChange']:+.2f}%" if results else "[big_holders] no results")

    return output


def run(args=None):
    output = compute()
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"[big_holders] written → {OUTPUT_FILE}")


if __name__ == "__main__":
    run()
