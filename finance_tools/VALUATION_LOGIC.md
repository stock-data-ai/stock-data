# 📈 台灣股市：市值與估值更新邏輯 (Valuation & Market Cap)

本文件說明 `stock-data` 專案中更新個股「市值」與「估值指標 (PE, PB, Yield)」的整合邏輯。

---

## 1. 核心目標
每日從 **Yahoo Finance** 同步獲取個股的最新市場數據，並精確更新至 `src/data/layer3/company-financials/{code}.json`。
此整合流程確保了市值與估值指標（本益比、股價淨值比、殖利率）的一致性與即時性。

---

## 2. 相關程式結構

| 角色 | 檔案路徑 | 功能說明 |
| :--- | :--- | :--- |
| **進入點 (CLI)** | `finance_tools/cli.py` | 指令 `update-marketcap` 現在會同時觸發市值與估值更新。 |
| **任務控管 (Task)** | `finance_tools/tasks/daily/marketcap.py` | 負責批次管理與迴圈執行。 |
| **核心流程 (Logic)** | `finance_tools/processing/company_processor.py` | `process_marketcap_only` 已升級為處理完整估值統計。 |
| **數據調度 (Orch)** | `finance_tools/processing/fetch_orchestrator.py` | `fetch_valuation_stats` 負責向 Fetcher 請求數據。 |
| **數據抓取 (Fetcher)**| `finance_tools/fetchers/yahoo_fetcher.py` | 透過 `yfinance` 取得 `marketCap`, `trailingPE`, `priceToBook`, `dividendYield`。 |
| **數據整合 (Merge)** | `finance_tools/processing/data_assembler.py` | `merge_valuation` 負責將多個指標填入 JSON 的 `latest` 區塊。 |

---

## 3. 執行流程 (Step-by-Step)

1.  **啟動**：每日自動化腳本執行 `update-marketcap`。
2.  **抓取數據**：調用 `YahooFetcher` 一次性取得所有關鍵指標。
3.  **數值轉換**：
    *   `trailingPE` -> `peRatio`
    *   `priceToBook` -> `pbRatio`
    *   `dividendYield` -> `dividendYield` (轉換為百分比)
4.  **數據整合**：更新 JSON 文件中的 `latest` 區塊與 `lastUpdated` 時間。
5.  **存檔**：完成更新。

---

## 4. 更新頻率與優勢

*   **頻率**：每週一至週五盤後執行。
*   **優勢**：
    *   **即時性**：不再需要等到週六才更新 PE/PB。
    *   **效能**：減少對 FinMind API 的依賴，降低被限流的風險。
    *   **簡化**：原本週六的 `update-valuation` 任務現在僅作為備援，核心數據已由每日任務覆蓋。

---

## 5. 常用指令

```bash
# 每日更新 (包含市值、PE、PB、殖利率)
uv run finance_tools/cli.py update-marketcap --code 2330
```
