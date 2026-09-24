"""
一次性種子萃取腳本：Phase 1

從 src/data/layer3/company-financials/*.json 讀取每支股票最後一筆有效的
trust_ratio / dealer_ratio，結合 companies-all.json 的 issuedCommonShares
反推持股張數，輸出 finance_tools/assets/seeds/inst_ratio_seeds.json。

用法：
    uv run finance_tools/scripts/extract_seeds.py

執行一次即可；種子檔建立後此腳本不再需要。
"""

import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent.parent
FINANCIALS_DIR = REPO_ROOT / "src/data/layer3/company-financials"
COMPANIES_FILE = REPO_ROOT / "src/data/layer3/companies/companies-all.json"
OUTPUT_FILE = REPO_ROOT / "finance_tools/assets/seeds/inst_ratio_seeds.json"


def load_companies() -> dict:
    with open(COMPANIES_FILE, encoding="utf-8") as f:
        return json.load(f)


def extract_seeds() -> dict:
    companies = load_companies()
    seeds = {}
    missing_shares = []

    files = sorted(FINANCIALS_DIR.glob("*.json"))
    logger.info(f"處理 {len(files)} 支股票...")

    for path in files:
        code = path.stem
        company = companies.get(code, {})
        issued_shares = (
            company.get("gov", {})
            .get("capital", {})
            .get("issuedCommonShares")
        )

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"{code}: 讀取失敗 {e}")
            seeds[code] = {"seed_date": None, "trust_shares": 0, "dealer_shares": 0}
            continue

        ii = data.get("historical", {}).get("institutionalInvestors") or {}

        # 找最後一筆有效的 trust_ratio（不為 None）
        last_date = None
        last_trust_ratio = None
        last_dealer_ratio = None
        for date in sorted(ii.keys(), reverse=True):
            entry = ii[date]
            if entry.get("trust_ratio") is not None:
                last_date = date
                last_trust_ratio = entry["trust_ratio"]
                last_dealer_ratio = entry.get("dealer_ratio") or 0.0
                break

        if last_date is None or issued_shares is None:
            if issued_shares is None:
                missing_shares.append(code)
            seeds[code] = {"seed_date": None, "trust_shares": 0, "dealer_shares": 0}
            continue

        # 反推持股張數（單位：張 = 1000 股）
        # trust_ratio 是百分比，issuedCommonShares 是股數
        # 持股張數 = ratio / 100 * issuedCommonShares / 1000
        trust_shares = round(last_trust_ratio / 100 * issued_shares / 1000)
        dealer_shares = round(last_dealer_ratio / 100 * issued_shares / 1000)

        seeds[code] = {
            "seed_date": last_date,
            "trust_shares": trust_shares,
            "dealer_shares": dealer_shares,
        }

    logger.info(f"種子值完整: {sum(1 for v in seeds.values() if v['seed_date'])} 支")
    logger.info(f"無 ratio 歷史（種子=0）: {sum(1 for v in seeds.values() if not v['seed_date'])} 支")
    if missing_shares:
        logger.warning(f"找不到 issuedCommonShares 的股票（前10）: {missing_shares[:10]}")

    return seeds


def main():
    seeds = extract_seeds()
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(seeds, f, ensure_ascii=False, indent=2)
    logger.info(f"輸出至 {OUTPUT_FILE}（共 {len(seeds)} 筆）")


if __name__ == "__main__":
    main()
