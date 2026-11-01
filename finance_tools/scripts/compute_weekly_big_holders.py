"""
計算「大戶加碼排行」：比較各公司最近 N 期 TDCC 股權分散表，
輸出多個回溯期間的排行，供前端切換。

輸出: src/data/market/weekly_big_holders.json

**TDCC 的宇宙不等於可交易的宇宙**，兩道過濾都不能省：

1. 股權分散表涵蓋興櫃，而且在個股終止買賣之後還會繼續發佈——2026-09 優你康(4150)
   9/3 終止興櫃買賣，TDCC 9/4 那期照發，45 天 freshness 一路過關，於是一檔買不到的
   股票掛在榜首。所以名單先跟證交所／櫃買的開放資料交易清單取交集。
2. 這張榜比的是「大戶持股比例」，分母是總股數。增資／減資那一週，比例會在沒有任何人
   買賣的情況下整段位移——國璽幹細胞(6704) 2026-09-04 增資 97.0M→106.8M 股，新股直接
   落在千張級距，榜上看起來是「大戶加碼 9,747 張」。跨股本變動的期間一律不比。
"""

# 專案支援到 Python 3.9（pyproject requires-python >=3.9），`float | None` 這類註記
# 在 3.9 執行期會炸；延後求值讓它只是字串。
from __future__ import annotations

import json
import statistics
from pathlib import Path
from datetime import datetime
from collections import Counter

import requests

from finance_tools.utils.http_fetch import get_json as http_get_json
from finance_tools.core.file_manager import resolve_companies_all_path
from finance_tools.utils.retry import retry as _retry
from finance_tools.utils.twse_url import bust

BASE = Path(__file__).parent.parent.parent / "src/data"
FINANCIALS_DIR = BASE / "layer3/company-financials"
OUTPUT_FILE = BASE / "market/weekly_big_holders.json"

THRESHOLDS = [200, 400, 800, 1000]  # 單位：張（千股）
FRESHNESS_DAYS = 45
MAX_LOOKBACK_PERIODS = 4
TOP_N = 20

# 可交易宇宙：政府資料開放授權，可商業利用（見 stock_map CLAUDE.md 的資料源判準）。
# 兩支都是「最新一個交易日、全市場一次回傳」，不吃日期參數。
TWSE_DAY_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_QUOTES_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
# 合理下界（2026-09 實測：上市 1382、上櫃 1014）。低於此視為抓到壞快取或半套資料，
# 寧可整批失敗保留上一版，也不要靜靜地放行整批興櫃。
MIN_TWSE_CODES = 900
MIN_TPEX_CODES = 700

# 兩期之間推估總股數的容許變動；超過即視為股本變動，該期不比。
# 0.03 是雜訊上界的數十倍：ratio_pct 只到小數第 2 位，反推總股數的誤差實測 <0.1%。
CAPITAL_CHANGE_TOL = 0.03

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.tpex.org.tw/",
}


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


