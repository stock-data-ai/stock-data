"""
初始化經濟日報資料庫（以主題分批）

依主題批次爬取公司新聞，建立基礎資料庫。
使用方式：
    uv run python -m Web_Crawler.money_udn.init_database --status
    uv run python -m Web_Crawler.money_udn.init_database --batch 1
    uv run python -m Web_Crawler.money_udn.init_database --topic silicon-photonics

每批次處理 2 個主題，共 8 批次。
執行進度會記錄到 .init_progress.json，下次可查看建議。
"""

import os
import sys
import json
import time
import random
import argparse
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional

# 加入父目錄到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from economic_daily_scraper import scrape_economic_daily_news

# 路徑設定
PROJECT_ROOT = Path(__file__).parent.parent.parent
TOPICS_PATH = PROJECT_ROOT / "src/data/layer0/topics.json"
INDEX_PATH = PROJECT_ROOT / "src/data/layer3/company-topics/index.json"
COMPANIES_PATH = PROJECT_ROOT / "src/data/layer3/companies/companies-all.json"
PROGRESS_PATH = Path(__file__).parent / ".init_progress.json"

# 每批次主題數
TOPICS_PER_BATCH = 2


def load_topics() -> List[Dict]:
    """載入所有 active 的台股主題"""
    with open(TOPICS_PATH, "r", encoding="utf-8") as f:
        topics = json.load(f)
    return [t for t in topics if t.get("active", True) and t["id"] != "us_housing"]


def load_company_index() -> Dict[str, List[str]]:
    """載入公司與主題的對應關係"""
    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_company_names() -> Dict[str, str]:
    """載入公司代碼與名稱的對應"""
    with open(COMPANIES_PATH, "r", encoding="utf-8") as f:
        companies = json.load(f)

    code_to_name = {}
    for code, data in companies.items():
        short_name = data.get("shortName")
        if short_name:
            code_to_name[code] = short_name
        elif data.get("name"):
            code_to_name[code] = data["name"]
    return code_to_name


def get_companies_by_topic(topic_id: str) -> List[Dict]:
    """取得特定主題的台股公司列表"""
    index = load_company_index()
    code_to_name = load_company_names()

    companies = []
    for code, topics in index.items():
        if topic_id in topics and code.isdigit():
            name = code_to_name.get(code, code)
            companies.append({"code": code, "name": name})

    companies.sort(key=lambda x: x["code"])
    return companies


def load_progress() -> Dict:
    """載入執行進度"""
    if PROGRESS_PATH.exists():
        with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"completed_topics": {}, "version": 1}


def save_progress(progress: Dict):
    """儲存執行進度"""
    with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def get_batches() -> List[List[Dict]]:
    """取得所有批次（每批次 2 個主題）"""
    topics = load_topics()
    batches = []

    for i in range(0, len(topics), TOPICS_PER_BATCH):
        batch = topics[i:i + TOPICS_PER_BATCH]
        batches.append(batch)

    return batches


def run_topic(topic_id: str, topic_name: str, delay: int = 10) -> Dict:
    """
    執行單一主題的爬取

    Returns:
        dict: 執行結果 {success: int, fail: int, companies: list}
    """
    companies = get_companies_by_topic(topic_id)

    if not companies:
        print(f"  主題 [{topic_name}] 沒有關聯的公司")
        return {"success": 0, "fail": 0, "companies": []}

    print(f"\n=== 主題: {topic_name} ({topic_id}) ===")
    print(f"公司數: {len(companies)}")
    print()

    success_count = 0
    fail_count = 0
    results = []

    for i, company in enumerate(companies, 1):
        code = company["code"]
        name = company["name"]

        print(f"[{i}/{len(companies)}] 爬取: {name} ({code})")

        try:
            df = scrape_economic_daily_news(name, code)
            if not df.empty:
                print(f"  ✓ 成功: {len(df)} 則新聞")
                success_count += 1
                results.append({"code": code, "name": name, "status": "success", "count": len(df)})
            else:
                print(f"  - 無新聞")
                success_count += 1
                results.append({"code": code, "name": name, "status": "empty", "count": 0})
        except Exception as e:
            print(f"  ✗ 失敗: {e}")
            fail_count += 1
            results.append({"code": code, "name": name, "status": "fail", "error": str(e)})

        # 隨機延遲（模擬人類行為，寬範圍避免被偵測）
        if i < len(companies):
            # 用 gauss 分佈：大部分落在 delay 附近，偶爾特別長
            random_delay = random.gauss(delay, delay * 0.6)
            # 偶爾插入較長的「休息」(10% 機率等 2~3 倍)
            if random.random() < 0.1:
                random_delay = delay * random.uniform(2.0, 3.0)
            random_delay = max(5, random_delay)
            print(f"  等待 {random_delay:.1f} 秒...")
            time.sleep(random_delay)

    return {"success": success_count, "fail": fail_count, "companies": results}


