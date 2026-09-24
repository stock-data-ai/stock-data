import os
import requests
import json
from pathlib import Path
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

# Load environment variables from .env file (find project root)
# Try multiple locations: current dir, parent dirs, project root
env_paths = [
    Path.cwd() / '.env',
    Path(__file__).parent.parent / '.env',  # Project root
]
for env_path in env_paths:
    if env_path.exists():
        load_dotenv(env_path)
        break
else:
    load_dotenv()  # Fallback to default behavior

class CloudflareD1Client:
    """
    Client for interacting with Cloudflare D1 via REST API.
    """
    def __init__(self, account_id: Optional[str] = None, database_id: Optional[str] = None, api_token: Optional[str] = None):
        self.account_id = account_id or os.environ.get('CLOUDFLARE_ACCOUNT_ID')
        self.database_id = database_id or os.environ.get('CLOUDFLARE_DATABASE_ID')
        self.api_token = api_token or os.environ.get('CLOUDFLARE_API_TOKEN')

        if not self.account_id:
            raise ValueError("CLOUDFLARE_ACCOUNT_ID is required")
        if not self.database_id:
            raise ValueError("CLOUDFLARE_DATABASE_ID is required")
        if not self.api_token:
            raise ValueError("CLOUDFLARE_API_TOKEN is required")

        self.base_url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/d1/database/{self.database_id}/query"
        self.headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }

    def execute_query(self, sql: str, params: List[Any] = None) -> Dict[str, Any]:
        """
        Execute a single SQL query.
        """
        payload: Dict[str, Any] = {"sql": sql}
        if params:
            payload["params"] = params

        try:
            response = requests.post(self.base_url, headers=self.headers, json=payload)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error executing D1 query: {e}")
            resp = getattr(e, "response", None)
            if resp is not None:
                print(f"Response body: {resp.text}")
            raise

    def batch_execute_query(self, sql: str, params_list: List[List[Any]]) -> List[Dict[str, Any]]:
        """
        Execute a batch of SQL queries (same SQL, different params).
        Cloudflare D1 REST API supports batching by sending an array of query objects doesn't directly support 
        'executemany' style in one object efficiently strictly speaking like python DBAPI, 
        but we can post multiple commands if needed or loop. 
        
        However, for bulk insert, it is often better to construct a single INSERT statement with multiple values 
        if the API limit allows, or send sequential requests.
        
        Here we will implement a simple loop wrapper for safety, or we can look into batch endpoint if available.
        For now, let's treat it as sequential calls to ensure reliability or construct a bulk INSERT string.
        """
        results = []
        # Fallback to creating a BULK INSERT statement to minimize network calls if possible,
        # but for simplicity and safety against SQL injection, we should reuse logical blocks.
        #
        # If the user passes many params, we might want to group them.
        # Let's perform sequential inserts for now or group them.
        
        # Actually checking D1 docs, the /query endpoint accepts "params": [...] 
        # But it executes one SQL statement.
        
        for params in params_list:
            results.append(self.execute_query(sql, params))
            
        return results

    def init_tables(self):
        """
        Initialize the generic tables if they don't exist.
        """
        # Table for MOPS Announcements
        # code (TEXT), pub_date (TEXT), pub_time (TEXT), subject (TEXT), source (TEXT)
        # We add 'id' as composite key or just rely on properties.
        # Let's add a simple primary key ID or composite unique constraint.
        mops_sql = """
        CREATE TABLE IF NOT EXISTS mops_announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT,
            name TEXT,
            pub_date TEXT,
            pub_time TEXT,
            subject TEXT,
            source TEXT,
            content TEXT,
            speaker TEXT,
            event_date TEXT,
            enter_date_roc TEXT,
            serial_number INTEGER,
            market_kind TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(code, pub_date, pub_time, subject)
        );
        """
        self.execute_query(mops_sql)

        # Migration: add new columns to existing table (safe to run repeatedly)
        for col, col_type in [
            ('content', 'TEXT'), ('speaker', 'TEXT'), ('event_date', 'TEXT'),
            ('enter_date_roc', 'TEXT'), ('serial_number', 'INTEGER'), ('market_kind', 'TEXT'),
        ]:
            try:
                self.execute_query(f"ALTER TABLE mops_announcements ADD COLUMN {col} {col_type};")
            except Exception:
                pass  # Column already exists

        # Table for Economic Daily News
        # pub_date (TEXT), title (TEXT), link (TEXT), source (TEXT)
        news_sql = """
        CREATE TABLE IF NOT EXISTS economic_daily_news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_code TEXT,
            pub_date TEXT,
            title TEXT,
            link TEXT,
            source TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(link)
        );
        """
        self.execute_query(news_sql)

        # Composite indexes aligned with stock_map/migrations/optimize_premium_db_indexes.sql.
        # Do NOT recreate the old single-column idx_economic_daily_news_company_code here:
        # it was dropped in that migration (redundant with idx_news_company_date).
        self.execute_query("""
            CREATE INDEX IF NOT EXISTS idx_news_company_date
            ON economic_daily_news(company_code, pub_date DESC);
        """)
        self.execute_query("""
            CREATE INDEX IF NOT EXISTS idx_news_pub_date
            ON economic_daily_news(pub_date DESC);
        """)

        print("Tables initialized (if not existed).")

    def init_crawler_tables(self):
        """
        Initialize crawler management tables in the crawler DB (stock-map-crawler).
        Use with CloudflareD1Client(database_id=os.environ['CLOUDFLARE_CRAWLER_DB_ID']).
        """
        self.execute_query("""
        CREATE TABLE IF NOT EXISTS crawl_schedule (
            company_code TEXT PRIMARY KEY,
            company_name TEXT NOT NULL,
            priority INTEGER DEFAULT 5,
            last_crawled TEXT,
            last_crawl_status TEXT,
            total_crawls INTEGER DEFAULT 0,
            total_hits INTEGER DEFAULT 0,
            recent_hits INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        self.execute_query("""
        CREATE TABLE IF NOT EXISTS topic_rotation (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_index INTEGER DEFAULT 0,
            last_topic_id TEXT,
            last_topic_name TEXT,
            last_run_at TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        self.execute_query("INSERT OR IGNORE INTO topic_rotation (id, current_index) VALUES (1, 0)")
        print("Crawler tables initialized (if not existed).")
