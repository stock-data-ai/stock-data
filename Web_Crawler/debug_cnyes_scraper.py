import asyncio
import json
import re
from playwright.async_api import async_playwright

async def scrape_cnyes_news(stock_id: str):
    news_list = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        
        url = f"https://www.cnyes.com/twstock/{stock_id}/news"
        print(f"Navigating to {url}")
        
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Find the script tag containing __NEXT_DATA__
            # This is a common pattern for Next.js applications
            next_data_script = await page.evaluate('''() => {
                const script = document.querySelector('script#__NEXT_DATA__');
                return script ? script.textContent : null;
            }''')

            if next_data_script:
                data = json.loads(next_data_script)
                # Navigate through the JSON structure to find the news data
                # Based on the inspection of the truncated HTML
                symbol_news = data.get('props', {}).get('pageProps', {}).get('symbolNews', {})
                articles_data = symbol_news.get('data', [])

                for i, article in enumerate(articles_data):
                    if i >= 10: # Limit to first 10 articles for testing
                        break
                    
                    title = article.get('title')
                    news_id = article.get('newsId')
                    
                    if title and news_id:
                        # Construct the full news link
                        link = f"https://news.cnyes.com/news/id/{news_id}"
                        news_list.append({"title": title, "link": link})
            else:
                print("Could not find __NEXT_DATA__ script tag.")
                # Fallback to visual scraping if __NEXT_DATA__ is not found or malformed (less robust)
                # (This part is commented out for now to focus on JSON extraction)
                # await page.wait_for_selector('div.vue--main-news-list a', timeout=30000)
                # articles = await page.locator('div.vue--main-news-list a').all()
                # for i, article in enumerate(articles):
                #     if i >= 10:
                #         break
                #     title_element = await article.locator('h3').first.text_content()
                #     link = await article.get_attribute('href')
                #     title = title_element.strip() if title_element else "No Title"
                #     if link and not link.startswith('http'):
                #         link = f"https://www.cnyes.com{link}"
                #     news_list.append({"title": title, "link": link})

        except Exception as e:
            print(f"Error during scraping {url}: {e}")
        finally:
            await browser.close()
    return news_list

async def main():
    stock_id = "2330" # Test with TSMC
    news = await scrape_cnyes_news(stock_id)
    if news:
        print(f"\nFound {len(news)} news articles for stock {stock_id} on CNYES:")
        for idx, item in enumerate(news):
            print(f"{idx+1}. Title: {item['title']}\n   Link: {item['link']}\n")
    else:
        print(f"No news articles found for stock {stock_id} or an error occurred.")

if __name__ == "__main__":
    asyncio.run(main())