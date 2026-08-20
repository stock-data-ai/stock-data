import pandas as pd
import datetime
import sys
import time
import re
import requests
from typing import List, Optional, Dict, Any
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


def fetch_mops_detail(
    enter_date: str,
    serial_number: int,
    company_id: str,
    market_kind: str,
    headers: dict,
) -> Dict[str, Any]:
    """
    呼叫 MOPS detail API 取得單筆公告的完整內容。
    回傳 {'content': str, 'speaker': str, 'event_date': str}，失敗時回傳空值。
    """
    try:
        params = {
            'enterDate': enter_date,
            'serialNumber': serial_number,
            'companyId': company_id,
            'marketKind': market_kind,
        }
        r = requests.post(
            'https://mops.twse.com.tw/mops/api/t05st02_detail',
            headers=headers,
            json=params,
            timeout=30,
        )
        r.raise_for_status()
        result = r.json()
        if result.get('code') != 200:
            return {}
        data_rows = result.get('result', {}).get('data', [])
        # Find the row matching serial_number (data[i][0] == serial_number)
        for row in data_rows:
            if len(row) >= 10 and int(row[0]) == int(serial_number):
                return {
                    'content': str(row[9]).replace('\r\n', '\n').strip() if row[9] else None,
                    'speaker': str(row[3]).strip() if row[3] else None,
                    'event_date': convert_roc_date(str(row[8])) if row[8] else None,
                }
        # Fallback: return first row if serial match fails
        if data_rows and len(data_rows[0]) >= 10:
            row = data_rows[0]
            return {
                'content': str(row[9]).replace('\r\n', '\n').strip() if row[9] else None,
                'speaker': str(row[3]).strip() if row[3] else None,
                'event_date': convert_roc_date(str(row[8])) if row[8] else None,
            }
    except Exception as e:
        print(f"  detail API 失敗 ({company_id} serial={serial_number}): {e}")
    return {}


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

                    # 解析 detail 參數（item[5] 含 enterDate/serialNumber/marketKind）
                    meta = item[5] if len(item) > 5 else {}
                    detail_params = meta.get('parameters', {}) if isinstance(meta, dict) else {}
                    enter_date = detail_params.get('enterDate')
                    serial_number = detail_params.get('serialNumber')
                    market_kind = detail_params.get('marketKind')

                    # 呼叫 detail API 取得內文
                    detail = {}
                    if enter_date and serial_number and market_kind:
                        detail = fetch_mops_detail(enter_date, serial_number, code, market_kind, headers)
                        time.sleep(0.2)  # 避免打爆 MOPS

                    all_data.append({
                        'code': code,
                        'name': name,
                        'pub_date': pub_date,
                        'pub_time': pub_time,
                        'subject': subject.replace('\r\n', ' ').replace('\n', ' '),
                        'source': '公開資訊觀測站',
                        'content': detail.get('content'),
                        'speaker': detail.get('speaker'),
                        'event_date': detail.get('event_date'),
                        'enter_date_roc': enter_date,
                        'serial_number': serial_number,
                        'market_kind': market_kind,
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
            INSERT INTO mops_announcements (code, name, pub_date, pub_time, subject, source, content, speaker, event_date, enter_date_roc, serial_number, market_kind)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code, pub_date, pub_time, subject) DO UPDATE SET
                content = COALESCE(excluded.content, mops_announcements.content),
                speaker = COALESCE(excluded.speaker, mops_announcements.speaker),
                event_date = COALESCE(excluded.event_date, mops_announcements.event_date),
                enter_date_roc = COALESCE(excluded.enter_date_roc, mops_announcements.enter_date_roc),
                serial_number = COALESCE(excluded.serial_number, mops_announcements.serial_number),
                market_kind = COALESCE(excluded.market_kind, mops_announcements.market_kind);
            """

            params_list = []
            for _, row in df.iterrows():
                params_list.append([
                    str(row['code']),
                    str(row['name']),
                    str(row['pub_date']),
                    str(row['pub_time']),
                    str(row['subject']),
                    str(row['source']),
                    row.get('content'),
                    row.get('speaker'),
                    row.get('event_date'),
                    row.get('enter_date_roc'),
                    row.get('serial_number'),
                    row.get('market_kind'),
                ])
            
            client.batch_execute_query(insert_sql, params_list)
            print("成功寫入 Cloudflare D1")

            # 清除 90 天前的舊資料
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=90)).strftime('%Y%m%d')
            deleted = client.execute_query(
                f"DELETE FROM mops_announcements WHERE pub_date < '{cutoff}';"
            )
            print(f"已清除 {cutoff} 以前的舊資料")

        except Exception as e:
            print(f"寫入 Cloudflare D1 時發生錯誤: {e}")
            print("請確認已設定 CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_DATABASE_ID, CLOUDFLARE_API_TOKEN 環境變數")
            # 不可靜默放行：抓到的公告一筆都沒進 D1，排程若還綠燈就沒人會發現公告斷更。
            raise

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

    # 交易日卻一筆都沒有 = MOPS 被擋或改版，不是「今天沒公告」。
    # 只在「無條件全市場撈當日」時判定，指定股票／關鍵字／歷史日期查無資料屬正常。
    if df.empty and not (args.stock or args.keyword or start_date or end_date):
        from finance_tools.core.trading_day import is_tw_trading_day

        if is_tw_trading_day(datetime.date.today()):
            print("\nSTALE: 今日為交易日，但重大訊息 0 筆，判定為撈取失敗。")
            sys.exit(1)
