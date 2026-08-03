"""
三大法人（上市）買賣超統計完成 → 推播通知。

資料源：src/data/market/sentiment.json 的 institutional.twse（BFI82U，單位：元）。
上櫃在 fetcher 端刻意停用（見 fetcher.fetch_all 的註解），故通知只涵蓋上市。

推播管道：POST stock_map 的 /api/cron/push-broadcast（帶 CRON_SECRET）。
該 endpoint 以 key 在 Firestore 佔位防重複，market-sentiment 一天跑 4 次也只會推一次。

守門（任一不成立就跳過，不是失敗）：
- institutional.date 必須是今天（台北）。非交易日、或當輪 BFI82U 尚未公布時
  tasks.py 會保留前一日資料，此時推出去就是錯的數字。
- 缺 CRON_SECRET 時只印不推，讓本機與未設 secret 的環境不會讓 workflow 紅掉。

用法：
    uv run finance_tools/scripts/notify_market_inst.py [--dry-run]
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

SENTIMENT_FILE = Path(__file__).parent.parent.parent / "src/data/market/sentiment.json"
PUSH_ENDPOINT = os.environ.get(
    "PUSH_ENDPOINT", "https://aistockmap.com/api/cron/push-broadcast"
)
PUSH_TOPIC = "market-inst"
TAIPEI = timezone(timedelta(hours=8))

# 與前端 MarketSentimentWidget 的 instNet 對齊：五個分項全加（foreignDealer 目前恆為 0，
# 但 BFI82U 的「外資」是不含外資自營商的那列，漏加會在該欄有值時與 App 顯示對不上）。
NET_KEYS = ("foreign", "foreignDealer", "trust", "dealer", "dealerHedge")


def _net(group: dict, key: str) -> int:
    return int(group.get(key, {}).get("net", 0) or 0)


def _format_yi(net: int) -> str:
    """元 → 「買超/賣超 N 億元」。0 視為買超（與大盤慣例一致，實務上不會出現）。"""
    label = "賣超" if net < 0 else "買超"
    return f"{label} {abs(net) / 1e8:.1f} 億元"


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    if not SENTIMENT_FILE.exists():
        print(f"[notify_market_inst] 找不到 {SENTIMENT_FILE}，跳過")
        return 0

    data = json.loads(SENTIMENT_FILE.read_text(encoding="utf-8"))
    inst = data.get("institutional") or {}
    twse = inst.get("twse")
    date_str = inst.get("date")
    today = datetime.now(TAIPEI).strftime("%Y-%m-%d")

    if not twse:
        print("[notify_market_inst] institutional.twse 缺漏，跳過")
        return 0
    if date_str != today:
        print(f"[notify_market_inst] 資料日期 {date_str} 非今日 {today}（非交易日或尚未公布），跳過")
        return 0

    foreign = _net(twse, "foreign") + _net(twse, "foreignDealer")
    trust = _net(twse, "trust")
    dealer = _net(twse, "dealer") + _net(twse, "dealerHedge")
    total = sum(_net(twse, k) for k in NET_KEYS)

    title = "⚡ 三大法人（上市個股）買賣超統計完成"
    body = "\n".join([
        f"三大法人合計: {_format_yi(total)}",
        f"外資: {_format_yi(foreign)}",
        f"投信: {_format_yi(trust)}",
        f"自營商: {_format_yi(dealer)}",
    ])

    payload = {
        "key": f"market-inst-{date_str}",
        "topic": PUSH_TOPIC,
        "title": title,
        "body": body,
        "url": "/?activeTab=daily",
    }

    print(f"[notify_market_inst] {title}\n{body}")

    secret = os.environ.get("CRON_SECRET")
    if dry_run or not secret:
        print(f"[notify_market_inst] {'dry-run' if dry_run else '缺 CRON_SECRET'} → 不發送")
        return 0

    resp = requests.post(
        PUSH_ENDPOINT,
        json=payload,
        headers={"Authorization": f"Bearer {secret}"},
        timeout=30,
    )
    result = resp.text[:300]
    if not resp.ok:
        print(f"[notify_market_inst] ❌ 推播失敗 HTTP {resp.status_code}: {result}")
        return 1

    print(f"[notify_market_inst] ✅ {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
