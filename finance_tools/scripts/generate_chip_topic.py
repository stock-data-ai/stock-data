"""
聚合三大法人（外資/投信）+ 大股東持股，輸出每個題材的籌碼訊號。

輸出格式（chip-topic.json）:
{
  "lastUpdated": "2026-06-08",
  "topics": {
    "ai-pc": {
      "foreign":     { "bull": 8, "total": 12 },
      "trust":       { "bull": 5, "total": 12 },
      "largeHolder": { "bull": 7, "total": 10, "avgChange": 0.3 }
    }
  }
}

bull = 近 5 個交易日法人淨買超為正的公司數
total = 有足夠資料（≥3 個交易日）的公司數
大戶 = 持股 400 張（400,000 股）以上的大股東持股比例，週度比較

輸出方式：
- PAT 環境變數存在 → 推送至 stock_map repo（GitHub Contents API）
- dry_run=True 或無 PAT → 寫入 /tmp/chip-topic.json
"""

import base64
import json
import os
import sys
from datetime import datetime, date
from pathlib import Path
from typing import Union

import requests

from finance_tools.core.timezone import now_tw
from finance_tools.core.trading_day import is_tw_trading_day

BASE = Path(__file__).parent.parent.parent / "src/data"
FINANCIALS_DIR = BASE / "layer3/company-financials"

STOCK_MAP_REPO = "CHIJUI0128/stock_map"
STOCK_MAP_BRANCH = "publish_cloudflare"
CHIP_TOPIC_PATH = "src/data/chip-topic.json"

# 大戶門檻：持股 400 張以上（= 400,000 股以上）
LARGE_HOLDER_RANGES = frozenset({
    "400,001-600,000",
    "600,001-800,000",
    "800,001-1,000,000",
    "1,000,001以上",
})

MIN_II_DAYS = 3   # 外資/投信：至少有 MIN_II_DAYS 天資料才計入 total
II_WINDOW = 5     # 近 5 個交易日


