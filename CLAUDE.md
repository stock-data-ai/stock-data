# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Taiwan stock financial data pipeline that fetches data from FinMind API, TDCC, and other sources, processes it, and outputs JSON files to GitHub Pages as a static API. Paired with a private `stock_map` repo that handles analysis and UI.

## Agent safety / Cross-workspace instructions

This pipeline and sibling `stock_map` are one system. For cross-repo data operations read stock_map's `AGENTS.md` / `CLAUDE.md` and `.claude/skills/_shared/agent-safety.md` (Q2/Q3); this file supplies data-workspace details, not an override of those safety restrictions. If the paired checkout is unavailable or instructions conflict, stop the affected write and report it; do not infer policy from historical notes.

Company-topic content follows stock_map's `docs/guides/COMPANY_TOPIC_STANDARD.md`, even when initiated here. Local copies of app metadata/topic indexes are inputs, not a new app source of truth; confirm the input version before use. Financial history and ETF accumulated metadata are not assumed rebuildable from a fresh clone.

## Package Manager

This project uses `uv` (not pip). Always use `uv run` to execute Python scripts.

```bash
uv sync --all-extras --dev   # Install dependencies
uv run finance_tools/cli.py [COMMAND] [OPTIONS]
```

## Common Commands

```bash
# Income statement / revenue update (writes local financial JSON; not the former all-domain update)
uv run finance_tools/cli.py financials-update --code 2330

# Topic-based update
uv run finance_tools/cli.py update-marketcap-inst --topic bbu

# Batch processing (used in CI — batch N of 4)
uv run finance_tools/cli.py financials-update --batch 2/4

# Rerun companies that failed in previous run
uv run finance_tools/cli.py financials-update --rerun --batch 1/4

# Force update (bypass "already updated today" guard)
uv run finance_tools/cli.py update-marketcap-inst --force

# Limit number of companies (for quick testing)
uv run finance_tools/cli.py update-revenue --limit 10

# 內部人持股（月頻全量，一次拿全市場再逐家併進 company-financials）
uv run finance_tools/cli.py update-insider-holdings --force

# 日期型歷史封存：主檔只留當年＋前一年，更舊的整年搬進 company-financials-archive/（gzip）
# daily-update 週一~週六會自動跑（週日沒新資料，2026-09-13 起不跑）；--dry-run 真的不寫任何檔
uv run finance_tools/cli.py archive-history --dry-run

# 下市清除：名單上沒有的公司刪主檔，連同封存一起刪
uv run finance_tools/cli.py prune-financials --dry-run

# Run tests
uv run pytest finance_tools/tests/
```

These examples use current parser commands in `finance_tools/cli.py`; older `full-update`, `update-marketcap` and `update-institutional-investors` examples in legacy guides are not executable instructions. `financials-update` does not mean “all domains”.

Update commands fetch external data and write local JSON / queues; they are not validation. `check-quality` also writes queues. The `generate-chip-topic` / `generate-disposition-forecast` commands can push outputs into stock_map; their `--dry-run` writes temporary outputs while skipping that push, not zero filesystem effects. CI commit/push can publish data; news crawlers can write remote D1. Inspect the exact task before running it, and never run updates or remote writes merely to verify instructions.

## Architecture

### Data Flow

```
FinMind API / TDCC API / voidful
        ↓
finance_tools/domains/       (domain-specific fetchers and tasks)
        ↓
finance_tools/orchestration/ (orchestration, batching)
        ↓
src/data/layer3/company-financials/{code}.json   (one file per company)
        ↓
GitHub Pages (public static JSON API)
```

### finance_tools/ Structure

