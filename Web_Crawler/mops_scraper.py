import pandas as pd
import datetime
import time
import re
import requests
from typing import List, Optional
from cloudflare_d1_client import CloudflareD1Client


def convert_roc_date(date_str: str) -> str:
    """
    將民國日期轉換為西元日期字串 (YYYYMMDD 格式)。

    Args:
        date_str (str): 民國日期字串 (例如: '115/01/29')

    Returns:
        str: 西元日期字串 (YYYYMMDD 格式)
    """
    if not date_str:
        return ''

    match = re.match(r'(\d+)/(\d+)/(\d+)', date_str)
    if match:
        year = int(match.group(1)) + 1911
        month = int(match.group(2))
        day = int(match.group(3))
        return f'{year}{month:02d}{day:02d}'

    return date_str


def scrape_mops_material_info(
    stock_codes: Optional[List[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    keyword: Optional[str] = None
) -> pd.DataFrame:
    """
    從公開資訊觀測站 API 爬取重大訊息資料。

    Args:
        stock_codes (List[str], optional): 股票代碼列表，例如 ['2330', '2317']。
                                           若為 None 則取得所有公司資料。
        start_date (str, optional): 起始日期 (格式: YYYYMMDD)，預設為今日。
        end_date (str, optional): 結束日期 (格式: YYYYMMDD)，預設與 start_date 相同。
        keyword (str, optional): 主旨關鍵字篩選。

    Returns:
        pd.DataFrame: 包含重大訊息的 DataFrame，欄位包括:
            - code: 股票代碼
            - name: 公司名稱
            - pub_date: 發布日期 (YYYYMMDD)
            - pub_time: 發布時間
            - subject: 主旨
            - source: 資料來源

    Examples:
        # 取得今日所有重大訊息
        df = scrape_mops_material_info()

        # 取得特定股票的重大訊息
        df = scrape_mops_material_info(stock_codes=['2330', '2317'])

        # 取得特定日期的重大訊息
        df = scrape_mops_material_info(start_date='20250120')

        # 取得日期區間的重大訊息
        df = scrape_mops_material_info(start_date='20250115', end_date='20250120')

        # 關鍵字篩選
        df = scrape_mops_material_info(keyword='董事')
    """
    # 處理日期參數
    if start_date is None:
        start_date = datetime.datetime.now().strftime('%Y%m%d')
    if end_date is None:
        end_date = start_date

    # 產生日期列表
    start_dt = datetime.datetime.strptime(start_date, '%Y%m%d')
    end_dt = datetime.datetime.strptime(end_date, '%Y%m%d')
    date_list = pd.date_range(start=start_dt, end=end_dt).strftime('%Y%m%d').tolist()

    print(f"開始從公開資訊觀測站爬取重大訊息資料")
    print(f"日期範圍: {start_date} ~ {end_date} ({len(date_list)} 天)")
    if stock_codes:
        print(f"篩選股票: {stock_codes}")
    if keyword:
        print(f"關鍵字篩選: {keyword}")

    column_names = ['code', 'name', 'pub_date', 'pub_time', 'subject', 'source']

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'Referer': 'https://mops.twse.com.tw/mops/',
    }

    api_url = 'https://mops.twse.com.tw/mops/api/t05st02'
    all_data = []

    for idx, target_date in enumerate(date_list):
        # 轉換為民國年月日
        year = str(int(target_date[0:4]) - 1911)
        month = target_date[4:6]
        day = target_date[6:8]

        params = {
            'year': year,
            'month': month,
            'day': day,
            'TYPEK': 'all'
        }

        print(f"[{idx+1}/{len(date_list)}] 正在查詢: {target_date} (民國 {year}/{month}/{day})")

        try:
            response = requests.post(api_url, headers=headers, json=params, timeout=30)
            response.raise_for_status()

            result = response.json()

            if result.get('code') != 200:
                print(f"  {result.get('message', '無資料')}")
                continue

            data = result.get('result', {}).get('data', [])

            if not data:
                print(f"  無資料")
                continue

            for item in data:
                if len(item) >= 5:
                    roc_date = item[0]  # 115/01/28
                    pub_time = item[1]  # 17:30:36
                    code = item[2]      # 5215
                    name = item[3]      # 科嘉-KY
                    subject = item[4]   # 主旨

                    # 股票代碼篩選
                    if stock_codes and code not in stock_codes:
                        continue

                    # 關鍵字篩選
                    if keyword and keyword not in subject:
                        continue

                    # 轉換日期
                    pub_date = convert_roc_date(roc_date)

                    all_data.append({
                        'code': code,
                        'name': name,
                        'pub_date': pub_date,
                        'pub_time': pub_time,
                        'subject': subject.replace('\r\n', ' ').replace('\n', ' '),
                        'source': '公開資訊觀測站'
                    })

            print(f"  取得 {len([d for d in data if not stock_codes or d[2] in stock_codes])} 筆資料")

        except requests.exceptions.RequestException as e:
            print(f"  請求失敗: {e}")
        except Exception as e:
            print(f"  錯誤: {e}")

        # 避免請求過於頻繁
        if idx < len(date_list) - 1:
            time.sleep(0.5)

    if all_data:
        df = pd.DataFrame(all_data)
        df = df.drop_duplicates(subset=['code', 'pub_date', 'pub_time', 'subject'])
        print(f'\n共取得 {len(df)} 筆重大訊息資料')
        
        # Save to Cloudflare D1
        try:
            client = CloudflareD1Client()
            client.init_tables()
            
            print("正在寫入 Cloudflare D1...")
            # Prepare data for insertion
            insert_sql = """
            INSERT OR IGNORE INTO mops_announcements (code, name, pub_date, pub_time, subject, source)
            VALUES (?, ?, ?, ?, ?, ?);
            """
            
            # Convert DataFrame rows to list of param lists
            # Note: ensuring all types are JSON serializable (str mostly)
            params_list = []
            for _, row in df.iterrows():
                params_list.append([
                    str(row['code']),
                    str(row['name']),
                    str(row['pub_date']),
                    str(row['pub_time']),
                    str(row['subject']),
                    str(row['source'])
                ])
            
            client.batch_execute_query(insert_sql, params_list)
            print("成功寫入 Cloudflare D1")
            
        except Exception as e:
            print(f"寫入 Cloudflare D1 時發生錯誤: {e}")
            print("請確認已設定 CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_DATABASE_ID, CLOUDFLARE_API_TOKEN 環境變數")

        return df
    else:
        print('\n無重大訊息資料')
        return pd.DataFrame(columns=column_names)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='公開資訊觀測站重大訊息爬蟲')
    parser.add_argument('-s', '--stock', nargs='+', help='股票代碼 (可多個，空格分隔)')
    parser.add_argument('-d', '--date', help='查詢日期 (格式: YYYYMMDD)')
    parser.add_argument('--start', help='起始日期 (格式: YYYYMMDD)')
    parser.add_argument('--end', help='結束日期 (格式: YYYYMMDD)')
    parser.add_argument('-k', '--keyword', help='主旨關鍵字篩選')
    parser.add_argument('-o', '--output', help='輸出 CSV 檔案路徑')

    args = parser.parse_args()

    # 處理日期參數
    start_date = args.start or args.date
    end_date = args.end or args.date

    # 執行爬蟲
    df = scrape_mops_material_info(
        stock_codes=args.stock,
        start_date=start_date,
        end_date=end_date,
        keyword=args.keyword
    )

    if not df.empty:
        print("\n--- 重大訊息資料 ---")
        pd.set_option('display.max_rows', 50)
        pd.set_option('display.max_columns', None)
        pd.set_option('display.width', None)
        pd.set_option('display.max_colwidth', 60)
        print(df)

        # 輸出 CSV
        if args.output:
            df.to_csv(args.output, index=False, encoding='utf-8-sig')
            print(f"\n已儲存至: {args.output}")
    else:
        print("\n--- 無符合條件的重大訊息 ---")
