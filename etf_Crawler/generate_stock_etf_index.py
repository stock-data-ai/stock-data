"""
generate_stock_etf_index.py

掃描所有 src/data/etf/{code}.json 的 topHoldings，
生成反向索引 src/data/etf-by-stock/{stockCode}.json。

每支個股一個檔案，記錄哪些 ETF 持有它、持倉比重、及本期增減。
供 stock_map 前端在個股頁面顯示「ETF 持倉」tab 使用。

用法：
    uv run python etf_Crawler/generate_stock_etf_index.py
"""

import json
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"
OUT_DIR = REPO_ROOT / "src/data/etf-by-stock"


def load_etf_index() -> dict[str, dict]:
    """Load ETF index for metadata (categoryId, issuer)."""
    index_path = ETF_DATA_DIR / "index.json"
    if not index_path.exists():
        return {}
    with open(index_path, encoding="utf-8") as f:
        entries = json.load(f)
    return {e["code"]: e for e in entries}


def build_reverse_index(etf_index: dict[str, dict]) -> dict[str, list]:
    """Build stock_code → list of ETF holder records."""
    reverse: dict[str, list] = {}

    for path in sorted(ETF_DATA_DIR.glob("*.json")):
        if path.stem == "index":
            continue
        try:
            with open(path, encoding="utf-8") as f:
                etf_data = json.load(f)
        except Exception as e:
            print(f"  [WARN] 無法讀取 {path.name}: {e}")
            continue

        etf_code = etf_data.get("code", path.stem)
        etf_name = etf_data.get("name", etf_code)
        etf_last_updated = etf_data.get("lastUpdated")
        meta = etf_index.get(etf_code, {})

        for holding in etf_data.get("topHoldings", []):
            stock_code = holding.get("code")
            if not stock_code:
                continue

            weight = holding.get("weight")
            weight_change = holding.get("weightChange")
            shares = holding.get("shares")
            shares_change = holding.get("sharesChange")
            prev_weight = holding.get("previousWeight")

            # Detect new entry: first-time appearance in ETF
            is_new_entry = (
                weight_change is not None
                and prev_weight is None
                and weight_change == weight
            )

            record = {
                "etfCode": etf_code,
                "etfName": etf_name,
                "categoryId": meta.get("categoryId"),
                "issuer": meta.get("issuer"),
                "lastUpdated": etf_last_updated,
                "weight": weight,
                "weightChange": weight_change,
                "shares": shares,
                "sharesChange": shares_change,
                "isNewEntry": is_new_entry,
            }

            if stock_code not in reverse:
                reverse[stock_code] = []
            reverse[stock_code].append(record)

    return reverse


def write_stock_files(reverse: dict[str, list], generated_at: str) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = 0

    for stock_code, holders in reverse.items():
        # Sort by weight descending
        holders_sorted = sorted(
            holders,
            key=lambda h: (h.get("weight") or 0),
            reverse=True,
        )
        out = {
            "stockCode": stock_code,
            "generatedAt": generated_at,
            "holderCount": len(holders_sorted),
            "holders": holders_sorted,
        }
        out_path = OUT_DIR / f"{stock_code}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        written += 1

    return written


def remove_stale_files(reverse: dict[str, list]) -> int:
    """Remove files for stocks no longer in any ETF topHoldings."""
    removed = 0
    if not OUT_DIR.exists():
        return 0
    for path in OUT_DIR.glob("*.json"):
        if path.stem not in reverse:
            path.unlink()
            removed += 1
    return removed


def main():
    today = str(date.today())
    print(f"📅 生成日期: {today}")

    print("📂 載入 ETF index...")
    etf_index = load_etf_index()
    print(f"   共 {len(etf_index)} 支 ETF")

    print("🔄 建立反向索引...")
    reverse = build_reverse_index(etf_index)
    print(f"   涵蓋個股: {len(reverse)} 支")

    stale = remove_stale_files(reverse)
    if stale:
        print(f"   移除過期檔案: {stale} 個")

    written = write_stock_files(reverse, today)
    print(f"✅ 已輸出 {written} 支個股的 ETF 持倉索引至 src/data/etf-by-stock/")


if __name__ == "__main__":
    main()
