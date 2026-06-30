"""
計算「大戶加碼排行」：比較各公司最近 N 期 TDCC 股權分散表，
輸出多個回溯期間的排行，供前端切換。

輸出: src/data/market/weekly_big_holders.json
"""

import json
from pathlib import Path
from datetime import datetime
from collections import Counter

BASE = Path(__file__).parent.parent.parent / "src/data"
FINANCIALS_DIR = BASE / "layer3/company-financials"
COMPANIES_ALL = BASE / "layer3/companies/companies-all.json"
OUTPUT_FILE = BASE / "market/weekly_big_holders.json"

THRESHOLDS = [200, 400, 800, 1000]  # 單位：張（千股）
FRESHNESS_DAYS = 45
MAX_LOOKBACK_PERIODS = 4
TOP_N = 20


def big_holder_ratio(snapshot: list, min_lots: int) -> tuple[float, int]:
    min_shares = min_lots * 1000
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
        if low_val >= min_shares:
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


def load_raw_company_data(display_names: dict, today: datetime) -> list[dict]:
    """Load raw history for all companies (threshold-independent)."""
    files = sorted(FINANCIALS_DIR.glob("*.json"))
    raw = []
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

        # Skip snapshots where total holders dropped >90% from previous period
        # (corporate action freeze, TDCC bad data, etc.)
        latest_snapshot = history[dates[-1]]
        total_people = sum(
            e.get("holder_count", 0) for e in latest_snapshot
            if e.get("holding_range") not in ("合計", "")
        )
        if len(dates) >= 2:
            prev_snapshot = history[dates[-2]]
            prev_total = sum(
                e.get("holder_count", 0) for e in prev_snapshot
                if e.get("holding_range") not in ("合計", "")
            )
            if prev_total > 100 and total_people < prev_total * 0.1:
                print(f"[big_holders] 跳過 {code}: 人數異常下降 {prev_total}→{total_people}，疑似公司行動或壞資料")
                continue

        raw.append({
            "code": code,
            "name": display_names.get(code, d.get("companyName", code)),
            "dates": dates,
            "history": history,
        })
    return raw


def compute_for_threshold(raw: list[dict], min_lots: int) -> list[dict]:
    """Build period rankings for a given lot threshold."""
    # Pre-compute latest ratio per company for this threshold
    company_data = []
    for c in raw:
        dates = c["dates"]
        ratio_latest, shares_latest = big_holder_ratio(c["history"][dates[-1]], min_lots)
        if ratio_latest == 0:
            continue
        company_data.append({**c, "ratioLatest": ratio_latest, "sharesLatest": shares_latest})

    periods_output = []
    for lb in range(1, MAX_LOOKBACK_PERIODS + 1):
        results = []
        for c in company_data:
            dates = c["dates"]
            if len(dates) <= lb:
                continue

            prev_date = dates[-(lb + 1)]
            ratio_prev, shares_prev = big_holder_ratio(c["history"][prev_date], min_lots)

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

        from_counter = Counter(r["fromDate"] for r in results)
        main_from = from_counter.most_common(1)[0][0] if from_counter else ""
        main_to = results[0]["toDate"] if results else ""

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
            # Ensure gainers and losers never overlap when len(results) < 2*TOP_N
            "topLosers": list(reversed(results[max(TOP_N, len(results) - TOP_N):])),
        })

        print(
            f"[big_holders] {min_lots}張 period-{lb}: {len(results)} companies | "
            f"{main_from} → {main_to} ({days}d) | "
            + (f"top: {results[0]['code']} {results[0]['name']} {results[0]['ratioChange']:+.2f}%"
               if results else "no data")
        )

    return periods_output


def compute(today: datetime = None) -> dict:
    if today is None:
        today = datetime.today()

    display_names = load_display_names()
    raw = load_raw_company_data(display_names, today)

    thresholds_output: dict[str, dict] = {}
    latest_date = ""

    for min_lots in THRESHOLDS:
        periods = compute_for_threshold(raw, min_lots)
        thresholds_output[str(min_lots)] = {"periods": periods}
        if not latest_date and periods:
            latest_date = periods[0].get("toDate", "")

    return {
        "generatedAt": today.strftime("%Y-%m-%dT%H:%M:%S"),
        "latestDate": latest_date,
        "thresholds": thresholds_output,
    }


def run(args=None):
    output = compute()
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"[big_holders] written → {OUTPUT_FILE}")


if __name__ == "__main__":
    run()
