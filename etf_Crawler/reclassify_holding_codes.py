#!/usr/bin/env python3
"""
reclassify_holding_codes.py — 既有 ETF 檔的持股代號，用 classify_holding_code 重新分類。

爬蟲改用共用分類（etf_utils.classify_holding_code）之前，「4～6 碼數字就是台股」把混合市場 ETF
（MIXED_MARKET_ETFS，例如 009826）的日、港、韓股寫成台股 code；已發布的 topHoldings 與 holdingsHistory
留著錯的代號。這支依共用規則重新分類。新增 MIXED_MARKET_ETFS 後跑一次即可；可重複執行，結果相同。

用法：
  uv run python etf_Crawler/reclassify_holding_codes.py            # 寫檔
  uv run python etf_Crawler/reclassify_holding_codes.py --dry-run  # 只統計，不寫任何檔
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from etf_utils import MIXED_MARKET_ETFS, classify_holding_code  # noqa: E402

ETF_DIR = Path(__file__).resolve().parents[1] / "src/data/etf"


def _reclassify(holdings: list, mixed_market: bool) -> int:
    changed = 0
    for h in holdings or []:
        if not isinstance(h, dict):
            continue
        raw = h.get("code") or h.get("foreignCode")
        if not raw:
            continue
        tw_code, foreign_code = classify_holding_code(raw, h.get("name"), mixed_market=mixed_market)
        if (h.get("code"), h.get("foreignCode")) == (tw_code, foreign_code):
            continue
        h.pop("code", None)
        h.pop("foreignCode", None)
        if tw_code:
            h["code"] = tw_code
        elif foreign_code:
            h["foreignCode"] = foreign_code
        changed += 1
    return changed


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    total = 0
    for path in sorted(ETF_DIR.glob("*.json")):
        if path.stem == "index":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        mixed = path.stem in MIXED_MARKET_ETFS
        changed = _reclassify(data.get("topHoldings"), mixed)
        for snapshot in (data.get("holdingsHistory") or {}).values():
            changed += _reclassify(snapshot if isinstance(snapshot, list) else snapshot.get("holdings"), mixed)
        if changed:
            total += changed
            print(f"  {path.stem}: {changed} 筆")
            if not dry_run:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{'（試算）' if dry_run else ''}共重新分類 {total} 筆持股")


if __name__ == "__main__":
    main()
