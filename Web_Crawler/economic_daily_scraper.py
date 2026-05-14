import pandas as pd
import datetime
import time
import json
import html
import random
import subprocess
from urllib.parse import quote
from bs4 import BeautifulSoup
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from cloudflare_d1_client import CloudflareD1Client


def _get_crawler_client() -> CloudflareD1Client:
    """取得爬蟲管理 DB client（stock-map-crawler）"""
    crawler_db_id = os.environ.get('CLOUDFLARE_CRAWLER_DB_ID')
    if not crawler_db_id:
        raise ValueError("CLOUDFLARE_CRAWLER_DB_ID 未設定")
    return CloudflareD1Client(database_id=crawler_db_id)


def update_crawl_status(company_code: str, company_name: str,
                        status: str, news_count: int = 0):
    """更新爬取狀態到 crawl_schedule（crawler DB）"""
    now = datetime.datetime.now().isoformat()
    try:
        crawler = _get_crawler_client()
        crawler.execute_query("""
            INSERT INTO crawl_schedule (company_code, company_name, last_crawled, last_crawl_status, total_crawls, total_hits)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(company_code) DO UPDATE SET
                last_crawled = excluded.last_crawled,
                last_crawl_status = excluded.last_crawl_status,
                total_crawls = total_crawls + 1,
                total_hits = CASE WHEN ? > 0 THEN total_hits + 1 ELSE total_hits END,
                updated_at = CURRENT_TIMESTAMP
        """, [
            company_code,
            company_name,
            now,
            status,
            1 if news_count > 0 else 0,
            news_count
        ])
    except Exception as e:
        print(f"更新爬取狀態失敗: {e}")


def is_relevant_to_company(title: str, company_name: str, company_code: str,
                            all_companies: dict = None) -> bool:
    """
    判斷文章標題是否真正與該公司相關，過濾掉誤判。

    策略：
    1. 股票代碼出現在標題中 → 最可靠，直接通過
    2. 公司名稱出現在另一間公司名稱內（如「南亞」出現在「南亞科」）→ 排除
    3. 公司名稱左邊緊接著中文字（如「東南亞」的「東」）→ 排除
    """
    # 代碼比對最可靠
    if company_code and company_code in title:
        return True

    if company_name not in title:
        return False

    # 檢查是否為其他公司名稱的子字串（如「南亞科」包含「南亞」）
    if all_companies:
        for other_code, other_company in all_companies.items():
            if other_code == company_code:
                continue
            other_name = other_company.get('shortName') or other_company.get('name', '')
            if (other_name and company_name in other_name and
                    len(other_name) > len(company_name) and other_name in title):
                return False

    # 檢查每個出現位置：左邊緊接中文字視為誤判（如「東」南亞）
    def is_cjk(ch: str) -> bool:
        return '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf'

    idx = title.find(company_name)
    while idx != -1:
        char_before = title[idx - 1] if idx > 0 else ''
        if not is_cjk(char_before):
            return True  # 左邊清晰，這是一個獨立提及
        idx = title.find(company_name, idx + 1)

    return False


