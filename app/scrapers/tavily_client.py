"""统一的 Tavily HTTP 客户端。

采集器只负责业务筛选；网络重试、错误分类和熔断集中在此处，避免四份实现
对 429、配额耗尽、认证失败等状态产生不同处理。客户端失败时返回 ``None``，
与现有采集器“失败后走下一通道”的契约保持一致。
"""

from __future__ import annotations

import http.client
import json
import random
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional


#: 可重试的 HTTP 状态码；认证错误、普通 4xx 和配额耗尽均不在此列。
RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
#: Tavily 的配额耗尽常见状态码。
QUOTA_EXHAUSTED_STATUS = 432
DEFAULT_ENDPOINT = "https://api.tavily.com"


def _urllib_post_transport(
    url: str,
    data: bytes,
    headers: dict[str, str],
    timeout: int,
):
    """用标准库发送 JSON POST，并保留测试 transport 的统一调用契约。"""
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=timeout)


class TavilyErrorType:
    """稳定的错误类型常量，便于日志、测试和健康记录复用。"""

    AUTH = "auth"
    CLIENT = "client"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    TIMEOUT = "timeout"
    SSL_EOF = "ssl_eof"
    CONNECTION_RESET = "connection_reset"
    NETWORK = "network"
    CIRCUIT_OPEN = "circuit_open"
    INVALID_RESPONSE = "invalid_response"
    INCOMPLETE_READ = "incomplete_read"
    BAD_STATUS_LINE = "bad_status_line"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TavilyCallResult:
    """一次调用的可观测结果。"""

    data: Optional[dict[str, Any]]
    error_type: Optional[str] = None
    attempts: int = 0
    status_code: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.data is not None and self.error_type is None


