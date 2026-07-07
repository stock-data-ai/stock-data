"""盤中足跡圖（footprint）資料收集：輪詢 TWSE MIS 5 秒快照，記錄成交增量並分內外盤。

原理：
- 每次輪詢取得 z（最新成交價）、v（當日累計成交量）、b/a（委買賣五檔）
- v 的增量（dv）視為「上次輪詢至今的成交量」，記在當下成交價 z
- 用「前一次快照」的最佳買賣價分邊：z >= 前一次委賣 → 主動買（b）；
  z <= 前一次委買 → 主動賣（s）；介於其間 → 中性（n）
- 逐筆 append 到 JSONL，收盤後由 aggregator.py 聚合成 footprint JSON

限制：MIS 是快照不是逐筆 feed，快照間隔內的多筆成交會全部堆到最後成交價，
屬近似重建，非逐筆精確。

用法：
  uv run python -m finance_tools.footprint.poller                 # 等到 09:00，收到 13:33
  uv run python -m finance_tools.footprint.poller --minutes 2 --ignore-hours   # 煙霧測試
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TPE = ZoneInfo("Asia/Taipei")
MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://mis.twse.com.tw/stock/index.jsp",
}

# 熱門股名單（上市/上櫃由啟動時自動判別）
HOT_STOCKS = [
    "2330",  # 台積電
    "2317",  # 鴻海
    "2454",  # 聯發科
    "2308",  # 台達電
    "2303",  # 聯電
    "2382",  # 廣達
    "3711",  # 日月光投控
    "2881",  # 富邦金
    "2882",  # 國泰金
    "2891",  # 中信金
    "2603",  # 長榮
    "2609",  # 陽明
    "2615",  # 萬海
    "2618",  # 長榮航
    "3231",  # 緯創
    "2376",  # 技嘉
    "3017",  # 奇鋐
    "2356",  # 英業達
    "6669",  # 緯穎
    "3661",  # 世芯-KY
    "3443",  # 創意
    "2379",  # 瑞昱
    "3034",  # 聯詠
    "3008",  # 大立光
    "2327",  # 國巨
    "1519",  # 華城
    "1513",  # 中興電
    "2002",  # 中鋼
    "3037",  # 欣興
    "2345",  # 智邦
    "4938",  # 和碩
    "2412",  # 中華電
    "6415",  # 矽力-KY
    "2368",  # 金像電
    "5347",  # 世界先進
    "3105",  # 穩懋
    "8299",  # 群聯
    "6488",  # 環球晶
    "3529",  # 力旺
    "3324",  # 雙鴻
]


def parse_price(value):
    try:
        p = float(value)
        return p if p > 0 else None
    except (TypeError, ValueError):
        return None


def best_quote(value):
    """五檔字串（如 "1180_1185_..."）取第一個有效價。"""
    for part in (value or "").split("_"):
        p = parse_price(part)
        if p is not None:
            return p
    return None


def fetch_quotes(ex_chs):
    url = f"{MIS_URL}?ex_ch={'|'.join(ex_chs)}&json=1&delay=0"
    res = requests.get(url, headers=HEADERS, timeout=8)
    res.raise_for_status()
    return res.json().get("msgArray") or []


def resolve_channels(codes):
    """自動判別上市/上櫃：先全部試 tse_，沒回應的再試 otc_。"""
    resolved = {}
    tse_hits = fetch_quotes([f"tse_{c}.tw" for c in codes])
    for q in tse_hits:
        if q.get("c"):
            resolved[q["c"]] = f"tse_{q['c']}.tw"
    rest = [c for c in codes if c not in resolved]
    if rest:
        time.sleep(2)
        otc_hits = fetch_quotes([f"otc_{c}.tw" for c in rest])
        for q in otc_hits:
            if q.get("c"):
                resolved[q["c"]] = f"otc_{q['c']}.tw"
    missing = [c for c in codes if c not in resolved]
    if missing:
        print(f"⚠️ 無法解析市場別，略過：{missing}", flush=True)
    return resolved


def classify(price, prev_bid, prev_ask):
    if prev_ask is not None and price >= prev_ask:
        return "b"
    if prev_bid is not None and price <= prev_bid:
        return "s"
    return "n"


def run(out_dir: Path, interval: float, end_at: datetime, ignore_hours: bool):
    channels = resolve_channels(HOT_STOCKS)
    print(f"✅ 已解析 {len(channels)} 支：{sorted(channels.values())}", flush=True)
    if not channels:
        sys.exit(1)

    today = datetime.now(TPE).strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{today}.jsonl"
    state = {}  # code -> {v, bid, ask}
    ex_chs = list(channels.values())
    polls = events = 0

    with out_path.open("a", encoding="utf-8") as f:
        while datetime.now(TPE) < end_at:
            started = time.monotonic()
            try:
                quotes = fetch_quotes(ex_chs)
            except Exception as e:
                print(f"⚠️ 輪詢失敗：{e}", flush=True)
                quotes = []
            polls += 1

            for q in quotes:
                code = q.get("c")
                if not code:
                    continue
                z = parse_price(q.get("z"))
                bid, ask = best_quote(q.get("b")), best_quote(q.get("a"))
                try:
                    v = int(q.get("v") or 0)
                    tlong = int(q.get("tlong") or 0)
                except ValueError:
                    continue

                prev = state.get(code)
                if prev is not None and z is not None and v != prev["v"]:
                    dv = v - prev["v"] if v >= prev["v"] else v  # v 變小 = 跨日重置
                    trade_day = datetime.fromtimestamp(tlong / 1000, TPE).strftime("%Y-%m-%d")
                    if dv > 0 and (ignore_hours or trade_day == today):
                        side = classify(z, prev["bid"], prev["ask"])
                        f.write(json.dumps({
                            "code": code, "tlong": tlong, "z": z, "dv": dv,
                            "side": side, "pb": prev["bid"], "pa": prev["ask"],
                            "bid": bid, "ask": ask,
                        }, ensure_ascii=False) + "\n")
                        events += 1
                state[code] = {"v": v, "bid": bid, "ask": ask}

            f.flush()
            if polls % 30 == 0:
                print(f"[{datetime.now(TPE):%H:%M:%S}] polls={polls} events={events}", flush=True)
            time.sleep(max(0.0, interval - (time.monotonic() - started)))

    print(f"🏁 收集完成：polls={polls} events={events} → {out_path}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="footprint_raw", help="JSONL 輸出目錄")
    ap.add_argument("--interval", type=float, default=10.0, help="輪詢間隔（秒）")
    ap.add_argument("--minutes", type=float, default=None, help="只跑 N 分鐘（測試用）")
    ap.add_argument("--ignore-hours", action="store_true", help="不等開盤、不檢查成交日（測試用）")
    args = ap.parse_args()

    now = datetime.now(TPE)
    if args.minutes is not None:
        end_at = now + timedelta(minutes=args.minutes)
    else:
        end_at = now.replace(hour=13, minute=33, second=0, microsecond=0)

    if not args.ignore_hours:
        market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now < market_open:
            wait = (market_open - now).total_seconds()
            print(f"⏳ 等待開盤 {wait:.0f} 秒…", flush=True)
            time.sleep(wait)
        if datetime.now(TPE) >= end_at:
            print("已過收盤時間，結束。", flush=True)
            return

    run(Path(args.out), args.interval, end_at, args.ignore_hours)


if __name__ == "__main__":
    main()
