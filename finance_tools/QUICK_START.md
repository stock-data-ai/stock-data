# Finance Tools 快速開始指南

所有操作都透過 `finance_tools/cli.py` 執行（Python 一律 `uv run`）。

> [!IMPORTANT]
> **指令清單以 `finance_tools/cli.py` 的 argparse 為準**（`uv run finance_tools/cli.py --help`）。
> 本表是依 2026-09-11 的 parser 整理的摘要；兩者不一致時以 parser 為準。
> 更新類指令會抓外部資料並**寫入本地 JSON／佇列**，不是驗證手段——見 repo 根目錄 `CLAUDE.md`。

## 🚀 常用指令

| 指令 | 功能（取自 parser 的 help） | 範例 |
|---|---|---|
| `update-marketcap-inst` | 每日更新：市值估值＋三大法人（TWSE/TPEx 批次），單次 load/save | `uv run finance_tools/cli.py update-marketcap-inst --code 2330` |
| `financials-update` | 更新損益表、月營收（**不是**全領域更新） | `uv run finance_tools/cli.py financials-update --code 2330` |
| `update-revenue` | 僅更新月營收 | `uv run finance_tools/cli.py update-revenue --code 2330` |
| `update-balance-sheet` | 從 FinMind 抓資產負債表＋現金流量表，計算 ROE/ROA/流動比率/負債比率/OCF/FCF | `uv run finance_tools/cli.py update-balance-sheet --code 2330` |
| `update-dividends` | 從 MOPS 抓 2025+ 股利並併入 JSON | `uv run finance_tools/cli.py update-dividends` |
| `fetch-shareholder-data` | 擷取 TDCC 股東分配資料 | `uv run finance_tools/cli.py fetch-shareholder-data` |
| `update-insider-holdings` | 更新內部人持股餘額（董監、經理人、大股東） | `uv run finance_tools/cli.py update-insider-holdings --force` |
| `update-margin` | 更新融資融券 | `uv run finance_tools/cli.py update-margin --date 2026-09-10` |
| `update-lending` | 更新借券賣出餘額 | `uv run finance_tools/cli.py update-lending --date 2026-09-10` |
| `update-market-sentiment` | 整體市場情緒：三大法人買賣超＋融資融券加總 | `uv run finance_tools/cli.py update-market-sentiment` |
| `compute-big-holders` | 從 TDCC 股權分散表計算大戶加碼排行 | `uv run finance_tools/cli.py compute-big-holders` |
| `check-quality` | 檢查資料品質並**產生失敗佇列**（會寫檔） | `uv run finance_tools/cli.py check-quality` |
| `prune-financials` | 刪掉已不在 `companies-all.json` 名單上的財報檔（`--dry-run` 只列出） | `uv run finance_tools/cli.py prune-financials --dry-run` |
| `archive-history` | 舊年度日期型歷史搬進 archive，主檔只留當年＋前一年（`--dry-run` 不寫檔） | `uv run finance_tools/cli.py archive-history --dry-run` |
| `generate-chip-topic`／`generate-disposition-forecast` | 產出並推送至 stock_map；`--dry-run` 仍寫 `/tmp`，只跳過推送 | `uv run finance_tools/cli.py generate-chip-topic --dry-run` |

一次性／回補用：`import-historical-dividends`、`backfill-dividends`、`backfill-foreign-shares`、`update-margin-history`（用途見 parser help）。

舊版文件中的 full-update、update-marketcap、update-institutional-investors、update-company-info
（含美股／日股版本）、import-dividends、update-stock-prices **已不存在於 parser**，不要再使用。

### 通用參數

`--code`、`--topic`、`--limit`、`--batch N/M` 由 `add_common_arguments` 加在
`update-marketcap-inst`、`financials-update`、`update-revenue`、`update-balance-sheet`、`fetch-shareholder-data`、
`update-insider-holdings`、`update-margin`、`update-lending`、`backfill-foreign-shares` 上。

| 參數 | 說明 |
|---|---|
| `--code <ID>` | 只處理單一公司 |
| `--topic <主題>` | 只處理特定主題內的公司 |
| `--limit <數量>` | 限制處理家數。**只是少跑幾家，仍會寫正式 JSON，不是 dry-run** |
| `--batch N/M` | 批次處理（CI 用，例如 `2/4`） |
| `--force` | 忽略「今日已更新」檢查（部分指令才有） |
| `--rerun` | 從失敗佇列重跑（`update-marketcap-inst`、`financials-update`、`update-revenue`、`update-balance-sheet`、`fetch-shareholder-data`） |

## 🗓️ 自動化排程

排程時間**不寫在這裡**：以 `cron/wrangler.toml` 與 `cron/src/index.ts`（stock-data-cron Worker）
及其 dispatch 的 `.github/workflows/` 為準。已部署的 Worker 版本需另行查證，見 `CLAUDE.md` 的 Schedules 一節。
