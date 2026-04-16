# 📈 台灣股市：市值更新邏輯說明 (Market Cap Update)

本文件說明 `stock-data` 專案中更新個股最新市值的核心邏輯、程式結構與執行步驟。

---

## 1. 核心目標
每日從 **Yahoo Finance** 取得個股最新市值數據，並精確更新至 `src/data/layer3/company-financials/{code}.json` 文件中的 `metadata` 欄位。

---

## 2. 相關程式結構 (相關路徑)

目前的架構採用「職責分離」，各司其職：

| 角色 | 檔案路徑 | 功能說明 |
| :--- | :--- | :--- |
| **進入點 (CLI)** | `finance_tools/cli.py` | 定義 `update-marketcap` 指令並派發任務。 |
| **任務控管 (Task)** | `finance_tools/tasks/daily/marketcap.py` | 負責批次管理、迴圈跑公司清單、錯誤紀錄與重試機制。 |
| **核心流程 (Logic)** | `finance_tools/processing/company_processor.py` | 定義 `process_marketcap_only` 方法，串接抓取、整合與存檔。 |
| **數據抓取 (Fetcher)**| `finance_tools/fetchers/yahoo_fetcher.py` | 封裝 `yfinance` 邏輯，負責與 Yahoo 伺服器通訊獲取數據。 |
| **數據整合 (Merge)** | `finance_tools/processing/data_assembler.py` | 負責將抓到的數值正確填入 JSON 的資料結構中而不破壞原資料。 |
| **檔案管理 (IO)** | `finance_tools/core/file_manager.py` | 負責 JSON 文件的讀取、原子性寫入與更新時間檢查。 |

---

## 3. 執行流程 (Step-by-Step)

1.  **啟動任務**：使用者或 GitHub Action 透過 CLI 執行 `update-marketcap`。
2.  **選取清單**：根據參數（`--code`, `--topic`, `--batch`）從 `companies-all.json` 決定要處理的公司。
3.  **過濾檢查**：檢查該公司今日是否已更新。若非強制更新 (`--force`) 則跳過，節省 API 呼叫。
4.  **抓取數據**：調用 `YahooFetcher` 透過 Yahoo Finance API (`{code}.TW` 或 `{code}.TWO`) 直接取得最新市值。
5.  **數據整合**：
    *   讀取現有的 `{code}.json`。
    *   使用 `DataAssembler.merge_marketcap` 將新市值填入 `metadata.market_cap`。
    *   更新 `metadata.last_updated` 為當前時間。
6.  **安全存檔**：使用 `FileManager` 將更新後的資料寫回磁碟，並執行 `clean_nan` 確保資料格式正確。

---

## 4. 數據更新細節

*   **資料來源**：Yahoo Finance (穩定且無頻繁額度限制)。
*   **更新欄位**：
    *   `metadata.market_cap`: 最新市值數值。
    *   `metadata.last_updated`: 最後更新日期/時間。
*   **失敗處理**：
    *   若 Yahoo 抓不到數據，會記錄在 `quality_report` 並加入 `rerun_queue_marketcap.txt` 等待下次重試。
    *   **安全性**：此流程僅更新市值，**絕不**更動財務報表、營收或股利等歷史數據。

---

## 5. 常用維護指令

```bash
# 更新單一公司 (例如 台積電)
uv run finance_tools/cli.py update-marketcap --code 2330

# 強制更新所有 BBU 概念股的市值
uv run finance_tools/cli.py update-marketcap --topic bbu --force

# 執行每日自動化更新 (CI 腳本使用，分 4 個批次執行)
uv run finance_tools/cli.py update-marketcap --batch 1/4
```
