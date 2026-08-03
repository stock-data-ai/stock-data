"""
產出「處置股預警」disposition-forecast.json 並推送至 stock_map。

比照 generate_chip_topic.py：
- STOCK_MAP_PAT 存在 → 推送至 stock_map repo（GitHub Contents API）
- dry_run=True 或無 PAT → 寫入 /tmp/disposition-forecast.json

引擎邏輯：finance_tools/disposition/p1_reconcile.py（款1~7 規則 + 狀態機 + reconcile）。
規則對照與設計：stock_map docs/future/處置股預測.md。

每日流程：notice 快照官方標籤 → 回測近 N 日 prediction → forecast 摺算 → 裁掉純 safe → 推送。
predictions/cache 皆可再生（gitignore），CI 每次重建，故 stateless。
"""

import base64
import json
import os
from pathlib import Path

import requests

from finance_tools.disposition import p1_reconcile as engine

STOCK_MAP_REPO = "CHIJUI0128/stock_map"
STOCK_MAP_BRANCH = "publish_cloudflare"
FORECAST_PATH = "src/data/market/disposition-forecast.json"
BACKTEST_DAYS = 30


def _trim(forecast: dict) -> dict:
    """只留非 safe 個股（safe 不逐檔顯示，數量保留在 counts）。"""
    keep = [s for s in forecast["stocks"] if s["status"] != "safe"]
    return {
        "as_of": forecast["as_of"],
        "generated_at": forecast["generated_at"],
        "window": forecast["window"],
        "counts": forecast["counts"],
        "stocks": keep,
    }


def _push_to_stock_map(output: dict, pat: str) -> None:
    content_b64 = base64.b64encode(
        json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    headers = {"Authorization": f"token {pat}", "Accept": "application/vnd.github.v3+json"}

    get_url = f"https://api.github.com/repos/{STOCK_MAP_REPO}/contents/{FORECAST_PATH}?ref={STOCK_MAP_BRANCH}"
    sha_resp = requests.get(get_url, headers=headers, timeout=30)
    sha = sha_resp.json().get("sha") if sha_resp.ok else None

    put_url = f"https://api.github.com/repos/{STOCK_MAP_REPO}/contents/{FORECAST_PATH}"
    payload = {
        "message": f"sync: 處置股預警更新 {output['as_of']}",
        "content": content_b64,
        "branch": STOCK_MAP_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    resp = requests.put(put_url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    print(f"[disposition] ✅ disposition-forecast.json 已推送至 stock_map ({resp.status_code})")


def run(dry_run: bool = False) -> None:
    pat = os.getenv("STOCK_MAP_PAT") if not dry_run else None

    print(f"[disposition] 快照官方 notice + 回測 {BACKTEST_DAYS} 日 + 摺算 forecast...")
    try:
        engine.notice()          # 官方標籤快照（best-effort；forward 累積驗證用）
    except Exception as e:
        print(f"[disposition] notice 快照失敗（略過，不影響預警）: {e}")
    try:
        # 官方注意名單（含款號）：上市可回溯 60 日、上櫃補最近兩日。
        # 必須在 backtest 之前 —— 款2 豁免要靠「近30日曾公告款1」的官方 state。
        engine.backfill_notice()
    except Exception as e:
        print(f"[disposition] 官方名單回補失敗（款2 豁免將失效，預警可能變吵）: {e}")
    engine.backtest(BACKTEST_DAYS)
    engine.forecast()

    fp = Path(engine.HERE) / "disposition-forecast.json"
    forecast = json.loads(fp.read_text(encoding="utf-8"))
    output = _trim(forecast)
    counts = output.get("counts", {})
    n = len(output["stocks"])
    print(f"[disposition] as_of={output['as_of']} 非safe={n} counts={counts}")

    # 護欄：非safe=0 或 處置名單為空（punish API 抓取失敗）→ 疑似資料異常，不推。
    if n == 0 or counts.get("disposed", 0) == 0:
        print("[disposition] ⚠️  非safe=0 或處置名單為空，疑似資料異常，中止推送。")
        return

    if pat and not dry_run:
        _push_to_stock_map(output, pat)
    else:
        out_path = Path("/tmp/disposition-forecast.json")
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[disposition] dry_run → 已寫入 {out_path}")


if __name__ == "__main__":
    run(dry_run=True)
