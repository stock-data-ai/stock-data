"""把 poller 收集的 JSONL 聚合成 footprint JSON（每支股票 × 每根 5 分 K × 每價位買賣量）。

輸出：src/data/market/footprint/{date}/{code}.json + index.json

用法：
  uv run python -m finance_tools.footprint.aggregator --date 2026-07-08
  uv run python -m finance_tools.footprint.aggregator --raw footprint_raw/2026-07-08.jsonl
"""

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TPE = ZoneInfo("Asia/Taipei")
BAR_MINUTES = 5


def bar_key(tlong_ms: int) -> str:
    dt = datetime.fromtimestamp(tlong_ms / 1000, TPE)
    return dt.replace(minute=dt.minute - dt.minute % BAR_MINUTES, second=0).strftime("%H:%M")


def clean(evs):
    """過濾 MIS 落後快照造成的假事件（單筆 dv ≈ 當時累計量）。

    規則：累計量 cum 超過 1000 後，單筆 dv > cum 的一半視為異常丟棄。
    開盤第一筆（集合競價大單）在 cum 小時不受此規則影響。
    """
    out, cum, dropped = [], 0, 0
    for e in evs:
        if cum > 1000 and e["dv"] > cum * 0.5:
            dropped += 1
            continue
        cum += e["dv"]
        out.append(e)
    return out, dropped


def aggregate(raw_path: Path, out_dir: Path, date: str) -> int:
    events = defaultdict(list)  # code -> [event]
    with raw_path.open(encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            events[e["code"]].append(e)

    out_dir.mkdir(parents=True, exist_ok=True)
    index = []
    for code, evs in sorted(events.items()):
        evs.sort(key=lambda e: e["tlong"])
        evs, dropped = clean(evs)
        if dropped:
            print(f"  {code}: 清掉 {dropped} 筆落後快照假事件")
        bars = {}
        for e in evs:
            key = bar_key(e["tlong"])
            bar = bars.setdefault(key, {
                "t": key, "o": e["z"], "h": e["z"], "l": e["z"], "c": e["z"],
                "v": 0, "delta": 0, "levels": defaultdict(lambda: {"b": 0, "s": 0, "n": 0}),
            })
            bar["h"] = max(bar["h"], e["z"])
            bar["l"] = min(bar["l"], e["z"])
            bar["c"] = e["z"]
            bar["v"] += e["dv"]
            bar["delta"] += e["dv"] if e["side"] == "b" else -e["dv"] if e["side"] == "s" else 0
            bar["levels"][e["z"]][e["side"]] += e["dv"]

        out_bars = []
        for key in sorted(bars):
            bar = bars[key]
            bar["levels"] = [
                {"p": p, **vols} for p, vols in sorted(bar["levels"].items(), reverse=True)
            ]
            out_bars.append(bar)

        payload = {"code": code, "date": date, "interval": f"{BAR_MINUTES}m", "bars": out_bars}
        (out_dir / f"{code}.json").write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        index.append({"code": code, "bars": len(out_bars), "events": len(evs)})

    (out_dir / "index.json").write_text(
        json.dumps({"date": date, "interval": f"{BAR_MINUTES}m", "stocks": index},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return len(index)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(TPE).strftime("%Y-%m-%d"))
    ap.add_argument("--raw", default=None, help="JSONL 路徑（預設 footprint_raw/{date}.jsonl）")
    ap.add_argument("--out", default=None, help="輸出目錄（預設 src/data/market/footprint/{date}）")
    args = ap.parse_args()

    raw_path = Path(args.raw) if args.raw else Path("footprint_raw") / f"{args.date}.jsonl"
    if not raw_path.exists() or raw_path.stat().st_size == 0:
        print(f"⚠️ 無原始資料：{raw_path}，不輸出。")
        return
    out_dir = Path(args.out) if args.out else Path("src/data/market/footprint") / args.date

    n = aggregate(raw_path, out_dir, args.date)
    print(f"✅ 聚合完成：{n} 支股票 → {out_dir}")


if __name__ == "__main__":
    main()
