"""git 合併驅動：`company-financials/{code}.json` 以 **JSON 結構**三方合併，不做逐行文字合併。

**為什麼要有這支**：六個排程（日更、融資融券、大戶、股利、財報、資產負債表）各自 commit
同一批檔案，推送前都是 `git pull --rebase -X ours`。兩邊都改到同一檔時，git 預設逐行合併：
  - 拼得起來 → 可能是**壞掉的 JSON**（壞檔會讓所有寫入者退開，直到重抓——而重抓只救得回
    季報／月營收，融資融券、大戶、股利都回不來）；
  - 拼不起來 → `-X ours` 讓遠端那段整段勝出，這次寫的資料**無聲消失**。
改成結構合併之後，三大法人加一天、融資融券加一天，兩邊的新資料都留得住，也不可能產出壞 JSON。

**啟用方式**（兩件事都要有，缺一就退回 git 預設的逐行合併）：
  1. `.gitattributes`：`src/data/layer3/company-financials/*.json merge=company-json`
  2. 每個會推送財報檔的 workflow，在 pull 之前：
     `git config merge.company-json.driver "python3 finance_tools/scripts/json_merge_driver.py %O %A %B"`
     （git config 不隨 repo 走，所以每個 job 都要設。只用標準函式庫，不需要 uv。）

**rebase 時三個檔的意思**：%O＝共同祖先、%A＝遠端已有的版本（current）、
%B＝這次要推上去的 commit（incoming）。結果寫回 %A。

合併規則：
  - 只有一邊改 → 取改的那邊（含刪除：封存把舊年度從主檔移走也是一種修改）
  - 兩邊都是 dict → 逐 key 遞迴
  - 兩邊都改了同一個葉節點／list → 取 incoming（這次推的，時間上較新）
  - 一邊刪、一邊改 → **留改過的**（寧可多留一筆，也不要丟資料）
  - 某一邊本身就不是合法 JSON → 取另一邊；兩邊都壞 → 回 1，交給 git 標成衝突
"""

import json
import sys

_MISSING = object()


def merge3(base, current, incoming):
    """三方合併。`_MISSING` 代表該 key 在那一邊不存在；回傳 `_MISSING` 表示結果裡要刪掉。"""
    if current == incoming:
        return current
    if base == current:
        return incoming
    if base == incoming:
        return current
    # 以下是兩邊都改了
    if incoming is _MISSING:
        return current
    if current is _MISSING:
        return incoming
    if isinstance(current, dict) and isinstance(incoming, dict):
        base_d = base if isinstance(base, dict) else {}
        merged = {}
        for key in list(current) + [k for k in incoming if k not in current]:
            value = merge3(base_d.get(key, _MISSING),
                           current.get(key, _MISSING),
                           incoming.get(key, _MISSING))
            if value is not _MISSING:
                merged[key] = value
        return merged
    return incoming


def _load(path):
    """讀不回來（壞檔）或空檔（%O 在兩邊都新增同一檔時是空的）→ None。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        return json.loads(text) if text.strip() else None
    except (OSError, ValueError):
        return None


def main(argv) -> int:
    base_path, current_path, incoming_path = argv[1:4]
    base, current, incoming = _load(base_path), _load(current_path), _load(incoming_path)

    if current is None and incoming is None:
        print(f"[json-merge] {current_path}: 兩邊都不是合法 JSON，交給 git 標衝突", file=sys.stderr)
        return 1
    if incoming is None:
        result = current
    elif current is None:
        result = incoming
    else:
        result = merge3(base if base is not None else {}, current, incoming)

    # 與 `FileManager.save_financial_data` 同一個格式，否則每次合併都變成整檔 diff。
    with open(current_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
