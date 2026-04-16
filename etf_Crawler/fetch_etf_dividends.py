import requests
import json
import os
import time
from datetime import datetime

def fetch_from_yahoo(code):
    yahoo_code = f"{code}.TW"
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{yahoo_code}?range=5y&interval=1d&events=div"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            yahoo_code = f"{code}.TWO"
            url = f"https://query2.finance.yahoo.com/v8/finance/chart/{yahoo_code}?range=5y&interval=1d&events=div"
            resp = requests.get(url, headers=headers, timeout=15)

        if resp.status_code == 200:
            data = resp.json()
            result = data.get("chart", {}).get("result", [{}])[0]
            events = result.get("events", {}).get("dividends", {})
            if not events: return None
            
            history = []
            for ts, div in events.items():
                date_str = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
                history.append({
                    "exDividendDate": date_str,
                    "paymentDate": None,
                    "amount": round(div["amount"], 4),
                    "type": "現金股利",
                    "name": result.get("meta", {}).get("symbol", code)
                })
            history.sort(key=lambda x: x["exDividendDate"], reverse=True)
            return history
    except:
        return None

def main():
    base_path = "/Users/chiwu/Documents/GitHub/stock-data"
    index_path = os.path.join(base_path, "src/data/etf/index.json")
    output_dir = os.path.join(base_path, "src/data/etf/dividends")
    os.makedirs(output_dir, exist_ok=True)

    if not os.path.exists(index_path):
        print(f"找不到 index: {index_path}")
        return

    with open(index_path, "r", encoding="utf-8") as f:
        etfs = json.load(f)

    print(f"準備更新 {len(etfs)} 檔 ETF 的配息資料...")
    
    for etf in etfs:
        code = etf["code"]
        name = etf["name"]
        
        history = fetch_from_yahoo(code)
        if history:
            data = {
                "code": code,
                "name": name,
                "history": history,
                "lastUpdated": datetime.now().strftime("%Y-%m-%d")
            }
            file_path = os.path.join(output_dir, f"{code}.json")
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"✅ {code} {name}: 補全 {len(history)} 筆數據")
            time.sleep(1) # 禮貌延遲
        else:
            print(f"⏩ {code} {name}: 無配息資料")

if __name__ == "__main__":
    main()
