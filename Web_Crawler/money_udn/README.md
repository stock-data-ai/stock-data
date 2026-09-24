# Money UDN 經濟日報爬蟲

以主題為單位，批次爬取經濟日報新聞。

---

## 運作方式

**主題輪替**：每次跑一個主題，自動輪替所有主題，循環不斷。

```
16 個主題（IC 設計、晶圓代工、矽光子...）
    ↓ D1 記錄上次跑到第幾個
本次跑第 N 個主題
    ↓ 爬取該主題下所有公司的經濟日報新聞
    ↓ 結果寫入 D1
更新 N+1，下次自動跑下一個
```

**GitHub Actions**：每日兩次（台灣 09:00 / 17:00），每次跑一個主題。
16 個主題 ÷ 每日 2 次 = 約 8 天完整輪替一輪。

---

## 使用方式

### 自動輪替（GitHub Actions 用）

```bash
# 自動跑下一個主題（讀 D1 狀態 → 執行 → 更新狀態）
uv run python -m Web_Crawler.money_udn.init_database --rotate
```

### 手動指定主題

```bash
# 執行單一主題
uv run python -m Web_Crawler.money_udn.init_database --topic silicon-photonics

# 執行指定批次（每批 2 主題）
uv run python -m Web_Crawler.money_udn.init_database --batch 1
```

### 查看進度

```bash
# 查看初始化狀態與建議
uv run python -m Web_Crawler.money_udn.init_database --status
```

### 其他

```bash
# 手動爬取特定公司
uv run python Web_Crawler/economic_daily_scraper.py "台積電"

# 清除本地進度記錄
uv run python -m Web_Crawler.money_udn.init_database --reset
```

---

## 批次內容

| 批次 | 主題 |
|------|------|
| 1 | IC 設計 / 晶圓代工 |
| 2 | 晶圓製造供應鏈 / 封裝與測試 |
| 3 | 系統與終端應用 / BBU 電池備援 |
| 4 | 被動元件 / 連接器 |
| 5 | 設備與廠務工程 / CoWoS 先進封裝 |
| 6 | 矽光子 / 低軌衛星通訊 |
| 7 | 軍工產業 / 玻纖布 |
| 8 | 電器電纜 / 人形機器人 |

---

## 檔案結構

```
Web_Crawler/money_udn/
├── init_database.py     # 主程式 (主題批次 + 輪替模式)
├── smart_scheduler.py   # [保留] 智慧排程器 (目前未在 CI 使用)
├── priority_config.py   # [保留] 優先權設定
├── pre_filter.py        # [保留] Google News RSS 預篩
├── .init_progress.json  # 本地進度記錄 (自動產生)
└── README.md
```

---

## D1 資料表

| 表格 | 用途 |
|------|------|
| `topic_rotation` | 主題輪替狀態（current_index, last_topic_id, last_run_at） |
| `economic_daily_news` | 經濟日報新聞資料 |
| `crawl_schedule` | 公司爬取狀態 |
| `crawl_logs` | 爬取歷史紀錄 |
