"""
將 company-financials/{code}.json 中的三大法人與大戶歷史資料，
按年份拆分到 company-financials-archive/{year}/{code}.json。

只處理：
  - historical.institutionalInvestors (三大法人，key=日期字串)
  - shareholderDataHistory (大戶，key=YYYYMMDD)

財務資料 (annual/quarterly/monthlyRevenue/dividends) 不動。
主檔保留 >= CUTOFF_YEAR 的資料，舊的移到 archive。
"""

import json
import sys
from pathlib import Path
from collections import defaultdict

BASE = Path(__file__).parent.parent.parent / "src/data/layer3"
SRC_DIR = BASE / "company-financials"
ARCHIVE_DIR = BASE / "company-financials-archive"
CUTOFF_YEAR = 2025  # 保留 >= 這個年份


def get_year(s: str):
    try:
        return int(str(s)[:4])
    except (ValueError, TypeError):
        return None


def split_dict_by_year(d: dict):
    by_year = defaultdict(dict)
    for k, v in d.items():
        y = get_year(k)
        if y is not None:
            by_year[y][k] = v
    return dict(by_year)


def process_file(src_path: Path, dry_run: bool = False) -> dict:
    with open(src_path, encoding="utf-8") as f:
        data = json.load(f)

    code = data.get("companyCode", src_path.stem)
    hist = data.get("historical", {})

    # 分年收集 archive 資料（只包含三大法人與大戶）
    archive_by_year = defaultdict(lambda: {
        "companyCode": code,
        "companyName": data.get("companyName", ""),
    })

    # 三大法人
    ii = hist.get("institutionalInvestors")
    ii_kept = {}
    if isinstance(ii, dict):
        by_year = split_dict_by_year(ii)
        for y, entries in by_year.items():
            if y < CUTOFF_YEAR:
                archive_by_year[y]["institutionalInvestors"] = entries
            else:
                ii_kept.update(entries)

    # 大戶
    sdh = data.get("shareholderDataHistory")
    sdh_kept = {}
    if isinstance(sdh, dict):
        by_year = split_dict_by_year(sdh)
        for y, entries in by_year.items():
            if y < CUTOFF_YEAR:
                archive_by_year[y]["shareholderDataHistory"] = entries
            else:
                sdh_kept.update(entries)

    archived_years = sorted(archive_by_year.keys())

    if not dry_run and archived_years:
        # 寫入 archive 檔案
        for year, archive_data in archive_by_year.items():
            year_dir = ARCHIVE_DIR / str(year)
            year_dir.mkdir(parents=True, exist_ok=True)
            out_path = year_dir / f"{code}.json"
            # 若已存在則合併（避免重複執行時覆蓋）
            if out_path.exists():
                with open(out_path, encoding="utf-8") as f:
                    existing = json.load(f)
                if "institutionalInvestors" in archive_data:
                    existing.setdefault("institutionalInvestors", {}).update(
                        archive_data["institutionalInvestors"]
                    )
                if "shareholderDataHistory" in archive_data:
                    existing.setdefault("shareholderDataHistory", {}).update(
                        archive_data["shareholderDataHistory"]
                    )
                archive_data = existing
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(archive_data, f, ensure_ascii=False, indent=2)

        # 更新主檔：只保留 CUTOFF_YEAR+ 的三大法人與大戶
        if isinstance(ii, dict):
            data["historical"]["institutionalInvestors"] = ii_kept
        if isinstance(sdh, dict):
            data["shareholderDataHistory"] = sdh_kept

        with open(src_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    return {"code": code, "archived_years": archived_years}


def main():
    dry_run = "--dry-run" in sys.argv
    limit = None
    for arg in sys.argv[1:]:
        if arg.startswith("--limit="):
            limit = int(arg.split("=")[1])

    src_files = sorted(SRC_DIR.glob("*.json"))
    if limit:
        src_files = src_files[:limit]

    total = len(src_files)
    print("%sProcessing %d files..." % ("[DRY RUN] " if dry_run else "", total))

    year_counts = defaultdict(int)
    for i, src_path in enumerate(src_files, 1):
        result = process_file(src_path, dry_run=dry_run)
        for y in result["archived_years"]:
            year_counts[y] += 1
        if i % 200 == 0 or i == total:
            print("  %d/%d done" % (i, total))

    print("\nArchive summary (files per year):")
    for y in sorted(year_counts):
        print("  %d: %d files" % (y, year_counts[y]))
    print("Done!")


if __name__ == "__main__":
    main()