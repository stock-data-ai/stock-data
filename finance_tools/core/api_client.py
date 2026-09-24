import os
import requests
import logging
import pandas as pd
from FinMind.data import DataLoader
from typing import Tuple, Optional, Callable
import finance_tools.config as config
from finance_tools.core.exceptions import ApiExhaustedError, ApiResponseError

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)


class FinMindClient:
    def __init__(self, token: Optional[str] = None):
        if token:
            all_tokens = [t.strip() for t in token.split(",") if t.strip()]
        else:
            # GitHub Actions: FINMIND_API_TOKENS（workflow 覆寫為單一 token）
            # 本地開發: FINMIND_API_TOKEN_local（.env）
            raw = os.environ.get("FINMIND_API_TOKENS") or \
                  os.environ.get("FINMIND_API_TOKEN_local", "")
            all_tokens = [t.strip() for t in raw.split(",") if t.strip()]

        if not all_tokens:
            raise ValueError("未設定 FINMIND_API_TOKEN 或 FINMIND_API_TOKENS 環境變數。")

        self.token = all_tokens[0]
        token_index = 1  # 預設第 1 個
        if self.token in all_tokens:
            token_index = all_tokens.index(self.token) + 1
        self.loader = DataLoader(token=self.token)
        logger.info(f"已使用 Token #{token_index}/{len(all_tokens)} 初始化 FinMindClient: {self.token[:5]}...")

    def check_api_usage(self) -> Tuple[Optional[int], Optional[int]]:
        """
        Checks the current API usage for the token.
        Returns: (user_count, api_request_limit)
        """
        headers = {"Authorization": f"Bearer {self.token}"}
        url = config.FINMIND_API_URL

        try:
            resp = requests.get(url, headers=headers)
            resp.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
            data = resp.json()
            return data.get("user_count"), data.get("api_request_limit")
        except requests.exceptions.RequestException as e:
            logger.error(f"檢查 API 使用量時發生錯誤： {e}")
            return None, None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10), # Start with 1s, max 10s
        retry=retry_if_exception_type((requests.exceptions.ConnectionError, requests.exceptions.Timeout)),
        reraise=True  # Re-raise the last exception if all retries fail
    )
    def _retryable_api_call(self, fetch_fn: Callable, stock_id: str, start_date: str, **kwargs):
        """Wrapper to apply retry logic directly to the fetch_fn call."""
        # Execute the specific FinMind DataLoader method
        return fetch_fn(stock_id=stock_id, start_date=start_date, **kwargs)

    def _fetch(
        self,
        fetch_fn: Callable,  # This will be e.g., self.loader.taiwan_stock_financial_statement
        stock_id: str,
        start_date: str,
        label: str,
        **kwargs, # Pass kwargs down to fetch_fn as well
    ) -> Tuple[pd.DataFrame, bool]:
        """
        Internal method to fetch data and handle common FinMind API errors.
        Retries on connection/timeout errors, but not on API exhaustion.
        """
        try:
            df = self._retryable_api_call(fetch_fn, stock_id, start_date, **kwargs)

            # FinMind API returns {"data": []} on no data, not an error.
            if isinstance(df, dict) and df.get("data") is not None and not df["data"]:
                logger.warning(f"  FinMind 對於 {label} {stock_id} 沒有回傳資料。")
                return pd.DataFrame(), True
            
            # Handle potential API error messages or malformed responses
            if isinstance(df, dict) and df.get("data") is not None:
                error_msg = df.get("msg", "").lower()
                if "api token" in error_msg or "too many requests" in error_msg:
                    raise ApiExhaustedError(
                        f"擷取 {label} (代碼: {stock_id}) 時 API token 已耗盡。訊息: {error_msg}"
                    )
                else:
                    logger.error(f"  FinMind API 對於 {label} (代碼: {stock_id}) 回傳了非預期的錯誤格式。資料: {df}")
                    raise ApiResponseError(f"非預期的 API 回應 for {label} {stock_id}。訊息: {error_msg}")

            if not isinstance(df, pd.DataFrame):
                logger.error(f"  FinMind API 對於 {label} (代碼: {stock_id}) 回傳了非預期的非 DataFrame 格式。類型: {type(df)}")
                raise ApiResponseError(f"非預期的 API 回應格式 for {label} {stock_id}。")

            return df, True

        except KeyError as e:
            if str(e) == "'data'": # Specific KeyError often indicates API exhaustion for FinMind
                raise ApiExhaustedError(
                    f"擷取 {label} (代碼: {stock_id}) 時 API token 已耗盡 (KeyError: 'data')。"
                )
            logger.error(f"  處理 FinMind 資料時發生 KeyError for {label} {stock_id}: {e}")
            return pd.DataFrame(), False
        except ApiExhaustedError:
            raise # Re-raise immediately, as it's not a retryable error
        except ApiResponseError as e:
            logger.error(f"  API 回應錯誤 for {label} {stock_id}: {e}")
            return pd.DataFrame(), False
        except requests.exceptions.RequestException as e:
            # This block will only be hit if _retryable_api_call failed all retries (due to reraise=True)
            logger.error(f"  重試後網路/超時錯誤 for {label} {stock_id}: {e}")
            return pd.DataFrame(), False
        except Exception as e:
            logger.exception(f"  擷取 {label} (代碼: {stock_id}) 時發生未預期錯誤:")
            return pd.DataFrame(), False

    def fetch_financial_statements(
        self, stock_id: str, start_date: str
    ) -> Tuple[pd.DataFrame, bool]:
        return self._fetch(
            self.loader.taiwan_stock_financial_statement, stock_id, start_date, "財務報表"
        )

    def fetch_balance_sheet(
        self, stock_id: str, start_date: str
    ) -> Tuple[pd.DataFrame, bool]:
        return self._fetch(
            self.loader.taiwan_stock_balance_sheet, stock_id, start_date, "資產負債表"
        )

    def fetch_income_statement(
        self, stock_id: str, start_date: str
    ) -> Tuple[pd.DataFrame, bool]:
        return self._fetch(
            self.loader.taiwan_stock_income_statement, stock_id, start_date, "損益表"
        )

    def fetch_cash_flows_statement(
        self, stock_id: str, start_date: str
    ) -> Tuple[pd.DataFrame, bool]:
        return self._fetch(
            self.loader.taiwan_stock_cash_flows_statement, stock_id, start_date, "現金流量表"
        )

    def fetch_monthly_revenue(
        self, stock_id: str, start_date: str
    ) -> Tuple[pd.DataFrame, bool]:
        return self._fetch(
            self.loader.taiwan_stock_month_revenue, stock_id, start_date, "月營收"
        )

    def fetch_taiwan_stock_price(
        self, stock_id: str, start_date: str
    ) -> Tuple[pd.DataFrame, bool]:
        return self._fetch(
            self.loader.taiwan_stock_daily, stock_id, start_date, "台灣股價"
        )

    def fetch_per_pbr(
        self, stock_id: str, start_date: str
    ) -> Tuple[pd.DataFrame, bool]:
        return self._fetch(
            self.loader.taiwan_stock_per_pbr, stock_id, start_date, "本益比/股價淨值比"
        )

    def fetch_institutional_investors(
        self, stock_id: str, start_date: str
    ) -> Tuple[pd.DataFrame, bool]:
        return self._fetch(
            self.loader.taiwan_stock_institutional_investors, stock_id, start_date, "三大法人"
        )

    def fetch_shareholding(
        self, stock_id: str, start_date: str
    ) -> Tuple[pd.DataFrame, bool]:
        return self._fetch(
            self.loader.taiwan_stock_shareholding, stock_id, start_date, "外資持股"
        )