"""
批次檢測損壞的財報 JSON —— **只回報，不刪、不改。**
用法: uv run python finance_tools/scripts/fix_corrupted_json.py

舊版會把壞檔直接刪掉，那是錯的：刪掉＝「檔案不存在」＝新公司，
日更會建一份只有市值的空殼，歷史就再也回不來了（見 `FileManager.load_financial_data`）。
壞檔要留在原地，所有寫入者看到都會退開。

修復方式（擇一）：
  - 從 git 拿回上一個能讀的版本（**首選**，所有欄位都救得回來）：
        git log --oneline -5 -- src/data/layer3/company-financials/{code}.json
        git show <commit>:src/data/layer3/company-financials/{code}.json > src/data/layer3/company-financials/{code}.json
  - 不處理，等 financials-update 用完整視窗重抓（只救得回季報／年報／月營收）。
"""

import json
import sys
from pathlib import Path

FINANCIALS_DIR = Path(__file__).resolve().parents[2] / "src/data/layer3/company-financials"


def main() -> int:
    if not FINANCIALS_DIR.is_dir():
        print(f"目錄不存在: {FINANCIALS_DIR}")
        return 1

    json_files = sorted(FINANCIALS_DIR.glob("*.json"))
    print(f"掃描 {len(json_files)} 個 JSON 檔案...\n")

    corrupted = []
    for path in json_files:
        try:
            with open(path, encoding="utf-8") as f:
                json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            corrupted.append(path.name)
            print(f"  X {path.name}: {e}")

    if not corrupted:
        print("全部正常，沒有損壞的檔案。")
        return 0

    print(f"\n共發現 {len(corrupted)} 個損壞檔案。**不要刪**——修復方式見本檔開頭說明。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
