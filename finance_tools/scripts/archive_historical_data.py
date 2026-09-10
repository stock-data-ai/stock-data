"""把舊年度的三大法人與大戶明細搬到 archive，讓主檔不再無限成長。

**為什麼要有這支**：`historical.institutionalInvestors` 每天新增一筆、從來沒有東西刪舊的。
2026-09-10 實測：整個 `company-financials/` 是 **1.5 GB**，光三大法人就佔 **879 MB**
（每家平均 1544 個日期）。個股頁每開一家就要下載那一整包，而畫面上真正用得到的
最長只有籌碼疊圖的 6 個月（約 126 個交易日）。

**保留策略：當年 + 前一年**（也就是 12～24 個月，永遠不少於一年）。
為什麼不用「往回滾 365 天」——那會讓封存檔**每天都變動**：
2341 個 gzip 檔每天重寫一次，git 每次都存一份完整新 blob，一年下來會多出好幾 GB。
按整年切，某一年一旦被封存就**永遠不再變動**，git 只存一次。

**是搬不是刪。** 舊資料寫進 `company-financials-archive/{year}/{code}.json.gz`。
gzip 是因為這種資料重複度極高——實測壓縮比 **12.9x**，597 MB 壓成約 46 MB，
放進版控完全可以接受。

用法：uv run finance_tools/cli.py archive-history [--dry-run] [--limit=N]
      （冪等；沒有可封存的年度時什麼都不做）
"""

import gzip
import json
import logging
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from finance_tools.core.file_manager import FileManager
from finance_tools.core.timezone import now_tw

logger = logging.getLogger(__name__)

BASE = Path(__file__).resolve().parents[2] / "src/data/layer3"
ARCHIVE_DIR = BASE / "company-financials-archive"

#: 主檔保留「當年 + 前 KEEP_PAST_YEARS 年」。1 = 當年加去年。
KEEP_PAST_YEARS = 1

#: **所有以日期為 key 的歷史**都走這條，一套規則、一個下場。
#: key 是日期字串（`YYYY-MM-DD` 或 `YYYYMMDD`），前四碼就是年份。
ARCHIVABLE = (
    ("historical", "institutionalInvestors"),
    ("historical", "marginTrading"),
    ("historical", "securitiesLending"),
    (None, "shareholderDataHistory"),
)


def _year_of(key) -> int:
    try:
        return int(str(key)[:4])
    except (ValueError, TypeError):
        return -1


def _container(data: dict, path):
    """`("historical", "x")` → `data["historical"]`；`(None, "x")` → `data`。"""
    parent, _ = path
    return data.get(parent) if parent else data


def _split_by_year(records: dict, cutoff_year: int):
    """回傳 (要保留的, {年份: 要封存的})。年份解析不出來的一律保留。"""
    keep, archive = {}, defaultdict(dict)
    for key, value in records.items():
        year = _year_of(key)
        if 0 <= year < cutoff_year:
            archive[year][key] = value
        else:
            keep[key] = value
    return keep, dict(archive)


def _write_gz_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}_", suffix=".gz.tmp")
    try:
        with os.fdopen(fd, "wb") as raw:
            # mtime=0：同樣的內容要產生同樣的 bytes，否則每次執行都是新 blob。
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as gz:
                gz.write(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8"))
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read_gz(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        # 封存檔讀不回來就不要動它，也不要拿新資料覆蓋掉——寧可這次跳過。
        logger.error(f"封存檔損壞，跳過 {path}：{e}")
        raise


def process_file(file_mgr: FileManager, code: str, cutoff_year: int, dry_run: bool) -> dict:
    data = file_mgr.load_financial_data(code)
    if not data:
        # None（損壞）也走這裡：不碰它，等 financials-update 重抓。
        return {"code": code, "archived": {}, "skipped": True}

    to_archive = defaultdict(dict)
    updates = []
    for path in ARCHIVABLE:
        container = _container(data, path)
        if not isinstance(container, dict):
            continue
        records = container.get(path[1])
        if not isinstance(records, dict) or not records:
            continue
        keep, archive = _split_by_year(records, cutoff_year)
        if not archive:
            continue
        for year, entries in archive.items():
            to_archive[year][path[1]] = entries
        updates.append((container, path[1], keep))

    if not to_archive:
        return {"code": code, "archived": {}, "skipped": False}

    counts = {y: sum(len(v) for v in blocks.values()) for y, blocks in to_archive.items()}
    if dry_run:
        return {"code": code, "archived": counts, "skipped": False}

    # 先把封存寫成功，再動主檔——順序顛倒的話中途失敗就是真的少一段資料。
    for year, blocks in to_archive.items():
        out = ARCHIVE_DIR / str(year) / f"{code}.json.gz"
        merged = _read_gz(out)
        merged.setdefault("companyCode", code)
        for field, entries in blocks.items():
            merged.setdefault(field, {}).update(entries)
        _write_gz_atomic(out, merged)

    for container, field, keep in updates:
        container[field] = keep
    if not file_mgr.save_financial_data(code, data):
        raise RuntimeError(f"{code} 主檔寫入失敗；封存已寫好，重跑即可（冪等）")

    return {"code": code, "archived": counts, "skipped": False}


def run(dry_run: bool = False, limit=None) -> dict:
    file_mgr = FileManager()
    cutoff_year = now_tw().year - KEEP_PAST_YEARS
    codes = sorted(file_mgr.list_financial_codes())
    if limit:
        codes = codes[:limit]

    print(f"[archive] 保留 {cutoff_year} 年（含）以後；{cutoff_year - 1} 年（含）以前搬進 archive")
    total_records = 0
    touched = 0
    per_year = defaultdict(int)
    for code in codes:
        result = process_file(file_mgr, code, cutoff_year, dry_run)
        if result["archived"]:
            touched += 1
            for year, n in result["archived"].items():
                per_year[year] += n
                total_records += n

    verb = "預計搬" if dry_run else "已搬"
    print(f"[archive] {verb} {total_records} 筆／{touched} 家公司")
    for year in sorted(per_year, reverse=True):
        print(f"[archive]   {year}: {per_year[year]} 筆")
    if not touched:
        print("[archive] 沒有可封存的年度，什麼都沒做")
    return {"cutoff_year": cutoff_year, "records": total_records, "companies": touched}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    lim = next((int(a.split("=")[1]) for a in sys.argv[1:] if a.startswith("--limit=")), None)
    run(dry_run="--dry-run" in sys.argv, limit=lim)
