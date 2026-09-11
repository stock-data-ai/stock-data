"""
File Manager - 統一的檔案管理
"""

import os
import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)
from finance_tools.core.timezone import today_str
import finance_tools.config as config

# 名單的唯一來源是 stock_map 的 companies-all.json（政府上市／上櫃／興櫃三份名單的鏡像，
# 新上市自動加、下市自動刪）。CI 每次先抓最新版放到 config.COMPANIES_ALL_FILE；
# 本機沒抓時退到隔壁 checkout——兩個 repo 是同一系統，慣例放同一層。
_SIBLING_COMPANIES_ALL = (
    Path(__file__).resolve().parents[3] / "stock_map/src/data/layer3/companies/companies-all.json"
)

# 台股非 ETF 的合理下界（2026-09-09 實測 2341）。低於此視為抓到殘缺檔，寧可整批失敗，
# 也不要拿半份名單去跑 prune 把一半的財報檔刪掉。
ROSTER_MIN_TW = 2000


def resolve_companies_all_path() -> Optional[str]:
    """CI 抓下來的那份優先；沒有就找隔壁的 stock_map。兩邊都沒有回 None，由呼叫端決定要不要炸。"""
    primary = str(config.COMPANIES_ALL_FILE)
    if os.path.exists(primary):
        return primary
    if _SIBLING_COMPANIES_ALL.exists():
        logger.info(f"companies-all.json 使用隔壁 stock_map 的檔案：{_SIBLING_COMPANIES_ALL}")
        return str(_SIBLING_COMPANIES_ALL)
    return None


