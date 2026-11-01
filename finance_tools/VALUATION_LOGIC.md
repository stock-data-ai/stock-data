# 📈 台灣股市：市值與估值更新邏輯 (Valuation & Market Cap)

本文件說明個股「市值」與「估值指標（本益比、股價淨值比、殖利率）」如何寫進
`src/data/layer3/company-financials/{code}.json` 的 `latest` 區塊。
**以程式為準**；本檔於 2026-09-11 依現行程式改寫（舊版描述的是已退役的 yfinance 逐股抓取）。

---

## 1. 流程

```
uv run finance_tools/cli.py update-marketcap-inst        ← daily-update workflow 呼叫
  → finance_tools/orchestration/marketcap_inst_update.py  ← 一次抓全市場（估值＋三大法人）
      → finance_tools/domains/valuation/twse_valuation_fetcher.py  (TWSEValuationFetcher.fetch_all)
  → finance_tools/orchestration/company_processor.py      (CompanyProcessor.process_daily_only，逐家)
      → _build_valuation：收盤價 × 發行股數 = 市值
      → finance_tools/orchestration/data_assembler.py      (DataAssembler.merge_valuation)
```

## 2. 資料來源（`twse_valuation_fetcher.py`）

| 板別 | 來源 | 內容 |
|---|---|---|
| 上市 | FinMind `TaiwanStockPER`＋`TaiwanStockPrice` | 本益比／股價淨值比／殖利率、收盤價 |
| 上櫃 | 櫃買 OpenAPI `tpex_mainboard_quotes`＋`tpex_mainboard_peratio_analysis`（開放資料） | 同上；覆蓋 FinMind 回來的上櫃代號 |

- 不再抓 `www.twse.com.tw` 的 BWIBBU_d／MI_INDEX（2026-08-26 證交所來函後移除，理由寫在該檔 `_fetch_listed` docstring）。
- 市值＝收盤價 × `companies-all.json` 的 `gov.capital.issuedCommonShares`；沒有股數就不寫市值。

## 3. 欄位對應

| 抓到的值 | `_build_valuation` 產出 | `merge_valuation` 寫入 `latest` |
|---|---|---|
| 收盤價 × 發行股數 | `marketCap` | `marketCap` |
| 本益比 | `trailingPE` | `pe` |
| 股價淨值比 | `priceToBook` | `pb` |
| 殖利率（來源是百分比，如 1.84） | `dividendYield`（除以 100 → 0.0184） | `dividendYield`（乘回 100，存百分比） |

值為 `None` 的欄位不覆寫；有寫入就更新檔案的 `lastUpdated`。

## 4. 邊界

- 找不到該公司的當日資料＝今日無資料，不算失敗、不進 rerun queue。
- 財報檔讀不回來（壞檔）時日更跳過，等 `financials-update` 用完整視窗重建（見 repo 根目錄 `CLAUDE.md`）。
- 同日已更新過會跳過，`--force` 才重跑。

## 5. 排程與指令

排程看 `cron/wrangler.toml` 與 `cron/src/index.ts`（dispatch `daily-update.yml`），不在本檔重寫時刻。

```bash
uv run finance_tools/cli.py update-marketcap-inst --code 2330   # 會寫正式 JSON，不是驗證
```
