#!/usr/bin/env python3
"""從 stock_map git 歷史回補「當天真的發布出去」的預警快照 → forecasts/forecast_{as_of}.json。

為何要這個檔：戰績（「我們提前幾天示警」）**只能用當時真的發布過的名單**。
拿今天的引擎回頭重算過去，等於用改良後的規則考已知答案，是作弊、不可宣稱。
git 歷史裡的 `src/data/market/disposition-forecast.json` 每個版本都是實際部署過的產物，
是唯一可信的來源。同一 as_of 有多版時取**最後一版**（當日收盤後最終顯示給使用者的那份）。

一次性腳本；之後由 `p1_reconcile.forecast()` 每天自動存當日快照。
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "forecasts")
REPO = "/Users/chiwu/Documents/GitHub/stock_map"
PATH = "src/data/market/disposition-forecast.json"


def _git(*args):
    return subprocess.run(["git", "-C", REPO, *args],
                          capture_output=True, text=True, check=True).stdout


def main():
    os.makedirs(OUT, exist_ok=True)
    hashes = _git("log", "--format=%h", "--", PATH).split()
    # git log 由新到舊；反過來走，同一 as_of 讓後面（較新）的版本覆蓋前面的
    best = {}
    for h in reversed(hashes):
        try:
            d = json.loads(_git("show", f"{h}:{PATH}"))
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            continue
        # 實際部署時間（commit 時間）才是「使用者何時看得到」——資料日可能是回補的
        pub = _git("log", "-1", "--format=%cI", h).strip()
        best[d["as_of"]] = (h, d, pub)

    for as_of, (h, d, pub) in sorted(best.items()):
        snap = {
            "as_of": as_of,
            "source": f"published@{h}",       # 可回查是哪一版部署上去的
            "published_at": pub,              # 使用者實際看得到的時間點（戰績只能用這個）
            "generated_at": d.get("generated_at"),
            "stocks": {
                s["code"]: {
                    "name": s.get("name"),
                    "status": s["status"],
                    "countdown": s.get("countdown"),
                    **({"next_countdown": s["next_countdown"]}
                       if s.get("next_countdown") is not None else {}),
                }
                for s in d.get("stocks", [])
            },
        }
        with open(os.path.join(OUT, f"forecast_{as_of}.json"), "w") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        print(f"  {as_of} ← {h} 發布於 {pub[:16]}（{len(snap['stocks'])} 檔）")
    print(f"[backfill-alerts] 回補 {len(best)} 個資料日 → {OUT}")


if __name__ == "__main__":
    sys.exit(main())