class FileManager:
    """檔案管理器"""

    def __init__(self, base_dir: str = None):
        self.base_dir = base_dir or str(config.BASE_DIR)
        self.companies_dir = str(config.COMPANIES_DIR)
        self.company_topics_base_dir = str(config.COMPANY_TOPICS_DIR)
        self.financials_dir = str(config.COMPANY_FINANCIALS_DIR)

        # 確保目錄存在
        os.makedirs(self.financials_dir, exist_ok=True)
        os.makedirs(self.company_topics_base_dir, exist_ok=True) # Ensure this also exists

    def load_companies(self) -> List[Dict[str, str]]:
        """
        台股名單：companies-all.json 裡 4 碼數字、非 ETF 的那批。

        **刻意不掃 company-financials/ 目錄。** 2026-09-09 之前這裡是掃目錄的，
        結果名單變成自我延續：檔案在就繼續抓、檔案不在就永遠進不來——
        下市 16 檔照抓（TDCC 對已終止買賣的個股照發資料，看起來跟活的一樣），
        2026 年掛牌的 58 檔一個都沒建檔。目錄是儲存位置，不是名單。

        名單缺席或殘缺時直接拋出，不退回掃目錄——那等於把病根接回去。
        """
        all_companies_details = self.load_all_companies_with_details()
        if not all_companies_details:
            raise RuntimeError(
                "找不到 companies-all.json。CI 應先執行 .github/actions/fetch-roster；"
                f"本機請把 stock_map checkout 放在 {_SIBLING_COMPANIES_ALL.parents[4]} 底下。"
            )

        companies = [
            {"code": code, "name": detail.get("name") or code}
            for code, detail in all_companies_details.items()
            if code.isdigit() and len(code) == 4 and not detail.get("isETF")
        ]
        if len(companies) < ROSTER_MIN_TW:
            raise RuntimeError(
                f"companies-all.json 台股只有 {len(companies)} 檔（下界 {ROSTER_MIN_TW}），"
                "疑似抓到殘缺檔，本次不處理任何公司。"
            )

        companies.sort(key=lambda c: c["code"])
        logger.info(f"名單：companies-all.json 台股 {len(companies)} 檔")
        return companies

    def list_financial_codes(self) -> set[str]:
        """company-financials/ 目錄裡實際存在的 4 碼台股檔。這是「儲存了什麼」，不是名單。"""
        if not os.path.isdir(self.financials_dir):
            return set()
        codes = (fn[:-5] for fn in os.listdir(self.financials_dir) if fn.endswith(".json"))
        return {c for c in codes if c.isdigit() and len(c) == 4}

    def load_all_companies_with_details(self) -> Dict[str, Any]:
        """
        載入 companies-all.json 檔案以獲取完整的公司詳細資訊。
        """
        file_path = resolve_companies_all_path()
        if file_path is None:
            logger.error(f"Error: companies-all.json not found at {config.COMPANIES_ALL_FILE}（隔壁 stock_map 也沒有）")
            return {}
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding JSON from {file_path}: {e}")
            return {}
        except Exception as e:
            logger.error(f"Error reading {file_path}: {e}")
            return {}

    def save_all_companies_data(self, data: Dict[str, Any]) -> bool:
        """
        儲存所有公司的資料到 companies-all.json
        """
        import tempfile
        output_path = str(config.COMPANIES_ALL_FILE)
        try:
            fd, temp_path = tempfile.mkstemp(
                dir=os.path.dirname(output_path),
                prefix=".companies-all_",
                suffix=".json.tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, output_path)
            except Exception:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                raise
            logger.info(f"✅ Successfully saved all company data to {output_path}")
            return True
        except Exception as e:
            logger.error(f"Error saving all companies data to {output_path}: {e}")
            return False

    def save_financial_data(self, code: str, data: Dict[str, Any]) -> bool:
        """儲存財務數據（使用原子寫入）"""
        import tempfile
        try:
            output_path = os.path.join(self.financials_dir, f"{code}.json")

            # 使用原子寫入：先寫入臨時檔案，然後原子性地重命名
            # 這樣即使寫入過程中出錯，原檔案也不會損壞
            fd, temp_path = tempfile.mkstemp(
                dir=self.financials_dir,
                prefix=f".{code}_",
                suffix=".json.tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())  # 確保數據真正寫入磁盤

                # 原子性地重命名臨時檔案到目標檔案
                # 在 POSIX 系統上，這是原子操作
                os.replace(temp_path, output_path)
                return True
            except Exception:
                # 如果出錯，清理臨時檔案
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                raise
        except Exception as e:
            logger.error(f"Error saving data for {code}: {e}")
            return False

    def load_financial_data(self, code: str) -> Optional[Dict[str, Any]]:
        """載入財務數據。**回傳值有三種，呼叫端必須分辨：**

          - `{}`    檔案不存在 → 新公司，可以建新檔。
          - `None`  檔案在、但**解析失敗**（損壞）→ 這家公司的既有歷史暫時讀不到。
                    **絕對不可以**拿這次抓到的片段去覆蓋——抓取視窗只有一年，
                    覆蓋下去就是把多年的季報／月營收無聲換成一年份。
          - dict    正常。

        壞檔**留在原地不動**：每一輪讀到都會再回 `None`，所有寫入者持續退開，
        直到 `financials-update` 重抓、原子寫入蓋過去為止。

        `financials-update` 決定抓多長**不看這個三態**，看的是「檔裡的歷史夠不夠」
        （`company_processor.history_is_short`）。壞檔視同沒有歷史，所以會用完整視窗；
        萬一那一輪抓失敗寫出空殼，空殼一樣「不夠」，下一輪照樣放寬——訊號不會消失。
        重抓只救得回季報／年報／月營收；要連融資融券、大戶、股利一起救，從 git 拿上一版。

        （曾經試過把壞檔改名成 `.corrupt` 保存，那是錯的：下一輪就變成
        「檔案不存在」＝新公司，日更會建一份只有市值的空殼檔。）
        """
        file_path = os.path.join(self.financials_dir, f"{code}.json")
        if not os.path.exists(file_path):
            return {}
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # 不動這個檔案——它是「要重抓」的訊號，financials-update 會用完整視窗蓋過去。
            logger.error(f"[{code}] 財報檔損壞（{e}）；等 financials-update 用完整視窗重抓")
            return None
        except Exception as e:
            # 權限、IO 等暫時性問題：不動檔案，也不讓呼叫端拿空資料去覆蓋。
            logger.error(f"Error loading data for {code}: {e}")
            return None

    def file_exists(self, code: str) -> bool:
        """檢查檔案是否存在"""
        file_path = os.path.join(self.financials_dir, f"{code}.json")
        return os.path.exists(file_path)

    def is_updated_today(self, code: str) -> bool:
        """檢查檔案的 lastUpdated 是否為今天日期"""
        data = self.load_financial_data(code)
        if not data:
            return False
        last_updated = data.get("lastUpdated", "")
        return last_updated == today_str()

    def delete_file(self, code: str) -> bool:
        """刪除檔案"""
        try:
            file_path = os.path.join(self.financials_dir, f"{code}.json")
            if os.path.exists(file_path):
                os.remove(file_path)
                return True
            return False
        except Exception as e:
            logger.error(f"Error deleting file for {code}: {e}")
            return False