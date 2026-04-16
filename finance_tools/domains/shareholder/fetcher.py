from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import pandas as pd
import time
from io import StringIO
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any
import logging

logger = logging.getLogger(__name__)

def fetch_tdcc_shareholding_multi_dates(stock_id: str, max_dates: int = 24, headless: bool = True) -> List[Dict[str, Any]]:
    """
    抓取單隻股票 TDCC 股權分散表（多日期，前 max_dates 期）
    並回傳資料 (List[Dict])
    """
    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")

    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 20)

    all_records = []

    try:
        # 開啟 TDCC 頁面
        driver.get("https://www.tdcc.com.tw/portal/zh/smWeb/qryStock")
        wait.until(EC.presence_of_element_located((By.NAME, "scaDate")))

        # 取得所有可選日期
        date_select = Select(driver.find_element(By.NAME, "scaDate"))
        total_dates = len(date_select.options)
        # Skip "請選擇" option
        start_option_idx = 1 if date_select.options[0].text.strip() == "請選擇" else 0
        dates_to_fetch = min(max_dates, total_dates - start_option_idx)

        for idx in range(dates_to_fetch):
            # 每次重新抓取下拉選單，避免 stale element
            date_select = Select(driver.find_element(By.NAME, "scaDate"))
            option = date_select.options[idx + start_option_idx] 
            data_date = option.text.strip()
            logger.debug(f"[TDCC] {stock_id} | 抓取第 {idx + 1} 期：{data_date}")

            # 選日期
            date_select.select_by_visible_text(data_date)

            # 輸入股票代號
            stock_input = driver.find_element(By.ID, "StockNo")
            stock_input.clear()
            stock_input.send_keys(stock_id)

            # 查詢
            driver.find_element(By.XPATH, "//input[@value='查詢']").click()

            # 等待股權分散表出現
            wait.until(
                EC.text_to_be_present_in_element((By.TAG_NAME, "body"), "1-999")
            )
            time.sleep(1)

            # 解析 table
            tables = pd.read_html(StringIO(driver.page_source))
            target_df = None
            for df in tables:
                if not df.empty and df.shape[1] == 5 and df.iloc[:, 1].astype(str).str.contains("1-999").any():
                    target_df = df
                    break
            if target_df is None:
                logger.warning(f"[WARN] {stock_id} | {data_date} 查詢失敗，略過")
                continue

            # 欄位標準化
            target_df = target_df.rename(
                columns={
                    "持股/單位數分級": "holding_range",
                    "人數": "holder_count",
                    "股數/單位數": "shares",
                    "占集保庫存數比例 (%)": "ratio_pct",
                }
            )

            # 清理型別
            for col in ["holder_count", "shares", "ratio_pct"]:
                target_df[col] = pd.to_numeric(target_df[col], errors="coerce")

            # 移除非分析列 (保留合計，移除差異)
            target_df = target_df[
                ~target_df["holding_range"].str.contains("差異", na=False)
            ].copy()
            # 將 '合　計' 標準化為 '合計'
            target_df["holding_range"] = target_df["holding_range"].str.replace("合　計", "合計", regex=False)

            # 加上日期與股票代號
            target_df["data_date"] = data_date
            target_df["stock_id"] = stock_id

            all_records.extend(target_df.to_dict(orient="records"))

        if not all_records:
            raise RuntimeError(f"{stock_id} 未抓取到任何 TDCC 股權分散資料")

        unique_dates = {r["data_date"] for r in all_records if "data_date" in r}
        logger.info(f"[TDCC] {stock_id} OK — {len(unique_dates)} 期")
        return all_records

    finally:
        driver.quit()


def fetch_multiple_stocks(stock_ids: List[str], max_dates: int = 10, headless: bool = True) -> Dict[str, List[Dict[str, Any]]]:
    """
    平行抓取多隻股票
    """
    results = {}

    with ThreadPoolExecutor(max_workers=min(2, len(stock_ids))) as executor:
        future_to_stock = {
            executor.submit(fetch_tdcc_shareholding_multi_dates, stock_id, max_dates, headless): stock_id
            for stock_id in stock_ids
        }
        for future in as_completed(future_to_stock):
            stock_id = future_to_stock[future]
            try:
                tdcc_data = future.result()
                results[stock_id] = tdcc_data
            except Exception as e:
                logger.error(f"[ERROR] {stock_id} 抓取失敗：{e}")

    return results
