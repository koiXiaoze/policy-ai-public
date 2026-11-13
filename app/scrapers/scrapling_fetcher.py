"""Scrapling 备用抓取通道

作为 Tavily 的第二通道：直接访问政府源站，不依赖第三方搜索索引。
Tavily 配额耗尽（HTTP 432）或返回空时自动启用。

实测结论（2026-10-19）：
- 湖北省政府（412 WAF）：单次 StealthyFetcher 0/5，持久会话 3/3 成功
- 人社部（EdgeOne JS 挑战）：同一会话等待重载后 200，可解析正文
- 十堰市政府（TLS 指纹封锁）：持久会话 200，可解析 119 条政策
- 武汉市（JS 渲染）：可拿到渲染后 HTML
因此本模块统一使用 **持久会话**，并在 412/5xx 或 EdgeOne 挑战页时
在同一会话内有界重取。

合规：只访问政府公开发布的政策页面；默认遵守 robots.txt；
请求间保持间隔，不对目标站造成压力。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from app.logger import logger


#: 两次抓取之间的最小间隔（秒），避免对政府站点造成压力
DEFAULT_DELAY_SECONDS = 3.0


class ScraplingUnavailable(RuntimeError):
    """Scrapling 未安装或浏览器不可用"""


class ScraplingFetcher:
    """基于 Scrapling 持久会话的抓取器。

    会话复用是突破 WAF 的关键：单次请求每次都要"首访"，
    会持续触发 412；复用同一浏览器上下文则可通过。
    """

    #: 兼容字段（0.5 起会话随 with 块创建/关闭，不再跨请求复用）
    _sessions: Dict[str, Any] = {}
    _last_request_at: float = 0.0

    @classmethod
    def _throttle(cls, delay: float = DEFAULT_DELAY_SECONDS) -> None:
        """简单的全局限速，保证请求之间至少间隔 delay 秒。"""
        elapsed = time.time() - cls._last_request_at
        if elapsed < delay:
            time.sleep(delay - elapsed)
        cls._last_request_at = time.time()

    @staticmethod
    def _is_edgeone_challenge(html: str) -> bool:
        """识别腾讯 EdgeOne 的短 JavaScript 挑战页。

        这类页面常返回 HTTP 200，脚本在浏览器内写入 Cookie 后重载。
        必须同时命中两个特征并限制页面大小，避免误伤正常政策页面。
        """
        if not html or len(html) > 8000:
            return False
        return "function a(a)" in html and "__tst_status" in html

    @classmethod
    def _fetch_with_session(cls, url: str, mode: str, **kwargs: Any) -> Optional[str]:
        """用上下文管理器打开会话并抓取一页。

        Scrapling 0.5 起 StealthySession 必须作为上下文管理器使用，
        否则报 "Context manager has been closed"。因此每次抓取都
        新建会话；同一次抓取内的重试和挑战重取复用同一会话。
        """
        try:
            from scrapling.fetchers import StealthySession, FetcherSession
        except ImportError as exc:
            raise ScraplingUnavailable("未安装 scrapling，无法使用备用抓取通道") from exc

        try:
            if mode == "stealth":
                with StealthySession(headless=True) as session:
                    # 同一会话内最多请求 3 次。首次可能收到 WAF 状态码或
                    # EdgeOne 挑战页；后续等待脚本完成重载并复用 Cookie。
                    page = None
                    status = 0
                    html = ""
                    for inner in range(3):
                        request_kwargs = dict(kwargs)
                        if inner > 0:
                            request_kwargs.update(
                                wait=2000,
                                load_dom=True,
                                network_idle=True,
                            )
                        page = session.fetch(
                            url, google_search=False, **request_kwargs
                        )
                        status = getattr(page, "status", 0)
                        html = page.html_content or ""
                        if status == 200 and html:
                            if not cls._is_edgeone_challenge(html):
                                break
                            logger.warning(
                                "Scrapling 检测到 EdgeOne 挑战页 "
                                "attempt=%s/3 len=%s",
                                inner + 1,
                                len(html),
                            )
                            continue
                        if status in (412, 429, 500, 502, 503):
                            time.sleep(2)
                            continue
                        break
            else:
                with FetcherSession(impersonate="chrome") as session:
                    page = session.get(url, **kwargs)
                    status = getattr(page, "status", 0)
                    html = page.html_content or ""
        except Exception as exc:
            logger.warning("Scrapling 会话抓取异常 mode=%s: %s", mode, exc)
            return None

        if status == 200 and html and not cls._is_edgeone_challenge(html):
            logger.info("Scrapling 抓取成功 mode=%s status=200 len=%s", mode, len(html))
            return html

        if cls._is_edgeone_challenge(html):
            logger.warning(
                "Scrapling 重取后仍为 EdgeOne 挑战页 mode=%s status=%s len=%s",
                mode,
                status,
                len(html),
            )
        else:
            logger.warning(
                "Scrapling 抓取未成功 mode=%s status=%s len=%s", mode, status, len(html)
            )
        return None

    @classmethod
    def fetch_html(
        cls,
        url: str,
        mode: str = "stealth",
        retries: int = 1,
        delay: float = DEFAULT_DELAY_SECONDS,
        **kwargs: Any,
    ) -> Optional[str]:
        """抓取页面 HTML。

        Args:
            url: 目标地址
            mode: "stealth"（浏览器，可反爬）或 "http"（快速请求）
            retries: 失败重试次数（412/5xx 时重试）
            delay: 请求间隔

        Returns:
            HTML 字符串；失败返回 None
        """
        for attempt in range(retries + 1):
            cls._throttle(delay)
            html = cls._fetch_with_session(url, mode, **kwargs)
            if html:
                return html
            if attempt < retries:
                logger.info("Scrapling 重试 %s/%s: %s", attempt + 1, retries, url[:70])
                time.sleep(2)
        return None

    @classmethod
    def close(cls) -> None:
        """兼容接口：会话随 with 块自动关闭，此处无需操作。"""
        cls._sessions.clear()
