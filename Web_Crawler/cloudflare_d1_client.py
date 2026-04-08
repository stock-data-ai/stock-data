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
        payload = {
            "sql": sql,
            "params": params or []
        }
        
        try:
            response = requests.post(self.base_url, headers=self.headers, json=payload)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error executing D1 query: {e}")
            if e.response:
                print(f"Response: {e.response.text}")
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

    def migrate_add_company_code(self):
        """One-time migration: add company_code column to economic_daily_news if not exists."""
        try:
            self.execute_query("ALTER TABLE economic_daily_news ADD COLUMN company_code TEXT;")
            print("Migration: added company_code column to economic_daily_news")
        except Exception:
            pass  # Column already exists
        try:
            self.execute_query("""
                CREATE INDEX IF NOT EXISTS idx_economic_daily_news_company_code
                ON economic_daily_news(company_code);
            """)
            print("Migration: created company_code index")
        except Exception:
            pass

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
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(code, pub_date, pub_time, subject)
        );
        """
        self.execute_query(mops_sql)

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

        # Add company_code index for fast lookup
        self.execute_query("""
            CREATE INDEX IF NOT EXISTS idx_economic_daily_news_company_code
            ON economic_daily_news(company_code);
        """)

        # Table for Crawl Schedule (Smart Scheduler)
        schedule_sql = """
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
        """
        self.execute_query(schedule_sql)

        # Table for Crawl Logs
        logs_sql = """
        CREATE TABLE IF NOT EXISTS crawl_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_code TEXT NOT NULL,
            crawl_date TEXT NOT NULL,
            pre_filter_result TEXT,
            crawl_result TEXT,
            news_count INTEGER DEFAULT 0,
            error_message TEXT,
            duration_ms INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
        self.execute_query(logs_sql)

        print("Tables initialized (if not existed).")