class TavilyClient:
    """带有限重试和熔断的 Tavily 客户端。

    ``transport``、``sleep``、``clock`` 均可注入，测试无需真实等待或联网。
    transport 接收 (url, data, headers, timeout)，返回带 ``read()`` 的响应。
    """

    def __init__(
        self,
        api_key: str = "",
        *,
        base_url: str = DEFAULT_ENDPOINT,
        max_retries: int = 3,
        timeout: int = 60,
        backoff_base: float = 1.0,
        backoff_max: float = 8.0,
        circuit_failure_threshold: int = 5,
        circuit_recovery_seconds: float = 60.0,
        transport: Optional[Callable[..., Any]] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = datetime.now,
        logger: Any = None,
    ) -> None:
        if max_retries < 1:
            raise ValueError("max_retries 必须至少为 1")
        if circuit_failure_threshold < 1:
            raise ValueError("circuit_failure_threshold 必须至少为 1")
        self.api_key = api_key or ""
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.timeout = timeout
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.circuit_failure_threshold = circuit_failure_threshold
        self.circuit_recovery_seconds = circuit_recovery_seconds
        self._transport = transport or _urllib_post_transport
        self._sleep = sleep
        self._clock = clock
        self._logger = logger
        self._state_lock = threading.RLock()
        self._failure_count = 0
        self._open_until: Optional[datetime] = None
        self._half_open = False

    @property
    def failure_count(self) -> int:
        with self._state_lock:
            return self._failure_count

    @property
    def circuit_is_open(self) -> bool:
        with self._state_lock:
            if self._open_until is None:
                return False
            return self._clock() < self._open_until

    def _log(self, level: str, message: str, *args: Any) -> None:
        if self._logger is not None:
            getattr(self._logger, level, self._logger.warning)(message, *args)

    def _classify_exception(self, exc: Exception) -> tuple[str, Optional[int]]:
        """将底层异常归并为稳定错误类型。"""
        if isinstance(exc, http.client.IncompleteRead):
            return TavilyErrorType.INCOMPLETE_READ, None
        if isinstance(exc, http.client.BadStatusLine):
            return TavilyErrorType.BAD_STATUS_LINE, None
        if isinstance(exc, urllib.error.HTTPError):
            code = int(exc.code)
            if code in (401, 403):
                return TavilyErrorType.AUTH, code
            if code == QUOTA_EXHAUSTED_STATUS:
                return TavilyErrorType.QUOTA_EXHAUSTED, code
            if code == 429:
                return TavilyErrorType.RATE_LIMIT, code
            if 500 <= code <= 599:
                return TavilyErrorType.SERVER, code
            return TavilyErrorType.CLIENT, code
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return TavilyErrorType.TIMEOUT, None
        if isinstance(exc, urllib.error.URLError):
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                return TavilyErrorType.TIMEOUT, None
            if isinstance(reason, ssl.SSLError):
                text = str(reason).lower()
                if "eof" in text or "unexpected_eof" in text:
                    return TavilyErrorType.SSL_EOF, None
                return TavilyErrorType.SSL_EOF, None
            text = str(reason).lower()
            if "reset" in text or "10054" in text or "connection aborted" in text:
                return TavilyErrorType.CONNECTION_RESET, None
            return TavilyErrorType.NETWORK, None
        if isinstance(exc, ssl.SSLError):
            text = str(exc).lower()
            if "eof" in text:
                return TavilyErrorType.SSL_EOF, None
            return TavilyErrorType.NETWORK, None
        if isinstance(exc, (ConnectionError, ConnectionResetError)):
            return TavilyErrorType.CONNECTION_RESET, None
        text = str(exc).lower()
        if "eof" in text and "ssl" in text:
            return TavilyErrorType.SSL_EOF, None
        if "reset" in text or "10054" in text or "connection aborted" in text:
            return TavilyErrorType.CONNECTION_RESET, None
        return TavilyErrorType.UNKNOWN, None

    @staticmethod
    def _is_retryable(error_type: str) -> bool:
        return error_type in {
            TavilyErrorType.TIMEOUT,
            TavilyErrorType.SSL_EOF,
            TavilyErrorType.CONNECTION_RESET,
            TavilyErrorType.INCOMPLETE_READ,
            TavilyErrorType.BAD_STATUS_LINE,
            TavilyErrorType.RATE_LIMIT,
            TavilyErrorType.SERVER,
            TavilyErrorType.NETWORK,
        }

    def _retry_delay(self, attempt: int) -> float:
        # 加少量抖动，避免多个来源同时重试；测试可通过 sleep 注入观察调用。
        base = min(self.backoff_max, self.backoff_base * (2 ** (attempt - 1)))
        return base + random.uniform(0, min(0.25, base * 0.1))

    def _record_failure(self, error_type: str) -> None:
        with self._state_lock:
            if error_type in {TavilyErrorType.AUTH, TavilyErrorType.CLIENT, TavilyErrorType.QUOTA_EXHAUSTED}:
                # 配置/配额问题不是瞬时故障，不应打开熔断器；半开探测也不会
                # 因这类错误永久占用探测名额，下一次调用仍可正常探测。
                if self._half_open:
                    self._half_open = False
                return
            self._failure_count += 1
            if self._half_open or self._failure_count >= self.circuit_failure_threshold:
                self._open_until = self._clock() + timedelta(seconds=self.circuit_recovery_seconds)
                self._half_open = False
                self._log("warning", "Tavily 熔断器已打开，%s 秒内停止请求", self.circuit_recovery_seconds)

    def _record_success(self) -> None:
        with self._state_lock:
            self._failure_count = 0
            self._open_until = None
            self._half_open = False

    def _reserve_call_slot(self) -> tuple[bool, bool]:
        """原子检查冷却时间并占用半开探测名额。"""
        with self._state_lock:
            now = self._clock()
            if self._open_until is not None and now < self._open_until:
                return True, False
            if self._open_until is not None:
                # 冷却到期后只允许一次半开探测；成功会关闭熔断器，
                # 失败会重新打开冷却窗口。
                if self._half_open:
                    return True, False
                self._half_open = True
                return False, True
            return False, False

    def call(self, endpoint: str, payload: dict[str, Any], *, timeout: Optional[int] = None, max_retries: Optional[int] = None) -> Optional[dict[str, Any]]:
        """POST 到 Tavily 并返回已解析 JSON；失败返回 None。"""
        result = self.call_with_details(endpoint, payload, timeout=timeout, max_retries=max_retries)
        return result.data

    def call_with_details(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> TavilyCallResult:
        """执行调用并返回错误元数据，供健康服务或诊断使用。"""
        if not self.api_key:
            self._log("debug", "Tavily API Key 未配置")
            return TavilyCallResult(None, TavilyErrorType.AUTH, 0)

        rejected, is_probe = self._reserve_call_slot()
        if rejected:
            self._log("warning", "Tavily 熔断中，拒绝 endpoint=%s", endpoint)
            return TavilyCallResult(None, TavilyErrorType.CIRCUIT_OPEN, 0)

        try:
            return self._perform_call(
                endpoint,
                payload,
                timeout=timeout,
                max_retries=max_retries,
            )
        finally:
            # 半开调用若因 KeyboardInterrupt 或未预期异常提前退出，不能永久
            # 占用唯一探测名额。正常成功/失败路径已在记录结果时清理。
            if is_probe:
                with self._state_lock:
                    if self._half_open:
                        self._half_open = False

    def _perform_call(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        timeout: Optional[int],
        max_retries: Optional[int],
    ) -> TavilyCallResult:
        """在已获得调用名额后执行请求和重试。"""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        attempts_allowed = self.max_retries if max_retries is None else max(1, max_retries)
        last_error: Optional[str] = None
        last_status: Optional[int] = None

        for attempt in range(1, attempts_allowed + 1):
            try:
                response = self._transport(url, data, headers, timeout or self.timeout)
                try:
                    raw = response.read()
                finally:
                    close = getattr(response, "close", None)
                    if close:
                        close()
                try:
                    parsed = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
                except (ValueError, TypeError, UnicodeDecodeError):
                    last_error = TavilyErrorType.INVALID_RESPONSE
                    self._log("warning", "Tavily 响应解析失败 endpoint=%s attempt=%s/%s", endpoint, attempt, attempts_allowed)
                    # 响应格式错误是服务端异常，按可重试处理。
                    if attempt < attempts_allowed:
                        self._sleep(self._retry_delay(attempt))
                        continue
                    self._record_failure(last_error)
                    return TavilyCallResult(None, last_error, attempt, last_status)
                if not isinstance(parsed, dict):
                    last_error = TavilyErrorType.INVALID_RESPONSE
                    self._log("warning", "Tavily 返回非对象 endpoint=%s attempt=%s/%s", endpoint, attempt, attempts_allowed)
                    if attempt < attempts_allowed:
                        self._sleep(self._retry_delay(attempt))
                        continue
                    self._record_failure(last_error)
                    return TavilyCallResult(None, last_error, attempt, last_status)
                self._record_success()
                self._log("debug", "Tavily 调用成功 endpoint=%s attempt=%s", endpoint, attempt)
                return TavilyCallResult(parsed, None, attempt, None)
            except Exception as exc:
                error_type, status = self._classify_exception(exc)
                last_error, last_status = error_type, status
                retryable = self._is_retryable(error_type)
                self._log(
                    "warning" if not retryable or attempt >= attempts_allowed else "debug",
                    "Tavily 调用失败 endpoint=%s error_type=%s attempt=%s/%s status=%s",
                    endpoint,
                    error_type,
                    attempt,
                    attempts_allowed,
                    status,
                )
                if not retryable:
                    # 普通客户端错误不累计熔断，避免一个坏请求拖垮其他查询。
                    self._record_failure(error_type)
                    return TavilyCallResult(None, error_type, attempt, status)
                if attempt < attempts_allowed:
                    self._sleep(self._retry_delay(attempt))
                    continue
                # 一次逻辑调用只记一次熔断失败，不能把同一次调用的多次重试
                # 当成多次独立故障，否则低阈值下会过早熔断。
                self._record_failure(error_type)
                return TavilyCallResult(None, error_type, attempt, status)

        return TavilyCallResult(None, last_error or TavilyErrorType.UNKNOWN, attempts_allowed, last_status)


_CLIENT_CACHE: dict[tuple[str, str], TavilyClient] = {}
_CLIENT_CACHE_LOCK = threading.RLock()


def create_tavily_client(api_key: str = "", **kwargs: Any) -> TavilyClient:
    """按 Key 和 API 地址复用客户端，让熔断状态跨采集器共享。

    注入 transport、sleep 或 clock 的客户端通常来自测试或特殊运行环境，
    不进入生产缓存，避免不同测试之间共享可变状态。
    """
    key = api_key or ""
    base_url = str(kwargs.get("base_url", DEFAULT_ENDPOINT)).rstrip("/")
    if any(name in kwargs for name in ("transport", "sleep", "clock")):
        return TavilyClient(api_key=key, **kwargs)

    cache_key = (key, base_url)
    with _CLIENT_CACHE_LOCK:
        client = _CLIENT_CACHE.get(cache_key)
        if client is None:
            client = TavilyClient(api_key=key, **kwargs)
            _CLIENT_CACHE[cache_key] = client
        return client


def reset_tavily_clients() -> None:
    """清空共享客户端缓存，供测试和运行时重新加载配置使用。"""
    with _CLIENT_CACHE_LOCK:
        _CLIENT_CACHE.clear()
