"""
Qdrant 传输层重试

Qdrant Cloud 走公网，偶发的连接重置或响应处理失败会让整次召回直接失败
客户端本身没有自动重试能力，这里用一个带指数退避的包装统一兜底
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from httpx import TransportError
from qdrant_client.http.exceptions import ResponseHandlingException

from app.core.log import logger

T = TypeVar("T")

# 最多尝试三次，等待时间按 0.5s 1s 2s 递增
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 0.5

# 只重试网络类异常；请求本身返回 4xx 5xx 或结果解析失败重试没有意义
RETRYABLE_ERRORS = (ResponseHandlingException, TransportError)


async def with_transport_retry(
    operation: Callable[[], Awaitable[T]], action: str = "Qdrant 请求"
) -> T:
    """执行一次网络操作，遇到传输异常时按指数退避重试"""

    for attempt in range(RETRY_ATTEMPTS):
        try:
            return await operation()
        except RETRYABLE_ERRORS as exc:
            if attempt == RETRY_ATTEMPTS - 1:
                raise
            delay = RETRY_BASE_DELAY * 2**attempt
            # ResponseHandlingException 的 str() 是空串，真实原因保存在 source 上
            reason = getattr(exc, "source", exc)
            logger.warning(f"{action}失败，{delay}s 后重试：{reason}")
            await asyncio.sleep(delay)

    raise RuntimeError(f"{action}重试次数耗尽")
