# 📊 台灣股市：三大法人更新邏輯說明 (Institutional Investors Update)

本文件說明 `stock-data` 專案中更新「三大法人買賣超」與「持股比例」的核心邏輯、程式結構與執行步驟。

---

## 1. 核心目標
每日更新外資、投信、自營商在個股的買賣超數據，並根據買賣超張數與公司總股數，精確推估三大法人的最新持股比例。

---

## 2. 相關程式結構 (相關路徑)

| 角色 | 檔案路徑 | 功能說明 |
| :--- | :--- | :--- |
| **進入點 (CLI)** | `finance_tools/cli.py` | 定義 `update-institutional-investors` 指令。 |
| **任務控管 (Task)** | `finance_tools/tasks/daily/institutional_investors.py` | 負責批次管理、迴圈跑公司清單、API 額度監控。 |
| **核心流程 (Logic)** | `finance_tools/processing/company_processor.py` | 調用 `_build_ratios` 整合三大法人買賣超與比例。 |
| **計算引擎 (Calc)** | `finance_tools/processing/inst_ratio_calculator.py` | **核心演算法**：使用「種子值 + 累計買賣超」推算持股比。 |
| **數據抓取 (Fetcher)**| `finance_tools/fetchers/institutional_investors_shares.py` | 從 FinMind 抓取外資/投信/自營商每日買賣張數。 |
| **校正參考 (Ref)** | `finance_tools/fetchers/shareholding.py` | 抓取集保 (TDCC) 的大股東持股資料作為比例校正參考。 |
| **數據種子 (Data)** | `finance_tools/data/inst_ratio_seeds.json` | 存放各公司的初始持股基準值 (Seeds)。 |

---

## 3. 執行流程 (Step-by-Step)

1.  **啟動任務**：執行 `update-institutional-investors` 指令。
2.  **初始化計算器**：讀取 `inst_ratio_seeds.json` (種子值) 與 `companies-all.json` (總股數)。
3.  **抓取數據**：從 FinMind API 獲取指定日期範圍內（通常是最近 30-60 天）的三大法人每日交易明細。
4.  **比例推算 (Calculation)**：
    *   **公式**：`目前比例 = 初始比例 (Seed) + (累計買賣超張數 / 公司總張數)`。
    *   **校正**：若有最新的集保數據，會將其作為基準點重新校準，以消除長期買賣超紀錄與實際持股的誤差。
5.  **數據整合**：
    *   讀取現有的 `{code}.json`。
    *   使用 `DataAssembler.merge_institutional_investors` 更新 `institutional_investors` 陣列。
6.  **存檔**：將更新後的 JSON 寫回 `src/data/layer3/company-financials/` 目錄。

---

## 4. 數據更新細節

*   **資料來源**：FinMind API (主要)、TDCC 集保 (校正)。
*   **關鍵邏輯**：
    *   因為台灣官方不直接提供「每日」法人精確持股比例，本系統採用「增量計算」法。
    *   **種子值 (Seeds)** 非常重要，若推算的比例出現異常（如負數或過高），通常需要手動校對種子值。
*   **安全性**：此流程僅更新 `institutional_investors` 相關欄位，不影響營收、財報或市值數據。

---

## 5. 常用維護指令

```bash
# 更新特定公司的法人數據
uv run finance_tools/cli.py update-institutional-investors --code 2330

# 每日自動更新任務 (分批處理)
uv run finance_tools/cli.py update-institutional-investors --batch 1/4

# 若數據出現偏差，可使用 --force 強制重新從種子值推算
uv run finance_tools/cli.py update-institutional-investors --code 2330 --force
```
