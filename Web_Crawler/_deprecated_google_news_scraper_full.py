"# 載入套件
import requests
import pandas as pd
import time
import re
from bs4 import BeautifulSoup
import datetime
import base64

# 參數設定
# 欲下載新聞的股票關鍵字清單
searchList = ['2330台積電', '2317鴻海', '2412中華電']
# 新聞下載起始日
nearStartDate = (datetime.date.today() + datetime.timedelta(days=-10)).strftime('%Y-%m-%d')


# 整理Google新聞資料用
def arrangeGoogleNews(elem):
    return ([elem.find('title').getText(),
             elem.find('link').getText(),
             elem.find('pubDate').getText(),
             BeautifulSoup(elem.find('description').getText(), 'html.parser').find('a').getText(),
             elem.find('source').getText()])


# 擷取各家新聞網站新聞函數
def beautifulSoupNews(url):

    # 設定hearers
    headers = {'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                             'Chrome/87.0.4280.141 Safari/537.36'}

    # 取得該篇新聞連結內容，requests會自動處理重新導向
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        # 取得重新導向後的最終網址
        newsUrl = response.url
    except requests.exceptions.RequestException as e:
        print(f"Could not fetch {url}: {e}")
        return url, "Fetch Error"
        
    soup = BeautifulSoup(response.text, 'html.parser') 

    # 判斷url網域做對應文章擷取
    try:
        domain = re.findall('https://[^/]*', newsUrl)[0].replace('https://', '')
    except:
        print(f'網址解析錯誤: {newsUrl}')
        content = 'unknow domain'
        return newsUrl, content

    content_parts = []
    try:
        if domain == 'udn.com':
            # 聯合新聞網
            item = soup.find('section', class_='article-content__editor')
            if item:
                content_parts = [elem.getText() for elem in item.find_all('p')]
            
        elif domain == 'ec.ltn.com.tw':
            # 自由財經
            item = soup.find('div', class_='text')
            if item:
                # Exclude the script and the 'p' with the youtube subscription
                for p in item.find_all('p', class_=''):
                    if '點我訂閱' not in p.getText() and '一手掌握' not in p.getText():
                        content_parts.append(p.getText())

        elif domain in ['tw.stock.yahoo.com', 'tw.news.yahoo.com']:
            # Yahoo奇摩股市
            item = soup.find('div', class_='caas-body')
            if item:
                content_parts = [elem.getText() for elem in item.find_all('p')]
            
        elif domain == 'money.udn.com':
            # 經濟日報
            item = soup.find('section', id='article_body')
            if item:
                content_parts = [elem.getText() for elem in item.find_all('p')]

        elif domain == 'www.chinatimes.com':
            # 中時新聞網
            item = soup.find('div', class_='article-body')
            if item:
                content_parts = [elem.getText() for elem in item.find_all('p')]

        elif domain == 'ctee.com.tw':
            # 工商時報
            item = soup.find('div', class_='entry-content')
            if item:
                content_parts = [elem.getText() for elem in item.find_all('p')]
                
        elif domain == 'news.cnyes.com':
            # 鉅亨網
            item = soup.find('div', itemprop='articleBody')
            if item:
                 content_parts = [elem.getText() for elem in item.find_all('p')]

        elif domain == 'finance.ettoday.net':
            # ETtoday
            item = soup.find('div', itemprop='articleBody')
            if item:
                content_parts = [elem.getText() for elem in item.find_all('p')]

        elif domain == 'fnc.ebc.net.tw':
            # EBC東森財經新聞
            script_tag = soup.find('script', string=re.compile('ReactDOM.render'))
            if script_tag:
                script_content = script_tag.string
                match = re.search(r'\{"content":"(.*?)"\}', script_content)
                if match:
                    content = match.group(1)
                    content = content.replace('\n', ' ').replace('/p', ' ')
                    content_parts = [re.sub(r'\\u003c.*?\\u003e', '', content)]
    except Exception as e:
        print(f"Error parsing content for {newsUrl}: {e}")
        return newsUrl, "Parse Error"


    if content_parts:
        content = ''.join(content_parts).replace('\r', ' ').replace('\n', ' ').replace(u'\xa0', ' ')
        return newsUrl, content.strip()
    else:
        return newsUrl, 'Content not found'


# 迴圈下載股票清單的Google新聞資料
stockNews = pd.DataFrame()
for iSearch in range(len(searchList)):

    print('目前正在搜尋股票: ' + searchList[iSearch] +
          ' 在Google的新聞清單  進度: ' + str(iSearch + 1) + ' / ' + str(len(searchList)))

    # 建立搜尋網址
    url = 'https://news.google.com/news/rss/search/section/q/' + \
          searchList[iSearch] + '/?hl=zh-tw&gl=TW&ned=zh-tw_tw'
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'xml')
    item = soup.find_all('item')
    rows = [arrangeGoogleNews(elem) for elem in item]

    # 組成pandas
    df = pd.DataFrame(data=rows, columns=['title', 'link', 'pub_date', 'description', 'source'])
    # 新增時間戳記欄位
    df.insert(0, 'search_time', time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), True)
    # 新增搜尋字串
    df.insert(1, 'search_key', searchList[iSearch], True)
    # 篩選最近的新聞
    df['pub_date'] = pd.to_datetime(df['pub_date'], utc=True)
    df = df[df['pub_date'] >= pd.to_datetime(nearStartDate, utc=True)]
    # 按發布時間排序
    df = df.sort_values(['pub_date']).reset_index(drop=True)

    # 迴圈爬取新聞連結與內容
    newsUrls = list()
    contents = list()
    if not df.empty:
      for iLink in range(len(df['link'])):

          print('目前正在下載: ' + searchList[iSearch] +
                ' 各家新聞  進度: ' + str(iLink + 1) + ' / ' + str(len(df['link'])))

          newsUrl, content = beautifulSoupNews(url=df['link'][iLink])
          newsUrls.append(newsUrl)
          contents.append(content)
          time.sleep(1) 

      # 新增新聞連結與內容欄位
      df['newsUrl'] = newsUrls
      df['content'] = contents

      # 儲存資料
      stockNews = pd.concat([stockNews, df])

# 輸出結果檢查
if not stockNews.empty:
    stockNews.to_csv('checkData.csv', index=False, encoding='utf-8-sig')
    print("Scraping complete. Data saved to checkData.csv")
else:
    print("No news found for the given criteria.")