def run_batch(batch_num: int, delay: int = 10):
    """執行指定批次"""
    batches = get_batches()
    total_batches = len(batches)

    if batch_num < 1 or batch_num > total_batches:
        print(f"錯誤: 批次編號必須在 1-{total_batches} 之間")
        return

    batch = batches[batch_num - 1]
    progress = load_progress()

    print(f"╔══════════════════════════════════════════╗")
    print(f"║  初始化經濟日報資料庫 - 批次 {batch_num}/{total_batches}        ║")
    print(f"╚══════════════════════════════════════════╝")
    print()
    print(f"本批次主題:")
    for t in batch:
        print(f"  • {t['shortname']} ({t['id']})")
    print()

    total_success = 0
    total_fail = 0

    for topic in batch:
        topic_id = topic["id"]
        topic_name = topic["shortname"]

        result = run_topic(topic_id, topic_name, delay)
        total_success += result["success"]
        total_fail += result["fail"]

        # 記錄進度
        progress["completed_topics"][topic_id] = {
            "name": topic_name,
            "completed_at": datetime.now().isoformat(),
            "success": result["success"],
            "fail": result["fail"]
        }
        save_progress(progress)

    print()
    print(f"╔══════════════════════════════════════════╗")
    print(f"║  批次 {batch_num} 完成                              ║")
    print(f"╠══════════════════════════════════════════╣")
    print(f"║  成功: {total_success:4d}  失敗: {total_fail:4d}                  ║")
    print(f"╚══════════════════════════════════════════╝")

    # 顯示下一步建議
    if batch_num < total_batches:
        next_batch = batches[batch_num]
        print(f"\n下一步:")
        print(f"  uv run python -m Web_Crawler.money_udn.init_database --batch {batch_num + 1}")
        print(f"  主題: {', '.join(t['shortname'] for t in next_batch)}")


def run_single_topic(topic_id: str, delay: int = 10):
    """執行單一主題"""
    topics = load_topics()
    topic = next((t for t in topics if t["id"] == topic_id), None)

    if not topic:
        print(f"錯誤: 找不到主題 '{topic_id}'")
        print(f"可用主題: {', '.join(t['id'] for t in topics)}")
        return

    progress = load_progress()

    print(f"╔══════════════════════════════════════════╗")
    print(f"║  初始化經濟日報資料庫 - 單一主題         ║")
    print(f"╚══════════════════════════════════════════╝")

    result = run_topic(topic_id, topic["shortname"], delay)

    # 記錄進度
    progress["completed_topics"][topic_id] = {
        "name": topic["shortname"],
        "completed_at": datetime.now().isoformat(),
        "success": result["success"],
        "fail": result["fail"]
    }
    save_progress(progress)

    print()
    print(f"主題 [{topic['shortname']}] 完成: 成功 {result['success']}, 失敗 {result['fail']}")


def show_status():
    """顯示初始化狀態與建議"""
    topics = load_topics()
    batches = get_batches()
    progress = load_progress()
    completed = progress.get("completed_topics", {})

    # 檢查新增主題
    known_topics = set(completed.keys())
    current_topics = set(t["id"] for t in topics)
    new_topics = current_topics - known_topics

    print(f"╔══════════════════════════════════════════════════════════╗")
    print(f"║  經濟日報資料庫初始化狀態                                ║")
    print(f"╚══════════════════════════════════════════════════════════╝")
    print()

    # 顯示批次狀態
    print(f"批次進度 ({len(batches)} 批次，每批 {TOPICS_PER_BATCH} 主題):")
    print()

    incomplete_batches = []

    for i, batch in enumerate(batches, 1):
        batch_completed = all(t["id"] in completed for t in batch)
        status = "✓" if batch_completed else "○"

        if not batch_completed:
            incomplete_batches.append(i)

        topic_strs = []
        for t in batch:
            if t["id"] in completed:
                topic_strs.append(f"✓ {t['shortname']}")
            elif t["id"] in new_topics:
                topic_strs.append(f"★ {t['shortname']} (新)")
            else:
                topic_strs.append(f"○ {t['shortname']}")

        print(f"  批次 {i}: [{status}] {' / '.join(topic_strs)}")

    print()

    # 統計
    completed_count = len([t for t in topics if t["id"] in completed])
    print(f"進度: {completed_count}/{len(topics)} 主題已完成")

    # 新增主題提醒
    if new_topics:
        print()
        print(f"★ 發現 {len(new_topics)} 個新主題:")
        for tid in new_topics:
            topic = next(t for t in topics if t["id"] == tid)
            print(f"   • {topic['shortname']} ({tid})")

    # 建議
    print()
    if incomplete_batches:
        next_batch = incomplete_batches[0]
        batch = batches[next_batch - 1]
        print(f"建議執行:")
        print(f"   uv run python -m Web_Crawler.money_udn.init_database --batch {next_batch}")
        print(f"   主題: {', '.join(t['shortname'] for t in batch)}")
    else:
        print(f"所有主題都已完成初始化！")

    print()
    print(f"其他指令:")
    print(f"  --batch N        執行指定批次")
    print(f"  --topic ID       執行指定主題")
    print(f"  --reset          清除進度記錄")