def _fetch_stock_map_json(path: str, pat: str) -> Union[dict, list]:
    url = f"https://api.github.com/repos/{STOCK_MAP_REPO}/contents/{path}?ref={STOCK_MAP_BRANCH}"
    resp = requests.get(url, headers={
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github.v3.raw",
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _fetch_stock_map_json_local(path: str) -> Union[dict, list]:
    """開發用：直接讀本地 stock_map 目錄。"""
    local_root = Path(__file__).parent.parent.parent.parent / "stock_map"
    fp = local_root / path
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


def _load_stock_map_file(path: str, pat: Union[str, None]) -> Union[dict, list]:
    if pat:
        return _fetch_stock_map_json(path, pat)
    # 本地開發 fallback
    try:
        return _fetch_stock_map_json_local(path)
    except FileNotFoundError:
        raise RuntimeError(f"找不到 stock_map 檔案（PAT 未設定，且本地路徑不存在）: {path}")


def _large_holder_ratio(snapshot: list) -> float:
    return sum(
        e.get("ratio_pct", 0.0)
        for e in snapshot
        if e.get("holding_range") in LARGE_HOLDER_RANGES
    )


def _compute_ii_signal(inst_data: dict, field: str) -> tuple[bool, bool]:
    """
    回傳 (has_data, is_bull)：
    - has_data: 是否有 ≥ MIN_II_DAYS 個交易日的資料
    - is_bull: 近 II_WINDOW 日的淨買超合計 > 0
    """
    dates = sorted(inst_data.keys(), reverse=True)[:II_WINDOW]
    values = [inst_data[d].get(field) for d in dates]
    valid = [v for v in values if isinstance(v, (int, float))]
    if len(valid) < MIN_II_DAYS:
        return False, False
    return True, sum(valid) > 0


def _compute_large_holder_signal(sh_history: dict) -> tuple[bool, bool, float]:
    """
    回傳 (has_data, is_bull, delta)：
    - has_data: 是否有 ≥ 2 週資料
    - is_bull: 最新週比上週持股比例增加
    - delta: 持股比例差值（百分點）
    """
    if len(sh_history) < 2:
        return False, False, 0.0
    dates = sorted(sh_history.keys())  # YYYYMMDD
    latest = _large_holder_ratio(sh_history[dates[-1]])
    prev = _large_holder_ratio(sh_history[dates[-2]])
    delta = round(latest - prev, 4)
    return True, delta > 0, delta


def compute(topics_list: list, companies_index: list) -> dict:
    # 建立 topicId → [codes] 的 mapping（只處理非 ETF 的台股題材）
    active_topic_ids = {t["id"] for t in topics_list if t.get("active", True)}
    topic_companies: dict[str, list[str]] = {tid: [] for tid in active_topic_ids}
    code_to_name: dict[str, str] = {}

    for company in companies_index:
        if company.get("isETF"):
            continue
        code = company["code"]
        code_to_name[code] = company.get("name", code)
        for tid in company.get("topics", []):
            if tid in topic_companies:
                topic_companies[tid].append(code)

    results: dict[str, dict] = {}
    newest_inst_date = ""   # 用來擋「上游沒更新卻照算」的假綠燈

    for topic_id, codes in topic_companies.items():
        if not codes:
            continue

        foreign_bull = trust_bull = lh_bull = 0
        foreign_total = trust_total = lh_total = 0
        lh_deltas: list[float] = []

        foreign_companies: list[dict] = []
        trust_companies: list[dict] = []
        lh_companies: list[dict] = []

        for code in codes:
            fp = FINANCIALS_DIR / f"{code}.json"
            if not fp.exists():
                continue
            try:
                with open(fp, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue

            name = code_to_name.get(code, code)

            # 三大法人
            inst = data.get("historical", {}).get("institutionalInvestors", {})
            if inst:
                newest_inst_date = max(newest_inst_date, max(inst.keys()))
                has_f, bull_f = _compute_ii_signal(inst, "foreign_net_buy")
                has_t, bull_t = _compute_ii_signal(inst, "trust_net_buy")
                if has_f:
                    foreign_total += 1
                    if bull_f:
                        foreign_bull += 1
                    foreign_companies.append({"code": code, "name": name, "bull": bull_f})
                if has_t:
                    trust_total += 1
                    if bull_t:
                        trust_bull += 1
                    trust_companies.append({"code": code, "name": name, "bull": bull_t})

            # 大股東持股
            sh_history = data.get("shareholderDataHistory", {})
            has_lh, bull_lh, delta = _compute_large_holder_signal(sh_history)
            if has_lh:
                lh_total += 1
                if bull_lh:
                    lh_bull += 1
                lh_deltas.append(delta)
                lh_companies.append({"code": code, "name": name, "bull": bull_lh})

        avg_change = round(sum(lh_deltas) / len(lh_deltas), 4) if lh_deltas else 0.0

        # 多頭公司排前，空頭公司排後，同組內依代碼排序
        def sort_key(c: dict) -> tuple: return (not c["bull"], c["code"])
        foreign_companies.sort(key=sort_key)
        trust_companies.sort(key=sort_key)
        lh_companies.sort(key=sort_key)

        results[topic_id] = {
            "foreign":     {"bull": foreign_bull, "total": foreign_total, "companies": foreign_companies},
            "trust":       {"bull": trust_bull,   "total": trust_total,   "companies": trust_companies},
            "largeHolder": {"bull": lh_bull,      "total": lh_total,      "avgChange": avg_change, "companies": lh_companies},
        }

        print(
            f"[chip_topic] {topic_id}: "
            f"外資 {foreign_bull}/{foreign_total} "
            f"投信 {trust_bull}/{trust_total} "
            f"大戶 {lh_bull}/{lh_total} avgΔ={avg_change:+.2f}%"
        )

    # 上游 daily-update 沒跑成功時，這裡會拿舊資料算完、照樣寫檔並標今天的日期——
    # 綠燈但籌碼訊號其實停在前一天（2026-08-19 實際發生過）。交易日就必須有當日資料。
    today = now_tw().date()
    if is_tw_trading_day(today) and newest_inst_date != today.isoformat():
        print(f"[chip_topic] STALE: 三大法人最新資料為 {newest_inst_date or '無'}，"
              f"但 {today.isoformat()} 為交易日 —— 上游每日更新未完成，中止產出。")
        sys.exit(1)

    print(f"[chip_topic] 三大法人資料日期: {newest_inst_date}")
    return {
        "lastUpdated": today.isoformat(),
        "topics": results,
    }


def _push_to_stock_map(output: dict, pat: str) -> None:
    content_bytes = json.dumps(output, ensure_ascii=False, indent=2).encode("utf-8")
    content_b64 = base64.b64encode(content_bytes).decode("ascii")

    # 取得現有檔案的 SHA（PUT 更新時需要）
    get_url = f"https://api.github.com/repos/{STOCK_MAP_REPO}/contents/{CHIP_TOPIC_PATH}?ref={STOCK_MAP_BRANCH}"
    headers = {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github.v3+json",
    }
    sha_resp = requests.get(get_url, headers=headers, timeout=30)
    sha = sha_resp.json().get("sha") if sha_resp.ok else None

    put_url = f"https://api.github.com/repos/{STOCK_MAP_REPO}/contents/{CHIP_TOPIC_PATH}"
    payload: dict = {
        "message": f"sync: chip-topic 籌碼訊號更新 {date.today().isoformat()}",
        "content": content_b64,
        "branch": STOCK_MAP_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    resp = requests.put(put_url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    print(f"[chip_topic] ✅ chip-topic.json 已推送至 stock_map ({resp.status_code})")


def run(dry_run: bool = False) -> None:
    pat = os.getenv("STOCK_MAP_PAT") if not dry_run else None

    print("[chip_topic] 讀取 topics + companies index...")
    topics_list = _load_stock_map_file("src/data/layer0/topics.json", pat)
    companies_index = _load_stock_map_file("src/data/layer3/companies/index.json", pat)

    print(f"[chip_topic] 題材數: {len(topics_list)}  公司數: {len(companies_index)}")

    output = compute(topics_list, companies_index)
    topic_count = len(output["topics"])
    print(f"[chip_topic] 完成聚合，{topic_count} 個題材有資料")

    if topic_count < 5:
        print("[chip_topic] ⚠️  有效題材數 < 5，疑似資料異常，中止推送。")
        return

    if pat and not dry_run:
        _push_to_stock_map(output, pat)
    else:
        out_path = Path("/tmp/chip-topic.json")
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[chip_topic] dry_run → 已寫入 {out_path}")


if __name__ == "__main__":
    run(dry_run=True)
