"""
generate_stock_etf_index.py

掃描所有 src/data/etf/{code}.json 的 topHoldings，
生成反向索引 src/data/etf-by-stock/{stockCode}.json。

每支個股一個檔案，記錄哪些 ETF 持有它、持倉比重、及本期增減。
供 stock_map 前端在個股頁面顯示「ETF 持倉」tab 使用。

另產生 src/data/etf-by-stock/_moves.json：每支個股近期被 ETF「怎麼動」，給「我的持股」健檢用。
- 主動式（代號 A 結尾，每日揭露）：最新快照 vs 5 個交易日前，新進／加碼／減碼／出清（經理人的決定）。
- 被動式：只看最新一週的成分股納入／剔除。股數增減多半是規模變化（申購贖回），不是看好或看壞，不列。
放在同一個資料夾，排程既有的 `git add src/data/etf-by-stock/` 就會帶上，不必改 workflow。

用法：
    uv run python etf_Crawler/generate_stock_etf_index.py
"""

import json
import statistics
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"
OUT_DIR = REPO_ROOT / "src/data/etf-by-stock"
MOVES_FILE = "_moves.json"

# 主動式比較幾個交易日前（快照每個交易日一筆）
ACTIVE_WINDOW = 5
# 張數變化小於這個（股）不算加碼減碼：零股調整、配股尾數
MIN_SHARES_CHANGE = 1000
# 快照少於這個數量的 ETF 只揭露前幾大（槓桿、反向、期貨或只給前十），成分股納入／剔除判斷不可靠
MIN_FULL_HOLDINGS = 20
# 超過幾天沒更新的 ETF 不列（與 stock_map「超過 14 天不顯示增減」一致）
STALE_DAYS = 14
# 扣掉規模變化後，還要多（少）這個比例才算加碼（減碼）：照比例跟著申購贖回買賣不是經理人看好或看壞
MIN_EXCESS_RATIO = 0.05
# 歷史快照對得到代號的比例低於這個就整檔略過：對不到的列會讓比較誤判成新進或出清
MIN_CODE_COVERAGE = 0.8


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

            # 新進成分股：上一期沒有（爬蟲寫 None 或 0；2026-11-06 前只認 None，0 的新進從來標不出來）
            is_new_entry = (
                weight_change is not None
                and not prev_weight
                and bool(weight)
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
        if path.name == MOVES_FILE:
            continue
        if path.stem not in reverse:
            path.unlink()
            removed += 1
    return removed


def _load_etfs() -> list[dict]:
    etfs = []
    for path in sorted(ETF_DATA_DIR.glob("*.json")):
        if path.stem == "index":
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"  [WARN] 無法讀取 {path.name}: {e}")
            continue
        data.setdefault("code", path.stem)
        etfs.append(data)
    return etfs


def build_name_map(etfs: list[dict]) -> dict[str, str]:
    """持股名稱 → 代號。歷史快照七成沒有代號，靠同時帶名稱與代號的紀錄對回去；同名對到兩個代號就不用。"""
    seen: dict[str, set[str]] = {}
    for etf in etfs:
        rows = list(etf.get("topHoldings", []))
        for snap in (etf.get("holdingsHistory") or {}).values():
            rows.extend(snap)
        for r in rows:
            name, code = r.get("name"), r.get("code")
            if name and code:
                seen.setdefault(name, set()).add(code)
    return {name: next(iter(codes)) for name, codes in seen.items() if len(codes) == 1}


def _snapshot(rows: list[dict], name_map: dict[str, str]) -> tuple[dict[str, int], float]:
    """一個快照 → (代號 → 股數, 對得到代號的列數比例)。同一檔出現兩列就加總；對不到代號的略過。"""
    out: dict[str, int] = {}
    mapped = 0
    for r in rows:
        code = r.get("code") or name_map.get(r.get("name") or "")
        if not code:
            continue
        mapped += 1
        out[code] = out.get(code, 0) + int(r.get("shares") or 0)
    return out, (mapped / len(rows) if rows else 0.0)


def _flow_ratio(before: dict[str, int], after: dict[str, int]) -> float:
    """ETF 規模變化：兩期都持有的股票，股數比例的中位數（多數股票沒動時就是申購贖回造成的比例）"""
    ratios = [after[k] / before[k] for k in before if k in after and before[k] > 0]
    return statistics.median(ratios) if ratios else 1.0


def build_moves(etfs: list[dict], name_map: dict[str, str], today: date) -> dict[str, list]:
    """代號 → 近期 ETF 動作清單。規則見檔頭說明。"""
    moves: dict[str, list] = {}
    cutoff = str(today - timedelta(days=STALE_DAYS))
    for etf in etfs:
        code = etf["code"]
        history = etf.get("holdingsHistory") or {}
        dates = sorted(history)
        if len(dates) < 2 or (etf.get("lastUpdated") or dates[-1]) < cutoff:
            continue
        active = code.endswith("A")
        base_date = dates[-1 - ACTIVE_WINDOW] if active and len(dates) > ACTIVE_WINDOW else (dates[0] if active else dates[-2])
        to_date = dates[-1]
        before, cov_b = _snapshot(history[base_date], name_map)
        after, cov_a = _snapshot(history[to_date], name_map)
        if min(cov_b, cov_a) < MIN_CODE_COVERAGE:
            continue
        full = min(len(history[base_date]), len(history[to_date])) >= MIN_FULL_HOLDINGS
        flow = _flow_ratio(before, after) if active else 1.0
        for stock in sorted(set(before) | set(after)):
            b, a = before.get(stock, 0), after.get(stock, 0)
            if active:
                expected = b * flow
                excess = a - expected
                big = abs(excess) >= max(MIN_SHARES_CHANGE, MIN_EXCESS_RATIO * expected)
                if b == 0 and a > 0:
                    kind = "new" if full else None
                elif a == 0 and b > 0:
                    kind = "exit" if full else None
                elif big and excess > 0:
                    kind = "add"
                elif big and excess < 0:
                    kind = "trim"
                else:
                    kind = None
            else:
                kind = ("join" if b == 0 and a > 0 else "leave" if a == 0 and b > 0 else None) if full else None
            if not kind:
                continue
            moves.setdefault(stock, []).append({
                "etf": code,
                "etfName": etf.get("name", code),
                "active": active,
                "kind": kind,
                "shares": a,
                "sharesChange": a - b,
                "from": base_date,
                "to": to_date,
            })
    for rows in moves.values():
        # 主動式在前，再依變化張數大到小
        rows.sort(key=lambda m: (not m["active"], -abs(m["sharesChange"]), m["etf"]))
    return moves


def write_moves_file(moves: dict[str, list], generated_at: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "generatedAt": generated_at,
        "activeWindowDays": ACTIVE_WINDOW,
        "minSharesChange": MIN_SHARES_CHANGE,
        "moves": moves,
    }
    with open(OUT_DIR / MOVES_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))


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

    print("🔄 計算近期 ETF 動作（_moves.json）...")
    etfs = _load_etfs()
    moves = build_moves(etfs, build_name_map(etfs), date.fromisoformat(today))
    write_moves_file(moves, today)
    count = sum(len(v) for v in moves.values())
    print(f"✅ {len(moves)} 支個股、{count} 筆 ETF 動作")


if __name__ == "__main__":
    main()
