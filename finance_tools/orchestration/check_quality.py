import os
import logging
import json
import urllib.request
import finance_tools.config as config
from finance_tools.utils.rerun_manager import RerunManager

logger = logging.getLogger(__name__)

# 官方現存名單三份（上市／上櫃／興櫃）。用來抓「檔案還在、公司已經不在名單上」的殘留。
_LISTING_SOURCES = [
    "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_R",
]
# TPEx 會重置機房 IP 的預設 Python UA，一定要帶瀏覽器 UA（見 domains/insider/fetcher.py）
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _report_delisted(output_dir):
    """列出「財報檔還在、但代號已不在官方三份名單上」的公司。

    只報告不刪檔：下市股的歷史資料可能還有人持有、還有引用，該不該清是人的判斷。
    任何一份名單抓失敗就整個跳過——名單不完整會把整批活著的公司誤判成下市。

    2026-09-06 首次執行抓到 18 個，其中 12 個的 `companyName` 已退化成代號本身
    （2867 三商壽、3454 晶睿、5371 中強光電、6806 森崴能源等）。
    """
    live = set()
    for url in _LISTING_SOURCES:
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            rows = json.loads(urllib.request.urlopen(req, timeout=60).read())
        except Exception as e:
            logger.warning(f"下市殘留檢查：{url} 抓取失敗（{e}），本次跳過")
            return
        for r in rows:
            code = (r.get("公司代號") or r.get("SecuritiesCompanyCode") or "").strip()
            if code:
                live.add(code)

    orphans = []
    for filename in sorted(os.listdir(output_dir)):
        if not filename.endswith(".json"):
            continue
        code = filename[:-5]
        # 只查純數字代號：ETF 與外國股另有規則，不在這三份名單裡是正常的
        if not code.isdigit() or code in live:
            continue
        try:
            with open(os.path.join(output_dir, filename), encoding="utf-8") as f:
                name = json.load(f).get("companyName") or ""
        except Exception:
            name = ""
        orphans.append((code, name))

    if not orphans:
        logger.info("下市殘留檢查：無殘留")
        return
    logger.warning(f"下市殘留：{len(orphans)} 個財報檔的代號已不在官方名單上（只報告、不刪檔）")
    for code, name in orphans:
        logger.warning(f"  - {code} {name}")

def run_check_quality(args):
    """
    處理數據品質檢查任務
    - 檢查 company-financials 目錄下的所有 JSON 檔案。
    - 找出 dataQuality 為 "low" 的公司。
    - 將這些公司的代碼寫入 rerun queue 以便後續處理。
    """
    OUTPUT_DIR = str(config.COMPANY_FINANCIALS_DIR)

    rerun_mgr = RerunManager("quality")

    logger.info("Checking data quality...")
    low_quality_companies = []

    if not os.path.exists(OUTPUT_DIR):
        logger.error(f"Output directory not found: {OUTPUT_DIR}")
        return

    _report_delisted(OUTPUT_DIR)

    for filename in os.listdir(OUTPUT_DIR):
        if filename.endswith(".json"):
            file_path = os.path.join(OUTPUT_DIR, filename)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("dataQuality") == "low":
                        low_quality_companies.append({
                            "code": data.get("companyCode"),
                            "name": data.get("companyName"),
                            "file": filename
                        })
            except Exception as e:
                logger.error(f"Error reading {filename}: {e}")

    if low_quality_companies:
        logger.warning("Companies with low data quality:")
        codes_to_rerun = []
        for company in low_quality_companies:
            logger.warning(f"- {company['code']} {company['name']} (File: {company['file']})")
            if company['code']:
                codes_to_rerun.append(company['code'])

        rerun_mgr.save(codes_to_rerun)
        logger.warning(f"\nFound {len(codes_to_rerun)} companies with issues. Their codes have been saved to '{rerun_mgr.file_path}' for the rerun process.")

    else:
        logger.info("All processed companies have high data quality.")
        rerun_mgr.clear()
