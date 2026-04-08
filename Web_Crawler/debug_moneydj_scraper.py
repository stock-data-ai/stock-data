import requests
from bs4 import BeautifulSoup
import os

def scrape_moneydj_news(stock_id: str):
    """
    Scrapes news for a given stock ID from MoneyDJ.

    Args:
        stock_id: The stock ID in the format 'TW.XXXX'.

    Returns:
        A list of dictionaries, where each dictionary represents a news article
        with 'title' and 'link'.
    """
    news_list = []
    # Construct the URL
    url = f"https://www.moneydj.com/kmdj/common/listnewarticles.aspx?svc=NW&a={stock_id}"
    print(f"Fetching news from: {url}")

    try:
        # Fetch the HTML content
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()

        # Parse the HTML
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # --- Corrected Selector ---
        # Find the main table containing the news articles
        # The correct ID is 'MainContent_Contents_ArticleGridList1_gvList'
        news_table = soup.find('table', id='MainContent_Contents_ArticleGridList1_gvList')
        
        if news_table:
            # Find all rows in the table body, skipping the header row
            rows = news_table.find_all('tr')[1:] 
            for row in rows:
                # The link and title are in the second 'td', which has class 'ArticleTitle'
                cell = row.find('td', class_='ArticleTitle')
                if cell and cell.a:
                    link_tag = cell.a
                    title = link_tag.get('title')
                    href = link_tag.get('href')

                    if title and href:
                        # Prepend the domain if the link is relative
                        full_link = f"https://www.moneydj.com{href}" if href.startswith('/') else f"https://www.moneydj.com/kmdj/{href}"
                        
                        # The URL from the site is already relative to /kmdj/ so we can simplify
                        full_link = f"https://www.moneydj.com/kmdj/{href.lstrip('./')}"

                        news_list.append({'title': title.strip(), 'link': full_link})
        else:
            print("Could not find the news table with id 'MainContent_Contents_ArticleGridList1_gvList'.")

    except requests.exceptions.RequestException as e:
        print(f"Error fetching the URL: {e}")
    except Exception as e:
        print(f"An error occurred: {e}")

    return news_list

if __name__ == "__main__":
    # Test with the stock ID provided by the user
    test_stock_id = "TW.4552"
    articles = scrape_moneydj_news(test_stock_id)

    if articles:
        print(f"\nSuccessfully found {len(articles)} news articles for stock {test_stock_id}:")
        # Print the first 10 for brevity
        for idx, article in enumerate(articles[:10]):
            print(f"{idx+1}. Title: {article['title']}\n   Link: {article['link']}\n")
    else:
        print(f"No articles were found for stock {test_stock_id}.")
