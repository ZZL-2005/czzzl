import time
import functools
import random
import logging
from typing import Callable, Any

import httpx

logger = logging.getLogger(__name__)


class RetryableError(Exception):
    """可重试的错误"""
    def __init__(self, message: str, status_code: int = None, retry_after: float = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class NonRetryableError(Exception):
    """不可重试的错误"""
    pass


class TaskProcessingError(Exception):
    """任务处理过程中的错误"""
    def __init__(self, message: str, stage: str, details: dict = None):
        super().__init__(message)
        self.stage = stage
        self.details = details or {}


class ScorePollingTimeout(Exception):
    """评分轮询超时"""
    pass


class TokenBudgetExceeded(Exception):
    """Token 预算超出"""
    def __init__(self, used: int, limit: int):
        super().__init__(f"Token budget exceeded: {used}/{limit}")
        self.used = used
        self.limit = limit


def retry_with_backoff(
    max_retries: int = 3,
    initial_delay: float = 2.0,
    max_delay: float = 60.0,
    backoff_strategy: str = "exponential",
    retry_on_status_codes: list[int] = None,
    retry_on_exceptions: tuple = None,
):
    """重试装饰器，支持指数退避"""
    if retry_on_status_codes is None:
        retry_on_status_codes = [429, 500, 502, 503, 504]
    if retry_on_exceptions is None:
        retry_on_exceptions = (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.ReadTimeout,
            RetryableError,
            ConnectionError,
            TimeoutError,
        )

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retry_on_exceptions as e:
                    last_exception = e
                    if attempt == max_retries:
                        break

                    delay = _calculate_delay(attempt, initial_delay, max_delay, backoff_strategy)

                    if isinstance(e, RetryableError) and e.retry_after:
                        delay = max(delay, e.retry_after)

                    jitter = random.uniform(0, delay * 0.1)
                    sleep_time = delay + jitter

                    logger.warning(
                        f"[Retry] {func.__name__} attempt {attempt + 1}/{max_retries} "
                        f"failed with {type(e).__name__}: {e}. "
                        f"Retrying in {sleep_time:.1f}s"
                    )
                    time.sleep(sleep_time)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code in retry_on_status_codes:
                        last_exception = e
                        if attempt == max_retries:
                            break
                        delay = _calculate_delay(attempt, initial_delay, max_delay, backoff_strategy)
                        retry_after = e.response.headers.get("Retry-After")
                        if retry_after:
                            delay = max(delay, float(retry_after))
                        jitter = random.uniform(0, delay * 0.1)
                        sleep_time = delay + jitter
                        logger.warning(
                            f"[Retry] {func.__name__} attempt {attempt + 1}/{max_retries} "
                            f"got HTTP {e.response.status_code}. Retrying in {sleep_time:.1f}s"
                        )
                        time.sleep(sleep_time)
                    else:
                        raise

            raise last_exception

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs) -> Any:
            import asyncio
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except retry_on_exceptions as e:
                    last_exception = e
                    if attempt == max_retries:
                        break
                    delay = _calculate_delay(attempt, initial_delay, max_delay, backoff_strategy)
                    if isinstance(e, RetryableError) and e.retry_after:
                        delay = max(delay, e.retry_after)
                    jitter = random.uniform(0, delay * 0.1)
                    await asyncio.sleep(delay + jitter)
                    logger.warning(
                        f"[Retry] {func.__name__} async attempt {attempt + 1}/{max_retries} "
                        f"failed with {type(e).__name__}: {e}"
                    )
                except httpx.HTTPStatusError as e:
                    if e.response.status_code in retry_on_status_codes:
                        last_exception = e
                        if attempt == max_retries:
                            break
                        delay = _calculate_delay(attempt, initial_delay, max_delay, backoff_strategy)
                        retry_after = e.response.headers.get("Retry-After")
                        if retry_after:
                            delay = max(delay, float(retry_after))
                        jitter = random.uniform(0, delay * 0.1)
                        await asyncio.sleep(delay + jitter)
                        logger.warning(
                            f"[Retry] {func.__name__} async attempt {attempt + 1}/{max_retries} "
                            f"got HTTP {e.response.status_code}"
                        )
                    else:
                        raise
            raise last_exception

        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return wrapper

    return decorator


def _calculate_delay(attempt: int, initial: float, max_delay: float, strategy: str) -> float:
    if strategy == "exponential":
        delay = initial * (2 ** attempt)
    elif strategy == "linear":
        delay = initial * (attempt + 1)
    else:
        delay = initial
    return min(delay, max_delay)
