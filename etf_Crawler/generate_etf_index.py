"""
generate_etf_index.py

從 src/data/etf/{code}.json 重新生成 src/data/etf/index.json。

{code}.json 是 single source of truth for:
  code, name, assetClass, categoryId, trackingIndex,
  managementFee, dividendFrequency, fundSize, issuer

現有 index.json 保留以下欄位（無其他爬蟲維護）：
  trailingYield, beneficiaryCount

ETF 清單順序與現有 index.json 一致；新 ETF 附加到末尾。

用法：
    uv run python etf_Crawler/generate_etf_index.py
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
ETF_DATA_DIR = REPO_ROOT / "src/data/etf"
INDEX_PATH = ETF_DATA_DIR / "index.json"

# Fields where the crawler ({code}.json / MoneyDJ) is authoritative
CRAWLER_FIELDS = ["managementFee", "dividendFrequency", "fundSize", "issuer"]

# Fields where stock_map (existing index.json) is authoritative; {code}.json used only as fallback
MANUAL_FIELDS = ["name", "assetClass", "categoryId", "trackingIndex"]

# Fields that have no crawler source — always kept from existing index
PRESERVED_FIELDS = ["trailingYield", "beneficiaryCount"]


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


def build_entry(code: str, detail: dict | None, existing: dict | None) -> dict:
    entry: dict = {"code": code}

    # Crawler-authoritative: {code}.json wins, fallback to existing
    for field in CRAWLER_FIELDS:
        if detail is not None and detail.get(field) not in (None, ""):
            entry[field] = detail[field]
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

    all_codes_ordered = list(existing_index.keys())

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
