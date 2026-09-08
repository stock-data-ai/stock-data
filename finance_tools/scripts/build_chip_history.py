"""全市場籌碼歷史（橫斷面），供 stock_map 的籌碼選股使用。

`company-financials/{code}.json` 是**逐檔**的完整歷史（2,298 檔、共 1.5 GB）。
選股要問的是「今天全市場誰被買」，那是橫斷面問題——逐檔查等於要讀 1.5 GB，
不可能放在使用者的請求路徑上，也不該讓 stock_map 去 clone 這個 repo。

這支把它擠成一個小檔：只留最近 KEEP_DAYS 個交易日、只留籌碼選股真的會用到的 7 個欄位。

    uv run finance_tools/scripts/build_chip_history.py
    uv run finance_tools/scripts/build_chip_history.py --dry-run

輸出：src/data/market/chip-history.json（隨 repo 發到 GitHub Pages）

> [!IMPORTANT]
> **要排在 daily-update（三大法人）與 margin-trading-update（融資融券／借券）之後跑。**
> 這支不抓任何資料，只讀磁碟上已經有的檔案——排前面的話擠出來的是昨天的籌碼，
> 而且完全不會報錯。stock-data 的班表：19:30 法人、21:30 融資券借券。

> [!NOTE]
> **欄位單位是「張」**（`foreign_net_buy` 等於 FinMind 的股數 ÷ 1000，上游已經換算好）。
> `foreign_ratio` 是百分比。不要在這裡再換算一次。
"""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent.parent.parent / "src/data"
FINANCIALS_DIR = BASE / "layer3/company-financials"
OUTPUT_FILE = BASE / "market/chip-history.json"

# 保留幾個交易日。
#
# 只算「今天」的話 20 天就夠（連買最多看 10 天、餘額增幅看 5 天、券資比新高看 20 天）。
# 但 stock_map 的自訂規則要問「近 N 日內曾命中」，得把過去 10 天各自重算一次——
# **最舊那天也要有 20 天可回看**，否則「券資比創 20 日新高」在歷史裡會退化成
# 「創 10 日新高」，同一個條件在不同日期是不同定義，那種不一致查不出來。
# 10（歷史深度）＋ 20（回看窗）= 30。
KEEP_DAYS = 30

# 最後一筆資料超過這麼多天就整檔不收：下市、暫停交易、或上游停更。
# 收進來只會讓「連買 3 日」對到三個月前的資料。
FRESHNESS_DAYS = 10

# 欄位對照：輸出的短名 → 來源區塊與原始欄位名。
# 短名是為了體積——2,000 檔 × 20 天 × 7 欄，key 名長一個字就多 280KB。
FIELDS = [
    ("fNet",   "institutionalInvestors", "foreign_net_buy"),
    ("tNet",   "institutionalInvestors", "trust_net_buy"),
    ("dNet",   "institutionalInvestors", "dealer_net_buy"),
    ("fRatio", "institutionalInvestors", "foreign_ratio"),
    ("mBal",   "marginTrading",          "marginBalance"),
    ("sBal",   "marginTrading",          "shortBalance"),
    ("sbl",    "securitiesLending",      "sblBalance"),
]


def num(v, digits=2):
    """數字才留，其餘一律 None。**0 要保留**——法人今天沒買賣就是 0，不是缺值。

    取位不是美觀問題是體積問題：上游的浮點會出現 `406.73999999999995` 這種 18 個字元的
    值，2,000 檔 × 20 天 × 7 欄全用原值會多吃三成。張數取到小數第二位（＝10 股）
    對「連買幾天」「增幅幾成」這類判斷綽綽有餘。
    """
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    return int(v) if float(v).is_integer() else round(float(v), digits)


def collect():
    files = sorted(FINANCIALS_DIR.glob("*.json"))
    print(f"掃描 {len(files)} 個檔案…")

    # 先各自收成 {code: {block: {date: {field: value}}}}，再統一對齊日期軸。
    per_code = {}
    latest_seen = ""
    skipped = {"parse": 0, "empty": 0, "stale": 0}
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
        inst = hist.get("institutionalInvestors") or {}
        if not inst:
            skipped["empty"] += 1
            continue

        code = doc.get("companyCode") or fp.stem
        blocks = {
            "institutionalInvestors": inst,
            "marginTrading": hist.get("marginTrading") or {},
            "securitiesLending": hist.get("securitiesLending") or {},
        }
        newest = max(inst.keys())
        latest_seen = max(latest_seen, newest)
        per_code[code] = (blocks, newest)

    if not per_code:
        raise SystemExit("[chip] 一檔都沒讀到，中止")

    # 日期軸取「最新那天往回數 KEEP_DAYS 個有資料的日子」——用聯集而不是單一檔的日期，
    # 因為個別公司會缺席（暫停交易、當天沒有法人進出）。
    all_dates = set()
    for blocks, _ in per_code.values():
        all_dates |= set(blocks["institutionalInvestors"].keys())
    dates = sorted(all_dates)[-KEEP_DAYS:]
    cutoff = datetime.strptime(dates[0], "%Y-%m-%d")

    codes = {}
    for code, (blocks, newest) in per_code.items():
        # 太久沒更新就整檔不收（下市／停更）。用最新那天與日期軸的起點比。
        if datetime.strptime(newest, "%Y-%m-%d") < cutoff:
            skipped["stale"] += 1
            continue
        series = {}
        for short, block, field in FIELDS:
            src = blocks[block]
            arr = [num((src.get(d) or {}).get(field)) for d in dates]
            if any(v is not None for v in arr):
                series[short] = arr
        if series:
            codes[code] = series

    print(f"\n讀檔耗時 {time.time() - t0:.0f}s")
    print(f"略過：解析失敗 {skipped['parse']}、無法人資料 {skipped['empty']}、"
          f"太久沒更新 {skipped['stale']}")
    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "tradeDate": dates[-1],
        "source": "FinMind（三大法人／融資融券／借券賣出，皆為交易所盤後日結）",
        "units": {"fNet": "張", "tNet": "張", "dNet": "張", "fRatio": "%",
                  "mBal": "張", "sBal": "張", "sbl": "張"},
        "dates": dates,
        "codes": codes,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    out = collect()
    n = len(out["codes"])
    print(f"\n結果：{len(out['dates'])} 個交易日"
          f"（{out['dates'][0]} ~ {out['tradeDate']}）、{n} 檔")
    for short, _, _ in FIELDS:
        have = sum(1 for s in out["codes"].values() if short in s)
        print(f"  {short:<7}{have:>5} 檔（{have / n * 100:.0f}%）")

    if args.dry_run:
        print("\n--dry-run，不寫檔")
        return

    # 護欄：檔數比上一版掉超過三成就不寫，疑似上游殘缺（比照熱力圖的做法）。
    try:
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            prev = len(json.load(f).get("codes", {}))
    except (FileNotFoundError, ValueError):
        prev = 0
    if prev and n < prev * 0.7:
        raise SystemExit(f"[chip] 檔數從 {prev} 掉到 {n}（-{(1 - n / prev) * 100:.0f}%），不寫檔")

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        # allow_nan=False：NaN 是合法的 Python JSON 但 Node 讀不回來，
        # 消費端（stock_map）會直接掛掉而且本機測不出來。
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    print(f"\n→ {OUTPUT_FILE}（{OUTPUT_FILE.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
