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

from __future__ import annotations       # str | None 註記需要（本機 venv 為 Python 3.9）

import base64
import json
import os
import sys
from pathlib import Path

import requests

from finance_tools.disposition import p1_reconcile as engine

STOCK_MAP_REPO = "CHIJUI0128/stock_map"
STOCK_MAP_BRANCH = "publish_cloudflare"
FORECAST_PATH = "src/data/market/disposition-forecast.json"
# 舊版處置名單（/live 契約，ADR-010）。原由 stock_map fetch_punishments.py 打 TWSE openapi 產生，
# 那支只有上市、沒套 2026-08-10 新制、且沒有排程（靠 company-topics/index.json 被 push 才跑）。
# 改由同一個引擎產生 → 個股頁警示／熱力圖黃框／公司資料庫篩選／推播／每日焦點五處共用同一真相源。
PUNISH_PATH = "src/data/market/punishments.json"
BACKTEST_DAYS = 30


def _trim(forecast: dict) -> dict:
    """只留非 safe 個股（safe 不逐檔顯示，數量保留在 counts）。"""
    keep = [s for s in forecast["stocks"] if s["status"] != "safe"]
    return {
        "as_of": forecast["as_of"],
        "disposal_as_of": forecast.get("disposal_as_of"),   # 處置名單看的是今天，非價格資料日
        "generated_at": forecast["generated_at"],
        "window": forecast["window"],
        "counts": forecast["counts"],
        "stocks": keep,
    }


def _push_to_stock_map(output: dict, pat: str, path: str = FORECAST_PATH,
                       message: str | None = None) -> None:
    content_b64 = base64.b64encode(
        json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    headers = {"Authorization": f"token {pat}", "Accept": "application/vnd.github.v3+json"}

    get_url = f"https://api.github.com/repos/{STOCK_MAP_REPO}/contents/{path}?ref={STOCK_MAP_BRANCH}"
    sha_resp = requests.get(get_url, headers=headers, timeout=30)
    sha = sha_resp.json().get("sha") if sha_resp.ok else None

    put_url = f"https://api.github.com/repos/{STOCK_MAP_REPO}/contents/{path}"
    payload = {
        "message": message or f"sync: 處置股預警更新 {output['as_of']}",
        "content": content_b64,
        "branch": STOCK_MAP_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    resp = requests.put(put_url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    print(f"[disposition] ✅ {path.rsplit('/', 1)[-1]} 已推送至 stock_map ({resp.status_code})")


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
    # 不可靜靜 return：什麼都沒推卻綠燈，預警畫面會停在昨天而沒人察覺。
    if n == 0 or counts.get("disposed", 0) == 0:
        print("[disposition] ⚠️  非safe=0 或處置名單為空，疑似資料異常，中止推送。")
        sys.exit(1)

    # 舊版處置名單同步產生。護欄：0 筆代表官方端點抓失敗，推上去會讓四個畫面同時清空。
    punish = engine.export_punishments()
    print(f"[disposition] punishments.json {punish['count']} 筆（含上櫃與權證）")
    punish_failed = punish["count"] == 0
    if punish_failed:
        # 這檔不推（推 0 筆會讓四個畫面同時清空），但要留到最後讓整批紅燈。
        print("[disposition] ⚠️  處置名單 0 筆，疑似抓取失敗，該檔不推送。")
        punish = None

    if pat and not dry_run:
        _push_to_stock_map(output, pat)
        if punish:
            _push_to_stock_map(punish, pat, PUNISH_PATH,
                               f"sync: 處置股名單更新 {output['as_of']}")
    else:
        for name, data in (("disposition-forecast", output), ("punishments", punish)):
            if data is None:
                continue
            out_path = Path(f"/tmp/{name}.json")
            out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[disposition] dry_run → 已寫入 {out_path}")

    if punish_failed:
        print("[disposition] STALE: 處置名單撈取失敗，punishments.json 本次未更新。")
        sys.exit(1)


if __name__ == "__main__":
    run(dry_run=True)
