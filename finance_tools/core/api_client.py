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

    # ── 全市場抓取 ─────────────────────────────────────────────────────
    # 財報類資料（損益表、資產負債表、現金流量表、月營收）一律走這裡：不帶 data_id，
    # 一次請求拿回「某一天」全市場所有公司的資料（2026-10-22 實測 Sponsor Pro：
    # 資產負債表一季 2,368 家、約 186k 列、10 秒）。不帶 data_id 時 FinMind 只認
    # start_date 那一天，帶區間會回空，所以每一季／每一個月各發一次。
    # 舊做法是每家公司各打一次、每家之間 sleep 1～3 秒，2,300 家要跑 30～90 分鐘，
    # 還因此讓兩支週日排程時間重疊、互相覆蓋寫好的欄位。
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((requests.exceptions.ConnectionError, requests.exceptions.Timeout)),
        reraise=True,
    )
    def fetch_market_day(self, dataset: str, date: str) -> pd.DataFrame:
        """抓 `dataset` 在 `date` 這一天的全市場資料。

        - 來源沒有這天的資料（例如季報還沒公布）→ 空 DataFrame，這是正常情況。
        - 額度用盡 → `ApiExhaustedError`；其他非成功回應 → `ApiResponseError`。
          兩者都要讓整個任務停下來：抓不完整就不寫檔，不拿半套資料去合併。
        """
        resp = requests.get(
            config.FINMIND_DATA_URL,
            params={"dataset": dataset, "start_date": date},
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=config.MARKET_FETCH_TIMEOUT_SECONDS,
        )
        if resp.status_code == 402:
            raise ApiExhaustedError(f"{dataset} {date}：FinMind 額度已用盡（HTTP 402）")
        try:
            body = resp.json()
        except ValueError:
            raise ApiResponseError(f"{dataset} {date}：回應不是 JSON（HTTP {resp.status_code}）")
        msg = str(body.get("msg", ""))
        if resp.status_code != 200 or body.get("status") != 200:
            if "limit" in msg.lower() or "token" in msg.lower():
                raise ApiExhaustedError(f"{dataset} {date}：{msg}")
            raise ApiResponseError(f"{dataset} {date}：HTTP {resp.status_code} {msg}")
        return pd.DataFrame(body.get("data") or [])

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