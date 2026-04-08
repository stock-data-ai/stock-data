# Finance Tools 快速開始指南

歡迎使用 Finance Tools，這是一套用於自動化金融數據抓取與處理的工具。

## 🚀 快速開始

所有操作都透過 `finance_tools/cli.py` 執行。

### 常用指令概覽

以下是主要的指令及其功能：

| 指令                      | 功能描述                                                       | 範例                                                                 |
|---------------------------|----------------------------------------------------------------|----------------------------------------------------------------------|
| `update-company-info`     | 抓取台灣上市櫃、興櫃公司基本資料（首次設定或公司變動時）       | `uv run finance_tools/cli.py update-company-info`                    |
| `update-us-company-info`  | 抓取美國公司基本資料（需指定代碼，如 NVDA）                    | `uv run finance_tools/cli.py update-us-company-info --code NVDA`     |
| `update-jp-company-info`  | 抓取日本公司基本資料（需指定代碼，如 5201.JP）                 | `uv run finance_tools/cli.py update-jp-company-info --code 5201.JP`  |
| `full-update`             | 完整更新所有數據：財報、營收、股利、市值及**本地股價歷史**（每季財報後執行） | `uv run finance_tools/cli.py full-update`                            |
| `update-revenue`          | 更新月營收數據（每月 10 日後執行）                             | `uv run finance_tools/cli.py update-revenue`                         |
| `update-marketcap`        | 更新市值數據（每日收盤後執行，基於本地股價檔案計算，不需 API） | `uv run finance_tools/cli.py update-marketcap`                       |
| `update-valuation`        | 更新本益比(PE)/淨值比(PB)估值數據（每日收盤後執行）            | `uv run finance_tools/cli.py update-valuation`                       |
| `update-institutional-investors` | 更新三大法人買賣超數據（每日收盤後執行）                     | `uv run finance_tools/cli.py update-institutional-investors`         |
| `fetch-shareholder-data`  | 抓取股權分散表數據（每週執行）                                 | `uv run finance_tools/cli.py fetch-shareholder-data`                 |
| `import-dividends`        | 從本地 CSV 檔案匯入股利資料（手動操作）                        | `uv run finance_tools/cli.py import-dividends`                       |
| `update-stock-prices`     | 抓取台股歷史股價數據並儲存至本地（最多三個月）                 | `uv run finance_tools/cli.py update-stock-prices`                    |
| `check-quality`           | 檢查數據品質，並將失敗的公司記錄下來以便重新執行               | `uv run finance_tools/cli.py check-quality`                          |

### 通用參數 (Common Parameters)

這些參數可以搭配上述指令使用，提供更精細的控制：

| 參數           | 說明                                         |
|----------------|----------------------------------------------|
| `--code <ID>`    | 只處理單一公司 (例如: `--code 2330`)         |
| `--topic <主題>`  | 只處理特定主題內的所有公司 (例如: `--topic defense_industry`) |
| `--limit <數量>` | 限制處理的公司數量，主要用於測試 (例如: `--limit 5`) |
| `--force`        | 強制更新，即使資料已存在或近期更新過         |
| `--rerun`        | 重新執行上次 `check-quality` 標記為失敗的公司  |

### 進階用法範例

#### 處理特定公司或主題
```bash
# 完整強制更新台積電 (2330) 的所有數據
uv run finance_tools/cli.py full-update --code 2330 --force

# 更新「AI」主題內所有公司的月營收數據
uv run finance_tools/cli.py update-revenue --topic AI
```

#### 測試與錯誤處理
```bash
# 測試性地更新前 5 家公司的估值
uv run finance_tools/cli.py update-valuation --limit 5

# 檢查所有數據品質，並重新處理所有失敗的項目
uv run finance_tools/cli.py check-quality
uv run finance_tools/cli.py full-update --rerun
```

## 🗓️ 建議更新時程 (與自動化排程對應)

為了保持數據最新，建議按照以下頻率執行指令。這些指令與 `.github/workflows/data-pipeline.yml` 中的自動化排程相對應：

*   **每日 (週一至週五，收盤後) (GitHub Actions: 台灣時間 07:00 及 23:00):**
    *   `uv run finance_tools/cli.py update-stock-prices` (抓取台股歷史股價數據並儲存至本地)
    *   `uv run finance_tools/cli.py update-marketcap` (更新市值)
    *   `uv run finance_tools/cli.py update-institutional-investors` (更新三大法人買賣超數據)
*   **每週六 (GitHub Actions: 台灣時間 09:00):**
    *   `uv run finance_tools/cli.py update-valuation` (更新本益比(PE)/淨值比(PB)估值數據)
    *   `uv run finance_tools/cli.py fetch-shareholder-data` (抓取股權分散表數據)
*   **每週日 (大更新) (GitHub Actions: 台灣時間 09:00):**
    *   `uv run finance_tools/cli.py full-update` (完整更新所有數據：財報、營收、市值、股利)
*   **首次設定 / 不定期：** `update-company-info` (包含美股、日股)
*   **每月 10 日後：** `update-revenue`