def _fetch_json(url: str) -> list:
    if "tpex.org.tw" in url:
        # 櫃買大檔會傳到一半斷線，要用續傳版（見 finance_tools/utils/http_fetch.py）
        return http_get_json(bust(url), headers=_HEADERS, timeout=30)
    resp = requests.get(bust(url), headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def load_tradable_codes() -> set[str]:
    """目前仍在上市／上櫃交易的股票代號。

    抓不到或數量明顯不足時直接拋出——寧可讓這班失敗、保留上一版排行，
    也不要在沒有過濾的情況下把興櫃與已終止買賣的個股寫進榜單（那是靜默錯誤）。
    """
    twse_rows = _retry(lambda: _fetch_json(TWSE_DAY_ALL_URL), "TWSE STOCK_DAY_ALL")
    tpex_rows = _retry(lambda: _fetch_json(TPEX_QUOTES_URL), "TPEx tpex_mainboard_quotes")

    twse = {r["Code"] for r in (twse_rows or []) if r.get("Code")}
    tpex = {r["SecuritiesCompanyCode"] for r in (tpex_rows or []) if r.get("SecuritiesCompanyCode")}

    if len(twse) < MIN_TWSE_CODES or len(tpex) < MIN_TPEX_CODES:
        raise RuntimeError(
            f"可交易名單不完整（上市 {len(twse)}／上櫃 {len(tpex)}，"
            f"下界 {MIN_TWSE_CODES}／{MIN_TPEX_CODES}），本次不產出排行。"
        )

    print(f"[big_holders] 可交易宇宙: 上市 {len(twse)} + 上櫃 {len(tpex)} = {len(twse | tpex)} 檔")
    return twse | tpex


def implied_total_shares(snapshot: list) -> float | None:
    """由「級距股數 ÷ 級距佔比」反推公司總股數。

    TDCC 沒有直接給總股數（「合計」那列在這份資料裡是空的），但每個級距都同時有
    shares 與 ratio_pct，兩者相除就是分母。只取佔比 >= 1% 的級距並取中位數：
    ratio_pct 只到小數第 2 位，小級距（0.06%）的相對誤差可以到百分之數；大級距實測
    誤差 <0.1%，中位數再擋掉單一級距的壞值。
    """
    cands = [
        e["shares"] / (e["ratio_pct"] / 100)
        for e in snapshot
        if e.get("ratio_pct", 0) >= 1.0 and e.get("shares", 0) > 0
    ]
    return statistics.median(cands) if cands else None


def capital_stable(totals: dict[str, float | None], d_from: str, d_to: str) -> bool:
    """兩期之間股本是否沒動過。推不出總股數時不擋（該公司所有級距都 <1%，實務上不會發生）。"""
    a, b = totals.get(d_from), totals.get(d_to)
    if not a or not b:
        return True
    return abs(b / a - 1) <= CAPITAL_CHANGE_TOL


def load_display_names() -> dict[str, str]:
    # 與所有 fetcher 同一個名單解析（CI 抓的那份 → 隔壁 stock_map）。沒有就回全名，畫面端另有 shortName 對照。
    path = resolve_companies_all_path()
    if path is None:
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {code: info.get("shortName") or info.get("name", code)
            for code, info in data.items()}


def load_raw_company_data(display_names: dict, today: datetime,
                          tradable: set[str]) -> list[dict]:
    """Load raw history for all companies (threshold-independent)."""
    files = sorted(FINANCIALS_DIR.glob("*.json"))
    raw = []
    skipped_untradable = 0
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

        # 興櫃、已終止買賣、暫停交易——TDCC 照發股權分散表，但這些股票榜上無意義。
        if code not in tradable:
            skipped_untradable += 1
            continue

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
            "totals": {dt: implied_total_shares(history[dt]) for dt in dates},
        })

    print(f"[big_holders] 納入 {len(raw)} 間；非上市櫃／已終止買賣跳過 {skipped_untradable} 間")
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
        skipped_capital = 0
        for c in company_data:
            dates = c["dates"]
            if len(dates) <= lb:
                continue

            prev_date = dates[-(lb + 1)]
            # 增資／減資期間，持股比例會在沒人買賣的情況下整段位移；分母變了就不比。
            if not capital_stable(c["totals"], prev_date, dates[-1]):
                skipped_capital += 1
                continue

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
            f"[big_holders] {min_lots}張 period-{lb}: {len(results)} companies "
            f"(股本變動跳過 {skipped_capital}) | "
            f"{main_from} → {main_to} ({days}d) | "
            + (f"top: {results[0]['code']} {results[0]['name']} {results[0]['ratioChange']:+.2f}%"
               if results else "no data")
        )

    return periods_output


def compute(today: datetime = None) -> dict:
    if today is None:
        today = datetime.today()

    display_names = load_display_names()
    tradable = load_tradable_codes()
    raw = load_raw_company_data(display_names, today, tradable)

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