def reset_progress():
    """清除進度記錄"""
    if PROGRESS_PATH.exists():
        PROGRESS_PATH.unlink()
        print("進度記錄已清除")
    else:
        print("沒有進度記錄")


# === 主題輪替模式 (GitHub Actions 用) ===

def _get_d1_client():
    """取得 D1 client"""
    from cloudflare_d1_client import CloudflareD1Client
    return CloudflareD1Client()


def _init_rotation_table(d1):
    """建立主題輪替表"""
    d1.execute_query("""
        CREATE TABLE IF NOT EXISTS topic_rotation (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_index INTEGER DEFAULT 0,
            last_topic_id TEXT,
            last_topic_name TEXT,
            last_run_at TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 確保有一筆初始資料
    d1.execute_query("""
        INSERT OR IGNORE INTO topic_rotation (id, current_index)
        VALUES (1, 0)
    """)


def _get_current_rotation_index(d1) -> int:
    """從 D1 讀取目前輪替到第幾個主題"""
    result = d1.execute_query("SELECT current_index FROM topic_rotation WHERE id = 1")
    if result.get('success') and result.get('result'):
        rows = result['result'][0].get('results', [])
        if rows:
            return rows[0]['current_index']
    return 0


def _update_rotation_index(d1, next_index: int, topic_id: str, topic_name: str):
    """更新 D1 中的輪替狀態"""
    now = datetime.now().isoformat()
    d1.execute_query("""
        UPDATE topic_rotation SET
            current_index = ?,
            last_topic_id = ?,
            last_topic_name = ?,
            last_run_at = ?,
            updated_at = ?
        WHERE id = 1
    """, [next_index, topic_id, topic_name, now, now])


def run_rotate(delay: int = 10):
    """
    主題輪替模式：自動取得下一個主題並執行

    每次執行一個主題，下次自動跑下一個，循環不斷。
    狀態存在 D1 的 topic_rotation 表中。
    """
    d1 = _get_d1_client()
    _init_rotation_table(d1)

    topics = load_topics()
    total = len(topics)

    if total == 0:
        print("錯誤: 沒有可用的主題")
        return

    # 讀取上次跑到哪
    current_index = _get_current_rotation_index(d1)

    # 防止超出範圍
    if current_index >= total:
        current_index = 0

    topic = topics[current_index]
    topic_id = topic["id"]
    topic_name = topic["shortname"]
    next_index = (current_index + 1) % total

    print(f"╔══════════════════════════════════════════╗")
    print(f"║  主題輪替模式                             ║")
    print(f"╠══════════════════════════════════════════╣")
    print(f"║  本次主題: {topic_name} ({topic_id})")
    print(f"║  進度: {current_index + 1}/{total}")
    print(f"║  下次主題: {topics[next_index]['shortname']}")
    print(f"╚══════════════════════════════════════════╝")
    print()

    # 執行爬取
    result = run_topic(topic_id, topic_name, delay)

    # 更新輪替狀態到 D1
    _update_rotation_index(d1, next_index, topic_id, topic_name)

    print()
    print(f"主題 [{topic_name}] 完成: 成功 {result['success']}, 失敗 {result['fail']}")
    print(f"下次將執行: {topics[next_index]['shortname']} ({topics[next_index]['id']})")

    # 也更新本地進度
    progress = load_progress()
    progress["completed_topics"][topic_id] = {
        "name": topic_name,
        "completed_at": datetime.now().isoformat(),
        "success": result["success"],
        "fail": result["fail"]
    }
    save_progress(progress)


def main():
    parser = argparse.ArgumentParser(description="初始化經濟日報資料庫（以主題分批）")
    parser.add_argument("--batch", type=int, default=None,
                        help="執行指定批次 (1-8)")
    parser.add_argument("--topic", type=str, default=None,
                        help="執行指定主題 (例如: silicon-photonics)")
    parser.add_argument("--delay", type=int, default=20,
                        help="每家公司之間的延遲秒數 (預設 20)")
    parser.add_argument("--status", action="store_true",
                        help="顯示初始化狀態與建議")
    parser.add_argument("--reset", action="store_true",
                        help="清除進度記錄")
    parser.add_argument("--rotate", action="store_true",
                        help="主題輪替模式：自動跑下一個主題 (GitHub Actions 用)")

    args = parser.parse_args()

    if args.reset:
        reset_progress()
    elif args.rotate:
        run_rotate(args.delay)
    elif args.status:
        show_status()
    elif args.batch:
        run_batch(args.batch, args.delay)
    elif args.topic:
        run_single_topic(args.topic, args.delay)
    else:
        show_status()


if __name__ == "__main__":
    main()
