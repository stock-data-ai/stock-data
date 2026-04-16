"""
批次檢測並移除損壞的 JSON 檔案
用法: uv run python finance_tools/fix_corrupted_json.py [--dry-run]
"""

import json
import os
import sys

FINANCIALS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "src", "data", "layer3", "company-financials"
)


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("=== DRY RUN（僅檢測，不刪除）===\n")

    financials_dir = os.path.normpath(FINANCIALS_DIR)
    if not os.path.isdir(financials_dir):
        print(f"目錄不存在: {financials_dir}")
        sys.exit(1)

    json_files = sorted(f for f in os.listdir(financials_dir) if f.endswith(".json"))
    print(f"掃描 {len(json_files)} 個 JSON 檔案...\n")

    corrupted = []
    for filename in json_files:
        filepath = os.path.join(financials_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            corrupted.append((filename, str(e)))
            print(f"  X {filename}: {e}")

    if not corrupted:
        print("全部正常，沒有損壞的檔案。")
        return

    print(f"\n共發現 {len(corrupted)} 個損壞檔案。")

    if dry_run:
        print("加上 --dry-run 以外的方式執行即可刪除。")
        return

    for filename, _ in corrupted:
        filepath = os.path.join(financials_dir, filename)
        os.remove(filepath)
        print(f"  已刪除: {filename}")

    print(f"\n完成，已刪除 {len(corrupted)} 個損壞檔案。下次 workflow 執行時會重新產生。")


if __name__ == "__main__":
    main()
