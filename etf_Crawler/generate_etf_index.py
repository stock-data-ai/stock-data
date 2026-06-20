"""
generate_etf_index.py

從 src/data/etf/{code}.json 重新生成 src/data/etf/index.json。

{code}.json 是 single source of truth for:
  code, name, assetClass, categoryId, trackingIndex,
  managementFee, dividendFrequency, inceptionDate, fundSize, issuer,
  trailingYield, beneficiaryCount

ETF 清單順序與現有 index.json 一致；新 ETF 附加到末尾。

用法：
    uv run python etf_Crawler/generate_etf_index.py
"""

import json
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"
INDEX_PATH = ETF_DATA_DIR / "index.json"

# Fields where the detail json is authoritative
CRAWLER_FIELDS = [
    "managementFee",
    "dividendFrequency",
    "inceptionDate",
    "fundSize",
    "issuer",
    "trailingYield",
    "beneficiaryCount",
]

# Fields where stock_map (existing index.json) is authoritative; {code}.json used only as fallback
MANUAL_FIELDS = ["name", "assetClass", "categoryId", "trackingIndex"]

# Fields that have no detail-json source — always kept from existing index
PRESERVED_FIELDS = []


def load_existing_index() -> dict[str, dict]:
    if not INDEX_PATH.exists():
        return {}
    with open(INDEX_PATH, encoding="utf-8") as f:
        entries = json.load(f)
    return {e["code"]: e for e in entries}


def load_detail_files() -> dict[str, dict]:
    details = {}
    for path in sorted(ETF_DATA_DIR.glob("*.json")):
        if path.stem == "index":
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if "code" in data:
                details[data["code"]] = data
        except Exception as e:
            print(f"  [WARN] 無法讀取 {path.name}: {e}")
    return details


def build_entry(code: str, detail: Optional[dict], existing: Optional[dict]) -> dict:
    entry: dict = {"code": code}

    # Crawler-authoritative: {code}.json wins; explicit null in detail overrides existing too
    for field in CRAWLER_FIELDS:
        if detail is not None and field in detail:
            entry[field] = detail[field] if detail[field] != "" else None
        else:
            entry[field] = existing.get(field) if existing else None

    # Manual-authoritative: existing (stock_map) wins, fallback to {code}.json
    for field in MANUAL_FIELDS:
        if existing is not None and existing.get(field) not in (None, ""):
            entry[field] = existing[field]
        elif detail is not None:
            entry[field] = detail.get(field)
        else:
            entry[field] = None

    # Preserved: only from existing, no other source
    for field in PRESERVED_FIELDS:
        entry[field] = existing.get(field) if existing else None

    return entry


def main():
    existing_index = load_existing_index()
    details = load_detail_files()

    # 保留有 detail JSON 的 code；已刪除的 JSON 從 index 移除
    removed_codes = [c for c in existing_index if c not in details]
    if removed_codes:
        print(f"  移除 ETF（JSON 已刪）: {', '.join(sorted(removed_codes))}")
    all_codes_ordered = [c for c in existing_index if c in details]

    # Append new codes found in detail files but not yet in index
    new_codes = [c for c in details if c not in existing_index]
    if new_codes:
        print(f"  新 ETF（加入 index）: {', '.join(sorted(new_codes))}")
    all_codes_ordered += sorted(new_codes)

    result = []
    for code in all_codes_ordered:
        entry = build_entry(
            code,
            detail=details.get(code),
            existing=existing_index.get(code),
        )
        result.append(entry)

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"✅ index.json 已重新生成，共 {len(result)} 筆")


if __name__ == "__main__":
    main()
