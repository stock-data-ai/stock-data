# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Taiwan stock financial data pipeline that fetches data from FinMind API, TDCC, and other sources, processes it, and outputs JSON files to GitHub Pages as a static API. Paired with a private `stock_map` repo that handles analysis and UI.

## Package Manager

This project uses `uv` (not pip). Always use `uv run` to execute Python scripts.

```bash
uv sync --all-extras --dev   # Install dependencies
uv run finance_tools/cli.py [COMMAND] [OPTIONS]
```

## Common Commands

```bash
# Single company update
uv run finance_tools/cli.py full-update --code 2330

# Topic-based update
uv run finance_tools/cli.py update-marketcap --topic bbu

# Batch processing (used in CI — batch N of 4)
uv run finance_tools/cli.py full-update --batch 2/4

# Rerun companies that failed in previous run
uv run finance_tools/cli.py full-update --rerun --batch 1/4

# Force update (bypass "already updated today" guard)
uv run finance_tools/cli.py update-institutional-investors --force

# Limit number of companies (for quick testing)
uv run finance_tools/cli.py update-revenue --limit 10

# Run tests
uv run pytest finance_tools/tests/
```

## Architecture

### Data Flow

```
FinMind API / TDCC / voidful
        ↓
finance_tools/fetchers/      (one fetcher per data type)
        ↓
finance_tools/processing/    (orchestration, batching)
        ↓
src/data/layer3/company-financials/{code}.json   (one file per company)
        ↓
GitHub Pages (public static JSON API)
```

### finance_tools/ Structure

- **cli.py** — Entry point; dispatches to task modules
- **core/** — Shared abstractions: `api_client.py` (FinMind quota management), `file_manager.py` (atomic JSON writes), `data_processor.py`, `timezone.py`, `exceptions.py`
- **fetchers/** — One module per data type (financials, revenue, market cap, institutional investors, TDCC, dividends, P/E/P/B)
- **processing/** — `company_processor.py` (combines fetchers per company), `fetch_orchestrator.py` (coordinates batch runs)
- **tasks/** — CLI task implementations invoked by cli.py, organized by frequency/category:
  - **daily/**: Market cap, institutional investors
  - **periodic/**: Monthly revenue, weekly shareholder data, valuation
  - **fundamentals/**: Basic company info, manual dividend imports
  - **orchestrators/**: Full update coordination
  - **maintenance/**: Data quality checks
- **utils/** — `company_list_loader.py` (resolve --code/--topic to list), `rerun_manager.py` (track failures), `quality_report.py`

### CI/CD Pipeline (GitHub Actions)

Three-stage reusable workflow (`.github/workflows/_reusable-data-job.yml`):
1. **Setup** — Determine batch matrix
2. **Run** — 4 parallel batches, each with a dedicated FinMind API token
3. **Merge** — Combine artifacts, validate JSON, commit, trigger rerun if failures remain

**Rerun mechanism**: Workflows self-chain via `gh workflow run` up to `MAX_RERUN_ROUNDS=4` times with a 65-minute delay (API quota reset window). Permanent failures (where failure count doesn't decrease) are written to `permanent_failures_<type>.txt` and excluded from future retries.

**Schedules** (Taiwan time):
- Mon–Fri 09:00 & 17:00: Market cap + institutional investors
- Sat 09:00: TDCC shareholder data (API-based)
- Sun 09:00: Full update (financials, revenue, dividends)
- Mon–Fri 07:00 & 23:00: Economic Daily news scraper

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
- `rerun_queue_<type>.txt` — Failed companies pending retry (committed)
- `permanent_failures_<type>.txt` — Companies with unrecoverable failures (committed)

### Config Constants (finance_tools/config.py)

- `MAX_RETRIES=3` — Per-API-call retry limit
- `MAX_RERUN_ROUNDS=4` — Max CI self-chain reruns