def scrape_economic_daily_news(search_key: str, company_code: str = None,
                                all_companies: dict = None) -> pd.DataFrame:
    """
    從經濟日報網站爬取指定關鍵字的新聞列表 (使用 undetected-chromedriver 處理動態載入內容)。

    Args:
        search_key (str): 欲搜尋的股票關鍵字 (例如: '2330' 或 '台積電').

    Returns:
        pd.DataFrame: 包含新聞資料的 DataFrame，欄位包括 'pub_date', 'title', 'link', 'source'。
    """
    print(f"開始從經濟日報搜尋關鍵字: {search_key} (使用 undetected-chromedriver)")
    
    encoded_key = quote(search_key)
    url = f"https://money.udn.com/search/result/1001/{encoded_key}?search_type=title"
    
    # 隨機 viewport 大小（模擬不同裝置）
    viewports = [
        (1920, 1080), (1366, 768), (1440, 900),
        (1536, 864), (1280, 720), (1600, 900),
    ]
    vw, vh = random.choice(viewports)

    options = uc.ChromeOptions()
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument(f'--window-size={vw},{vh}')

    # Detect Chrome major version so uc downloads the matching ChromeDriver,
    # preventing version mismatch errors (e.g. ChromeDriver 147 vs Chrome 146).
    chrome_major = None
    for chrome_bin in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
        try:
            out = subprocess.check_output([chrome_bin, "--version"], stderr=subprocess.DEVNULL, text=True)
            chrome_major = int(out.strip().split()[2].split(".")[0])
            break
        except Exception:
            continue

    driver = None
    page_source = None
    try:
        driver = uc.Chrome(options=options, use_subprocess=True, version_main=chrome_major)

        # 載入前隨機等待 1~4 秒
        time.sleep(random.uniform(1, 4))

        print(f"正在載入頁面: {url}")
        driver.get(url)

        wait = WebDriverWait(driver, 20)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/money/story/']")))

        print("頁面似乎已載入新聞內容，取得動態 HTML...")

        # 模擬人類瀏覽：隨機滾動行為
        scroll_pause = random.uniform(0.5, 1.5)
        driver.execute_script(f"window.scrollTo(0, {random.randint(300, 600)});")
        time.sleep(scroll_pause)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(random.uniform(2, 5))

        page_source = driver.page_source

    except TimeoutException:
        print("等待新聞連結時發生超時。")
        if driver:
            driver.save_screenshot('debug_screenshot_timeout.png')
            print("已儲存截圖: debug_screenshot_timeout.png")
        return pd.DataFrame()
    except Exception as e:
        print(f"使用 undetected-chromedriver 抓取經濟日報網站失敗: {e}")
        if driver:
            driver.save_screenshot('debug_screenshot_error.png')
            print("已儲存截圖: debug_screenshot_error.png")
        return pd.DataFrame()
    finally:
        if driver:
            driver.quit()

    if not page_source:
        print("無法獲取頁面源碼。")
        return pd.DataFrame()

    soup = BeautifulSoup(page_source, 'html.parser')
    news_list = []

    # 從 JSON-LD 結構化資料解析
    script_tags = soup.find_all('script', type='application/ld+json')
    for script in script_tags:
        try:
            data = json.loads(script.string)
            # 處理 @graph 結構
            graph_items = data.get('@graph', [data])
            for graph_item in graph_items:
                if graph_item.get('@type') == 'ItemList':
                    for item in graph_item.get('itemListElement', []):
                        news_item = item.get('item', {})
                        if news_item.get('@type') == 'NewsArticle':
                            # 解碼 HTML entities 並移除 <u> 標籤
                            raw_title = news_item.get('headline', '')
                            title = html.unescape(raw_title).replace('<u>', '').replace('</u>', '')
                            link = news_item.get('url', '')
                            date_str = news_item.get('datePublished', '')

                            if title and link and date_str:
                                try:
                                    pub_date = datetime.datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                                    pub_date = pub_date.replace(tzinfo=None)
                                except ValueError:
                                    try:
                                        pub_date = datetime.datetime.strptime(date_str[:16], '%Y-%m-%dT%H:%M')
                                    except ValueError:
                                        continue

                                news_list.append({
                                    'pub_date': pub_date,
                                    'title': title,
                                    'link': link,
                                    'source': '經濟日報'
                                })
        except (json.JSONDecodeError, TypeError):
            continue

    # Fallback: Parse from HTML structure if JSON-LD fails
    if not news_list:
        print("JSON-LD 解析失敗，嘗試從 HTML 結構解析...")

        # Find all article containers
        articles = soup.find_all('li', class_='story-headline-wrapper')

        for article in articles:
            try:
                # Find the link with story content
                content_div = article.find('div', class_='story__content')
                if not content_div:
                    continue

                link_elem = content_div.find('a', href=lambda x: x and '/money/story/' in x)
                if not link_elem:
                    continue

                # Get link URL (remove tracking params)
                link = link_elem.get('href', '')
                if '?' in link:
                    link = link.split('?')[0]

                # Get time element
                time_elem = link_elem.find('time')
                if not time_elem:
                    continue

                date_text = time_elem.get_text(strip=True)

                # Get title (text after time element, excluding time text)
                full_text = link_elem.get_text(strip=True)
                title = full_text.replace(date_text, '').strip()

                # Parse date (format: "2026-01-31 21:25")
                if title and link and date_text:
                    try:
                        pub_date = datetime.datetime.strptime(date_text, '%Y-%m-%d %H:%M')
                    except ValueError:
                        try:
                            pub_date = datetime.datetime.strptime(date_text[:10], '%Y-%m-%d')
                        except ValueError:
                            continue

                    news_list.append({
                        'pub_date': pub_date,
                        'title': title,
                        'link': link,
                        'source': '經濟日報'
                    })
            except Exception:
                continue

        if news_list:
            print(f"從 HTML 結構成功解析到 {len(news_list)} 則新聞。")

    if not news_list:
        print("無法從 JSON-LD 或 HTML 結構解析新聞資料。")
        return pd.DataFrame()

    df = pd.DataFrame(news_list)
    df = df.sort_values(by='pub_date', ascending=False).reset_index(drop=True)

    # 過濾與公司無關的文章（如「東南亞」、「南亞科」誤判）
    if company_code or search_key:
        before_count = len(df)
        df = df[df['title'].apply(
            lambda t: is_relevant_to_company(t, search_key, company_code or '', all_companies)
        )].reset_index(drop=True)
        filtered_count = before_count - len(df)
        if filtered_count > 0:
            print(f"過濾掉 {filtered_count} 則不相關新聞（誤判）")

    print(f"成功從經濟日報抓取到 {len(df)} 則新聞。")

    # Save to Cloudflare D1
    try:
        client = CloudflareD1Client()
        client.migrate_add_company_code()  # ensure column exists
        client.init_tables()

        print("正在寫入 Cloudflare D1...")
        insert_sql = """
        INSERT OR IGNORE INTO economic_daily_news (company_code, pub_date, title, link, source)
        VALUES (?, ?, ?, ?, ?);
        """

        params_list = []
        for _, row in df.iterrows():
            params_list.append([
                company_code or None,
                str(row['pub_date']),
                str(row['title']),
                str(row['link']),
                str(row['source'])
            ])
        
        client.batch_execute_query(insert_sql, params_list)
        print(f"成功寫入 Cloudflare D1 ({len(params_list)} 則)")

        # 更新爬取狀態 (如果有提供公司代碼)
        if company_code:
            update_crawl_status(company_code, search_key, 'success', len(df))

    except Exception as e:
        print(f"寫入 Cloudflare D1 時發生錯誤: {e}")
        print("請確認已設定 CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_DATABASE_ID, CLOUDFLARE_API_TOKEN 環境變數")

    return df


if __name__ == '__main__':
    import sys
    import argparse

    parser = argparse.ArgumentParser(description='經濟日報新聞爬蟲')
    parser.add_argument('search_term', nargs='?', default='台積電',
                        help='搜尋關鍵字 (公司名稱)')
    parser.add_argument('--code', '-c', type=str, default=None,
                        help='股票代碼 (用於更新爬取狀態)')

    args = parser.parse_args()

    news_df = scrape_economic_daily_news(args.search_term, args.code)

    if not news_df.empty:
        print(f"\n--- 成功取得 {args.search_term} 新聞資料 ({len(news_df)} 則) ---")
    else:
        print(f"\n--- 未找到 {args.search_term} 相關新聞 ---")