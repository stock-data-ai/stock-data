"""
計算「大戶加碼排行」：比較各公司最近 N 期 TDCC 股權分散表，
輸出多個回溯期間的排行，供前端切換。

輸出: src/data/market/weekly_big_holders.json
"""

import json
import glob
from pathlib import Path
from datetime import datetime
from collections import Counter

BASE = Path(__file__).parent.parent.parent / "src/data"
FINANCIALS_DIR = BASE / "layer3/company-financials"
COMPANIES_ALL = BASE / "layer3/companies/companies-all.json"
OUTPUT_FILE = BASE / "market/weekly_big_holders.json"

BIG_HOLDER_MIN_SHARES = 400_000
FRESHNESS_DAYS = 45
MAX_LOOKBACK_PERIODS = 4  # 最多往回比較幾期
TOP_N = 20


def big_holder_ratio(snapshot: list) -> tuple[float, int]:
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

    # 收集每家公司的歷史大戶比例（依期數索引）
    company_data: list[dict] = []

    for fp in files:
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue

        history = d.get("shareholderDataHistory", {})
        if len(history) < 2:
            continue

        dates = sorted(history.keys())
        latest_date = datetime.strptime(dates[-1], "%Y%m%d")

        if (today - latest_date).days > FRESHNESS_DAYS:
            continue

        code = d.get("companyCode", fp.stem)
        name = display_names.get(code, d.get("companyName", code))

        # 計算最新期的大戶比例與股數
        ratio_latest, shares_latest = big_holder_ratio(history[dates[-1]])
        if ratio_latest == 0:
            continue

        company_data.append({
            "code": code,
            "name": name,
            "dates": dates,          # 所有可用日期（升序）
            "ratioLatest": ratio_latest,
            "sharesLatest": shares_latest,
            "history": history,
        })

    # 建立各回溯期的排行
    periods_output = []
    for lb in range(1, MAX_LOOKBACK_PERIODS + 1):
        results = []
        for c in company_data:
            dates = c["dates"]
            if len(dates) <= lb:
                continue  # 沒有足夠期數的跳過

            prev_date = dates[-(lb + 1)]
            ratio_prev, shares_prev = big_holder_ratio(c["history"][prev_date])

            results.append({
                "code": c["code"],
                "name": c["name"],
                "fromDate": prev_date,
                "toDate": dates[-1],
                "ratioFrom": ratio_prev,
                "ratioTo": c["ratioLatest"],
                "ratioChange": round(c["ratioLatest"] - ratio_prev, 2),
                "sharesChange": c["sharesLatest"] - shares_prev,
            })

        results.sort(key=lambda x: x["ratioChange"], reverse=True)

        # 計算最常見的 from/to（供顯示）
        from_counter = Counter(r["fromDate"] for r in results)
        main_from = from_counter.most_common(1)[0][0] if from_counter else ""
        main_to = results[0]["toDate"] if results else ""

        # 計算天數差
        if main_from and main_to:
            days = (datetime.strptime(main_to, "%Y%m%d")
                    - datetime.strptime(main_from, "%Y%m%d")).days
        else:
            days = 0

        periods_output.append({
            "id": f"{lb}p",
            "label": f"近 {lb} 期",
            "days": days,
            "fromDate": main_from,
            "toDate": main_to,
            "totalAnalyzed": len(results),
            "topGainers": results[:TOP_N],
            "topLosers": list(reversed(results[-TOP_N:])),
        })

        print(f"[big_holders] period-{lb}: {len(results)} companies | "
              f"{main_from} → {main_to} ({days}d) | "
              f"top: {results[0]['code']} {results[0]['name']} "
              f"{results[0]['ratioChange']:+.2f}%" if results else f"[big_holders] period-{lb}: no data")

    output = {
        "generatedAt": today.strftime("%Y-%m-%dT%H:%M:%S"),
        "latestDate": periods_output[0]["toDate"] if periods_output else "",
        "periods": periods_output,
    }
    return output


def run(args=None):
    output = compute()
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"[big_holders] written → {OUTPUT_FILE}")


if __name__ == "__main__":
    run()