- **cli.py** — Entry point; dispatches to task modules
- **core/** — Shared abstractions: `api_client.py` (FinMind quota management), `file_manager.py` (atomic JSON writes), `data_processor.py`, `merge_policy.py`, `timezone.py`, `trading_day.py`, `exceptions.py`
  - `merge_policy.py` — **合併規則與保留上限的唯一宣告處**（`merge_by_key`）。11 支程式都會寫
    `company-financials/{code}.json`，規則散在各處時曾長出兩套互斥的月營收政策。
    欄位歸誰管見 stock_map `docs/features/platform/財報檔欄位歸屬.md`。
  - `file_manager.load_financial_data()` 有**三種回傳值**：`{}`＝檔案不存在、`None`＝**檔案壞掉**、
    dict＝正常。看到 `None` 的寫入者一律跳過（不得拿片段覆寫）。壞檔**留在原地**。
  - `financials-update` 抓多長**看「檔裡的歷史夠不夠」，不看檔案在不在／壞不壞**
    （`company_processor.history_is_short`：有營收的季度 < 8 或月營收 < 24 → `FULL_HISTORY_DAYS`）。
    新公司（日更會先建只有市值的空殼）、壞檔、放寬後抓失敗寫出的空殼，全都落在「不夠」，
    沒補齊前每週都會再試。門檻必須高於一年視窗帶回的 5 季／13 個月，否則空殼會被當成夠了。
  - `data_processor.normalize_item_name()` — FinMind 科目名稱的括號有半形／全形兩種寫法且依年度而異，
    比對前一律正規化。漏掉會讓整年的營業現金流對不上（2021 年曾因此缺 823 家）。
- **domains/** — 一個資料領域一個資料夾，內含 `fetcher.py`（抓＋正規化）與 `tasks.py`（CLI 任務）。
  現有：`balance_sheet`、`company_info`、`dividends`、`financials`、`insider`、
  `institutional_investors`、`margin_trading`、`market_sentiment`、`revenue`、
  `securities_lending`、`shareholder`、`valuation`
- **orchestration/** — `company_processor.py`（逐家組裝）、`data_assembler.py`（把各領域的結果併進
  `company-financials/{code}.json`，一個領域一個 `merge_*`）、`fetch_orchestrator.py`（批次協調）、
  `check_quality.py`（資料品質＋下市殘留偵測）、`financials_update.py`、`marketcap_inst_update.py`
- **disposition/** — 處置股預測（獨立於 domains，自成一套流程）
- **us_financials/** — 美股財報，與台股走不同管線。**名單讀 companies-all.json 的美股代號**
  （＝有題材分析的美股），不讀任何手寫清單：2026-09-11 以前讀 `us-tickers.json`（5/27 的 198 檔），
  之後新增分析的 432 家從來沒抓過財報。不在名單上的檔會被清掉（上限 max(20, 2%)）。
  非美元報表存兩份：`native` 原幣永遠不動、美元欄位每次從原幣重算
- **utils/** — `company_list_loader.py` (resolve --code/--topic to list), `rerun_manager.py` (track failures), `quality_report.py`, `finmind.py`（FinMind 共用取數）

### CI/CD Pipeline (GitHub Actions)

Three-stage reusable workflow (`.github/workflows/_reusable-data-job.yml`):
1. **Setup** — Determine batch matrix
2. **Run** — 4 parallel batches, each with a dedicated FinMind API token
3. **Merge** — Combine artifacts, validate JSON, commit, trigger rerun if failures remain

**財報檔的合併**：六個 workflow 都推 `company-financials/`，推送前是 `git pull --rebase -X ours`。
`.gitattributes` 把這些檔導到 `finance_tools/scripts/json_merge_driver.py`（JSON 結構三方合併），
**每個 workflow 還要自己 `git config merge.company-json.driver ...`**（config 不隨 repo 走），
漏了就退回逐行合併：可能拼出壞 JSON，或讓 `-X ours` 把這次寫的資料整段丟掉（2026-09-11 實測重現）。
新增會推財報檔的 workflow 時，`test_json_merge_driver.py` 會擋。

**Rerun mechanism**: Workflows self-chain via `gh workflow run` up to `MAX_RERUN_ROUNDS=4` times with a 65-minute delay (API quota reset window). Permanent failures (where failure count doesn't decrease) are written to `permanent_failures_<type>.txt` and excluded from future retries.

**Schedules**: read `cron/wrangler.toml`, `cron/src/index.ts` and the dispatched workflows. Previous timetable prose here was stale; it is not an operational authority. Deployed Worker versions / enabled schedules require production verification; do not deploy to verify this file.

### API Token Management

Four FinMind API tokens stored in `FINMIND_API_TOKENS` (comma-separated secret). Each CI batch uses a distinct token to maximize throughput. Token index = batch number - 1.

### Web Crawlers (Web_Crawler/)

- **economic_daily_scraper.py** — Selenium-based Economic Daily news scraper
- **mops_scraper.py** — HTTP-based MOPS (公開資訊觀測站) crawler
- **money_udn/init_database.py** — Topic-rotation coordinator for news crawling
- **cloudflare_d1_client.py** — Stores scraped news to Cloudflare D1 (not committed to repo)

News data goes to Cloudflare D1; financial data goes to JSON files committed to the repo.

### Key Data Files

- `src/data/layer3/companies/companies-all.json` — Stock code ↔ company name lookup (synced from `stock_map`)
- `src/data/layer3/company-topics/index.json` — Stock code → topic IDs (synced from `stock_map`)
- `src/data/layer3/company-financials/{code}.json` — Per-company financial data (~2,300+ files)
  - `insiderHoldingsRecent` / `insiderHoldingsHistory` — 內部人持股（董監、經理人、大股東）。
    **明細只留當期、歷史只留兩組合計**，與 `shareholderDataRecent` / `shareholderDataHistory` 同一個拆法：
    一家最多 250 人，每期都留明細會讓檔案幾個月就翻倍。
    對外顯示的質押比用 `boardTotals`（只算董監事本人）而不是 `totals`（全體內部人）——
    市場慣稱的「董監持股質押比」不含經理人與大股東，用錯會跟其他網站對不起來。
- `src/data/layer3/company-financials-archive/{year}/{code}.json.gz` — 日期型歷史（三大法人、融資融券、借券、大戶）
  超過「當年＋前一年」的部分。**是搬不是刪**；按整年切，某一年封存後就不再變動（git 只存一次）。
  gzip 壓縮比約 13x。App 不讀，是保存不是服務。
- `rerun_queue_<type>.txt` — Failed companies pending retry (committed)
- `permanent_failures_<type>.txt` — Companies with unrecoverable failures (committed)

### Config Constants (finance_tools/config.py)

- `MAX_RETRIES=3` — Per-API-call retry limit
- `MAX_RERUN_ROUNDS=4` — Max CI self-chain reruns