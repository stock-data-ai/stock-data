"""
Google新聞即時爬蟲 (簡化版 - 模組化)
程式碼撰寫: 蘇彥庭
日期: 20210111
2026/01/25 由 Gemini 修改:
- 移除內文爬取功能，只專注於從 Google News RSS Feed 獲取新聞列表。
- 透過 requests 的自動重新導向功能來取得最終的真實新聞網址。
- 將核心邏輯封裝在函式中，使其可以被其他模組調用。
- 移除直接輸出到 CSV 檔案的功能，改為回傳 Pandas DataFrame。
"""

# 載入套件
import requests
import pandas as pd
import time
import re
from bs4 import BeautifulSoup
import datetime

def arrangeGoogleNews(elem):
    """從 RSS item 中解析出基本新聞資訊"""
    return ([elem.find('title').getText(),
             elem.find('link').getText(),
             elem.find('pubDate').getText(),
             elem.find('source').getText()])

def get_final_url(url: str) -> str:
    """使用 requests 跟隨 Google 的重新導向，取得最終的URL"""
    try:
        headers = {'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4280.141 Safari/537.36'}
        # 使用 head 請求以減少流量，並跟隨重新導向
        response = requests.head(url, headers=headers, allow_redirects=True, timeout=10)
        response.raise_for_status() # 檢查請求是否成功
        return response.url
    except requests.exceptions.RequestException as e:
        # print(f"Could not resolve URL {url}: {e}") # 僅在調試時打印
        return url # 如果解析失敗，返回原始 Google 連結

def get_google_stock_news(search_list: list, days_back_for_news: int = 10) -> pd.DataFrame:
    """
    從 Google News RSS Feed 獲取指定股票的最新新聞列表。

    Args:
        search_list (list): 欲下載新聞的股票關鍵字清單 (例如: ['2330台積電', '2317鴻海'])。
        days_back_for_news (int): 欲篩選的新聞回溯天數。

    Returns:
        pd.DataFrame: 包含新聞資料的 DataFrame，欄位包括 'search_time', 'search_key',
                      'pub_date', 'source', 'title', 'link'。
    """
    stockNews = pd.DataFrame()
    nearStartDate = (datetime.date.today() + datetime.timedelta(days=-days_back_for_news)).strftime('%Y-%m-%d')
    
    for iSearch, search_key in enumerate(search_list):
        print(f'目前正在搜尋股票: {search_key} 在Google的新聞清單  進度: {iSearch + 1} / {len(search_list)}')

        # 建立搜尋網址
        url = f'https://news.google.com/news/rss/search/section/q/{search_key}/?hl=zh-tw&gl=TW&ned=zh-tw_tw'
        
        try:
            response = requests.get(url)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"Failed to fetch RSS feed for {search_key}: {e}")
            continue

        soup = BeautifulSoup(response.text, 'xml')
        items = soup.find_all('item')
        rows = [arrangeGoogleNews(elem) for elem in items]

        if not rows:
            print(f"No news items found in RSS feed for {search_key}.")
            continue

        # 組成pandas
        df = pd.DataFrame(data=rows, columns=['title', 'google_link', 'pub_date', 'source'])
        
        # 新增時間戳記欄位
        df.insert(0, 'search_time', time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), True)
        # 新增搜尋字串
        df.insert(1, 'search_key', search_key, True)
        
        # 篩選最近的新聞
        df['pub_date'] = pd.to_datetime(df['pub_date'], utc=True)
        df = df[df['pub_date'] >= pd.to_datetime(nearStartDate, utc=True)]
        
        if df.empty:
            print(f"No recent news found for {search_key}.")
            continue

        # 按發布時間排序
        df = df.sort_values(['pub_date']).reset_index(drop=True)

        # 迴圈解析最終新聞連結
        final_urls = []
        print(f"正在解析 {len(df)} 則新聞的最終網址...")
        for i, link in enumerate(df['google_link']):
            print(f"  進度: {i+1}/{len(df)}")
            final_urls.append(get_final_url(link))
            time.sleep(0.5) # 稍微延遲以避免請求過於頻繁

        # 新增最終新聞連結欄位
        df['link'] = final_urls # 直接存到 'link' 欄位
        df.drop(columns=['google_link'], inplace=True) # 移除原始 Google 連結

        # 儲存資料
        stockNews = pd.concat([stockNews, df], ignore_index=True)

    # 重新命名欄位以符合一致性
    output_columns = ['search_time', 'search_key', 'pub_date', 'source', 'title', 'link']
    if not stockNews.empty:
        stockNews = stockNews[output_columns]

    return stockNews

if __name__ == "__main__":
    # 範例使用方式
    default_search_list = ['2330台積電']
    default_days_back = 5 # 測試最近5天的新聞

    print("--- 正在執行 Google Stock News Scraper (模組化版本) ---")
    scraped_news_df = get_google_stock_news(default_search_list, default_days_back)

    if not scraped_news_df.empty:
        print("\n--- 成功取得新聞資料 (前 5 筆) ---")
        print(scraped_news_df.head())
        # 如果需要，也可以在此處將 DataFrame 輸出到 CSV
        # scraped_news_df.to_csv('checkData_modular.csv', index=False, encoding='utf-8-sig')
    else:
        print("\n--- 未找到符合條件的新聞 ---")
