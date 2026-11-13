"""十堰市政府政策文件采集器
支持在线抓取 / Tavily API 回退 / 本地文件回退
"""

from __future__ import annotations

import json
import os
import re
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from app.scrapers.base import BaseScraper, ScrapedItem
from app.scrapers.content_cleaner import clean_title
from app.scrapers.tavily_client import create_tavily_client
from app.logger import logger

# Tavily API 配置
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
TAVILY_API_BASE = "https://api.tavily.com"


class ShiyanScraper(BaseScraper):
    def __init__(self, source_config: dict):
        super().__init__(source_config)
        self.list_url = source_config.get(
            "list_url", "https://www.shiyan.gov.cn/xxgk/zc_67263/qt/"
        )
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
            "Referer": "https://www.shiyan.gov.cn/",
        }
        self.headers = dict(self.base_headers)
        self.days_back: Optional[int] = source_config.get("days_back")
        self._session = None

    def _get_session(self):
        if self._session is None:
            try:
                import cloudscraper
                self._session = cloudscraper.create_scraper()
                logger.info("使用 cloudscraper 创建会话")
                return self._session
            except ImportError:
                logger.debug("cloudscraper 未安装，使用 requests Session")
                self._session = requests.Session()
                self._session.headers.update(self.headers)
        return self._session

    def _random_headers(self, referer: str = "https://www.shiyan.gov.cn/"):
        h = dict(self.base_headers)
        h["Referer"] = referer
        if random.random() < 0.5:
            h["Accept-Encoding"] = "gzip, deflate"
        if random.random() < 0.5:
            h["Accept-Language"] = "zh-CN,zh;q=0.9"
        return h

    def _request(self, url: str):
        session = self._get_session()
        return session.get(url, timeout=30, headers=self._random_headers())

    def _list_page_urls(self) -> List[str]:
        """返回配置的全部列表栏目，并对重复地址去重。"""
        configured = self.config.get("list_page_urls") or [self.list_url]
        if isinstance(configured, str):
            configured = [configured]
        pages: List[str] = []
        for value in configured:
            if not isinstance(value, str):
                continue
            page_url = value.strip()
            if page_url and page_url not in pages:
                pages.append(page_url)
        return pages or [self.list_url]

    @staticmethod
    def _is_allowed_host(url: str) -> bool:
        """只允许十堰市政府主站，防止伪域名和外站链接混入。"""
        try:
            parsed = urlsplit(url)
        except ValueError:
            return False
        return (
            parsed.scheme.lower() in {"http", "https"}
            and (parsed.hostname or "").lower().rstrip(".") == "www.shiyan.gov.cn"
        )

    def _normalize_url(self, href: str, base_url: Optional[str] = None) -> str:
        """按当前列表页地址解析链接，兼容绝对、根相对和相对路径。"""
        return urljoin(base_url or self.list_url, href.strip())

    def _clean_and_normalize_url(
        self, url: str, base_url: Optional[str] = None
    ) -> str:
        """清理 URL 并归一化为完整链接。"""
        url = url.strip()
        if '"' in url:
            url = url.split('"')[0].strip()
        if "'" in url:
            url = url.split("'")[0].strip()
        return self._normalize_url(url, base_url)

    @staticmethod
    def _safe_decode_mojibake(text: str) -> str:
        """严格尝试修复 UTF-8 被按单字节编码解释后的乱码。"""
        # 普通问号、英文和正常中文都不应触发转码。乱码文本必须出现
        # 典型单字节残留，并且修复后中文数量确实增加才接受。
        if not re.search(r"[\u0080-\u009fÃÂâäåæçèéðï]", text):
            return text

        original_cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
        for encoding in ("latin-1", "cp1252"):
            try:
                repaired = text.encode(encoding).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            repaired_cjk = len(re.findall(r"[\u4e00-\u9fff]", repaired))
            if repaired_cjk > original_cjk:
                return repaired
        return text

    def _ensure_utf8(self, text: str) -> str:
        """严格修复常见 mojibake；无法确认安全时保持原文。"""
        if not text:
            return text
        return self._safe_decode_mojibake(text)

    @staticmethod
    def _clean_policy_title(value: str) -> Optional[str]:
        """清洗标题并拒绝任何已确认无法可靠还原的乱码。"""
        if not value:
            return None
        if "�" in value or "锟斤拷" in value:
            return None
        if re.search(r"[\u0080-\u009f]", value):
            return None
        cleaned = clean_title(value)
        if not cleaned or len(cleaned) < 5:
            return None
        return cleaned

    @staticmethod
    def _merge_unique_items(
        target: List[ScrapedItem], incoming: List[ScrapedItem]
    ) -> None:
        """按 URL 合并列表，避免多个栏目重复写入同一政策。"""
        seen = {item.url for item in target}
        for item in incoming:
            if item.url and item.url not in seen:
                target.append(item)
                seen.add(item.url)

    @staticmethod
    def _failure_reason(channel: str, failures: List[str]) -> str:
        detail = "；".join(failures[:4])
        if len(failures) > 4:
            detail += f"；另有 {len(failures) - 4} 个栏目失败"
        return f"{channel}部分列表栏目采集失败: {detail}"

    def _failed_page_urls(self, failures: List[str]) -> List[str]:
        """从失败明细中还原失败栏目，避免成功栏目被重复抓取。"""
        failed: List[str] = []
        for failure in failures:
            for page_url in self._list_page_urls():
                if failure.startswith(f"{page_url}:") and page_url not in failed:
                    failed.append(page_url)
        return failed

    # ========== Tavily API 方法 ==========

    def _tavily_api_call(self, endpoint: str, payload: dict, timeout: int = 60, max_retries: int = 3) -> Optional[dict]:
        client = create_tavily_client(TAVILY_API_KEY, timeout=timeout, max_retries=max_retries, logger=logger)
        result = client.call_with_details(
            endpoint,
            payload,
            timeout=timeout,
            max_retries=max_retries,
        )
        self.last_fetch_error_type = result.error_type if not result.ok else None
        return result.data

    def _tavily_extract(self, url: str, depth: str = "advanced") -> Optional[dict]:
        return self._tavily_api_call("/extract", {"urls": [url], "depth": depth})

    def _tavily_search(self, query: str, max_results: int = 5) -> Optional[dict]:
        return self._tavily_api_call("/search", {"query": query, "max_results": max_results})

    def _extract_date_from_text(self, text: str) -> Optional[datetime]:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if m:
            try:
                return datetime.strptime(m.group(1), "%Y-%m-%d")
            except Exception:
                pass
        m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except Exception:
                pass
        m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", text)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except Exception:
                pass
        m = re.search(r"(\d{4})/(\d{2})/(\d{2})", text)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except Exception:
                pass
        return None

    def _extract_date_from_url(self, url: str) -> Optional[datetime]:
        patterns = [
            r"/(\d{4})/(\d{2})/(\d{2})/",
            r"/(\d{4})(\d{2})(\d{2})/",
            r"/t(\d{4})(\d{2})(\d{2})_",
        ]
        for pattern in patterns:
            m = re.search(pattern, url)
            if m:
                try:
                    return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                except Exception:
                    pass
        return None

    def _tavily_fetch_list(
        self, page_urls: Optional[List[str]] = None
    ) -> Tuple[List[ScrapedItem], List[str]]:
        """调用 Tavily；可只补抓指定栏目，返回合并结果和失败栏目。"""
        if not TAVILY_API_KEY:
            logger.info("未配置 TAVILY_API_KEY，跳过 Tavily 抓取")
            return [], ["Tavily 未配置"]

        logger.info("使用 Tavily extract API (advanced) 从列表页面获取十堰市政策...")
        all_links = []
        seen_urls = set()
        failures: List[str] = []
        cutoff = datetime.now() - timedelta(days=self.days_back) if self.days_back else None

        for page_url in page_urls if page_urls is not None else self._list_page_urls():
            page_has_valid_policy = False
            page_match_count = 0
            try:
                logger.info("Tavily extract 页面: %s", page_url)
                extract_data = self._tavily_extract(page_url, "advanced")

                if not extract_data or not extract_data.get("results"):
                    logger.warning("Tavily extract 无结果数据: %s", page_url)
                    failures.append(f"{page_url}: Tavily 未返回页面内容")
                    continue

                for result in extract_data["results"]:
                    raw_content = result.get("raw_content", result.get("content", ""))
                    if not raw_content:
                        continue

                    raw_content = self._ensure_utf8(raw_content)

                    # 三类输出统一归一化为 (标题, 链接, 日期)，避免把
                    # HTML 正则的 URL 字段误当成标题。
                    markdown_pattern = (
                        r'\*\s+\[([^\]]+)\]\((https?://[^)\s"]+)[^)]*\)'
                        r'\s*(\d{4}-\d{2}-\d{2})'
                    )
                    html_pattern = (
                        r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>"
                        r"([^<]+)</a>.*?(\d{4}-\d{2}-\d{2})"
                    )
                    text_pattern = (
                        r"([^\n]{10,}?)\s+(https?://[^\s]+)"
                        r"\s+(\d{4}-\d{2}-\d{2})"
                    )

                    markdown_matches = re.findall(
                        markdown_pattern, raw_content, re.DOTALL | re.IGNORECASE
                    )
                    html_matches = re.findall(
                        html_pattern, raw_content, re.DOTALL | re.IGNORECASE
                    )
                    text_matches = re.findall(
                        text_pattern, raw_content, re.DOTALL | re.IGNORECASE
                    )

                    matches = []
                    matches.extend(
                        (title, url, date_str)
                        for title, url, date_str in markdown_matches
                    )
                    matches.extend(
                        (title, url, date_str)
                        for url, title, date_str in html_matches
                    )
                    matches.extend(
                        (title, url, date_str)
                        for title, url, date_str in text_matches
                    )
                    page_match_count += len(matches)

                    logger.info(
                        "正则匹配到 %s 条结果 (页面: %s)", len(matches), page_url
                    )

                    for title, url, date_str in matches:
                        if not isinstance(title, str) or not isinstance(url, str):
                            continue
                        title = self._clean_policy_title(title)
                        if not title:
                            continue

                        full_url = self._clean_and_normalize_url(url, page_url)
                        if full_url in seen_urls:
                            continue
                        if not self._is_valid_policy_url(full_url):
                            continue
                        if "解读" in title or "【解读】" in title or "[解读]" in title:
                            logger.info("解读页面，跳过: %s", title[:30])
                            continue

                        # 结构校验通过即视为该栏目有效；即使同一政策也出现
                        # 在其他栏目，去重只能影响合并结果，不能伪造栏目失败。
                        page_has_valid_policy = True
                        if full_url in seen_urls:
                            continue

                        # 只有通过标题、域名和详情页校验后才占用去重键，
                        # 避免错误候选遮挡后续同一链接的有效标题。
                        seen_urls.add(full_url)

                        # 日期优先级（实测校准）：
                        # 1. 列表页明示日期 —— 官网展示的成文/发布日
                        #    （实测与 URL 归档日期系统性不同：
                        #     生育支持政策 列表 09-10 / URL 09-18）
                        # 2. URL 的 /tYYYYMMDD_ —— 归档日期，作为兜底
                        # 3. 正文日期
                        pub_time = None
                        if date_str:
                            pub_time = self._extract_date_from_text(date_str)
                        if not pub_time:
                            pub_time = self._extract_date_from_url(full_url)
                        if not pub_time:
                            pub_time = self._extract_date_from_text(raw_content[:5000])

                        if cutoff and pub_time and pub_time < cutoff:
                            logger.info(
                                "政策过期跳过: %s (发布日期: %s, 截止: %s)",
                                title[:30],
                                pub_time,
                                cutoff,
                            )
                            continue
                        if cutoff and not pub_time:
                            logger.info("无法获取日期，跳过: %s", title[:30])
                            continue

                        all_links.append({
                            "title": title.strip(),
                            "url": full_url.strip(),
                            "pub_time": pub_time,
                        })

                if not page_has_valid_policy:
                    if page_match_count:
                        failures.append(f"{page_url}: 匹配到候选但无有效政策")
                    else:
                        failures.append(f"{page_url}: 页面内容未匹配到政策条目")

            except Exception as exc:
                logger.warning("Tavily extract 页面失败 %s: %s", page_url, exc)
                failures.append(f"{page_url}: {exc}")

        if not all_links:
            logger.warning("Tavily extract 无有效结果")

        all_links.sort(key=lambda item: item["pub_time"] or datetime.min, reverse=True)
        items = [
            ScrapedItem(
                title=link["title"],
                url=link["url"],
                source_name=self.name,
                government_level=self.level,
                department="十堰市人民政府",
                publish_time=link["pub_time"],
            )
            for link in all_links
        ]
        logger.info("Tavily extract 获取到 %s 条十堰市政策", len(items))
        return items, failures

    def _infer_title_from_url(self, url: str) -> str:
        return url.split("/")[-1].replace(".shtml", "").replace(".html", "")

    # ========== 核心采集流程 ==========

    def _fill_failed_columns(
        self, items: List[ScrapedItem], page_urls: List[str]
    ) -> Tuple[List[ScrapedItem], List[str], List[str]]:
        """逐栏目补抓失败项，返回合并结果、仍未成功栏目和失败明细。

        先尝试 Tavily；未配置或仍失败的栏目再交给 Scrapling。空 HTML 与
        “HTTP 200 但未解析到政策”都不会被视为成功。
        """
        merged = list(items)
        remaining = list(page_urls)
        failures: List[str] = []

        if remaining and TAVILY_API_KEY:
            tavily_items, tavily_failures = self._tavily_fetch_list(remaining)
            self._merge_unique_items(merged, tavily_items)
            failed_urls = set(self._failed_page_urls(tavily_failures))
            remaining = [url for url in remaining if url in failed_urls]
            failures.extend(tavily_failures)
        elif remaining:
            failures.append("Tavily 未配置")

        if remaining:
            scrapling_items, scrapling_failures = self._fetch_via_scrapling(remaining)
            self._merge_unique_items(merged, scrapling_items)
            if self._has_scrapling_channel_failure(scrapling_failures):
                # 初始化失败并不携带具体栏目 URL；不能把“没有栏目级
                # 失败明细”误解为所有栏目都已经补齐。
                pass
            else:
                failed_urls = set(self._failed_page_urls(scrapling_failures))
                remaining = [url for url in remaining if url in failed_urls]
            failures.extend(scrapling_failures)

        return merged, remaining, failures

    @staticmethod
    def _has_scrapling_channel_failure(failures: List[str]) -> bool:
        """识别 Scrapling 通道整体失败，避免误清空待补栏目。"""
        return any(failure.startswith("Scrapling 初始化失败:") for failure in failures)

    def fetch(self) -> List[ScrapedItem]:
        logger.info("开始采集: %s", self.name)

        # 1. 直连抓取全部栏目。任一栏目成功都不能掩盖另一个栏目失败：
        # xxgk_zcfg 可能返回 200 但由 JavaScript 在浏览器端加载数据。
        items, direct_failures = self._fetch_online()
        if items:
            logger.info("直连抓取取得 %s 条，检查失败栏目", len(items))
            failed_urls = self._failed_page_urls(direct_failures)
            if failed_urls:
                items, unresolved, retry_failures = self._fill_failed_columns(
                    items, failed_urls
                )
                if unresolved:
                    self._mark_degraded(
                        self._failure_reason("栏目补抓", retry_failures),
                        self.last_fetch_error_type,
                    )
                else:
                    self._mark_live_success()
                return items
            if direct_failures:
                self._mark_degraded(
                    self._failure_reason("在线通道", direct_failures),
                    self.last_fetch_error_type,
                )
            else:
                self._mark_live_success()
            return items

        # 2. Tavily API 回退；部分成功时继续补抓失败栏目。
        logger.warning("直连抓取失败，尝试 Tavily API 回退")
        items, tavily_failures = self._tavily_fetch_list()
        if items:
            logger.info("Tavily API 回退取得 %s 条，检查失败栏目", len(items))
            failed_urls = self._failed_page_urls(tavily_failures)
            if failed_urls:
                items, unresolved, retry_failures = self._fill_failed_columns(
                    items, failed_urls
                )
                if unresolved:
                    self._mark_degraded(
                        self._failure_reason("栏目补抓", retry_failures),
                        self.last_fetch_error_type,
                    )
                else:
                    self._mark_live_success()
                return items
            if tavily_failures:
                self._mark_degraded(
                    self._failure_reason("Tavily 通道", tavily_failures),
                    self.last_fetch_error_type,
                )
            else:
                self._mark_live_success()
            return items

        # 2.5 Scrapling 直连通道（实测可突破 TLS 指纹封锁）
        logger.warning("Tavily 不可用，尝试 Scrapling 直连通道")
        items, scrapling_failures = self._fetch_via_scrapling()
        if items:
            logger.info("Scrapling 通道获取十堰政策: %s 条", len(items))
            if scrapling_failures:
                self._mark_degraded(
                    self._failure_reason("Scrapling 通道", scrapling_failures),
                    self.last_fetch_error_type,
                )
            else:
                self._mark_live_success()
            return items

        # 3. 本地文件回退
        logger.warning("Tavily API 失败，尝试本地文件回退")
        items = self._fetch_from_local()
        if items:
            logger.info("本地文件回退成功: %s 条", len(items))
            self._mark_degraded("在线通道未取得数据，使用本地文件旧数据", self.last_fetch_error_type)
            return items

        # 4. 最终降级：从库内已有政策回填
        logger.warning("所有采集方式均失败，回退到库内已有政策")
        items = self._fetch_from_database()
        if items:
            self._mark_degraded("在线通道未取得数据，使用数据库旧数据", self.last_fetch_error_type)
        else:
            self._mark_failed("在线通道未取得数据，本地文件和数据库均无可用数据", self.last_fetch_error_type)
        return items

    def _fetch_via_scrapling(
        self, page_urls: Optional[List[str]] = None
    ) -> Tuple[List[ScrapedItem], List[str]]:
        """遍历指定栏目抓取；返回合并结果和失败栏目。"""
        items: List[ScrapedItem] = []
        failures: List[str] = []
        try:
            from app.scrapers.scrapling_fetcher import ScraplingFetcher

            for page_url in page_urls if page_urls is not None else self._list_page_urls():
                try:
                    html = ScraplingFetcher.fetch_html(
                        page_url, mode="stealth", retries=1
                    )
                    if not html:
                        failures.append(f"{page_url}: 空响应")
                        continue
                    page_items = self._parse_html(html, base_url=page_url)
                    if not page_items:
                        failures.append(f"{page_url}: 未解析到政策")
                        continue
                    self._merge_unique_items(items, page_items)
                except Exception as exc:
                    logger.warning("Scrapling 栏目失败 %s: %s", page_url, exc)
                    failures.append(f"{page_url}: {exc}")
        except Exception as exc:
            logger.warning("Scrapling 通道失败: %s", exc)
            failures.append(f"Scrapling 初始化失败: {exc}")
        return items, failures

    def _fetch_from_database(self) -> List[ScrapedItem]:
        """从本地库回填政策，作为 Tavily 配额耗尽时的降级来源。"""
        try:
            from app.database import SessionLocal
            from app.models.policy import Policy
            db = SessionLocal()
            try:
                rows = (
                    db.query(Policy)
                    .filter(Policy.source_name == self.name)
                    .order_by(Policy.publish_time.desc())
                    .limit(30)
                    .all()
                )
                logger.info("库内回退获取 %s 政策: %s 条", self.name, len(rows))
                return [
                    ScrapedItem(
                        title=p.title, url=p.url, source_name=p.source_name,
                        government_level=p.government_level,
                        department=p.department, city=p.city,
                        publish_time=p.publish_time, content=p.content,
                    )
                    for p in rows
                ]
            finally:
                db.close()
        except Exception as exc:
            logger.warning("库内回退失败 %s: %s", self.name, exc)
            return []

    def _fetch_online(
        self, page_urls: Optional[List[str]] = None
    ) -> Tuple[List[ScrapedItem], List[str]]:
        """逐个抓取指定栏目，返回合并结果和失败栏目。"""
        items: List[ScrapedItem] = []
        failures: List[str] = []

        for page_url in page_urls if page_urls is not None else self._list_page_urls():
            try:
                resp = self._request(page_url)
                if resp.status_code == 200:
                    html = self._response_text(resp)
                    page_items = self._parse_html(html, base_url=page_url)
                    if page_items:
                        self._merge_unique_items(items, page_items)
                    else:
                        json_items, json_error = self._fetch_json_fallback(
                            html, page_url
                        )
                        if json_items:
                            logger.info(
                                "十堰 HTML 未解析到政策，同源 JSON 回退获取 %s 条",
                                len(json_items),
                            )
                            self._merge_unique_items(items, json_items)
                        else:
                            reason = "页面未解析到政策"
                            if json_error:
                                reason += f"；{json_error}"
                            failures.append(f"{page_url}: {reason}")
                elif resp.status_code == 412:
                    logger.warning("在线抓取遇到 412 WAF 拦截: %s", page_url)
                    failures.append(f"{page_url}: HTTP 412 WAF 拦截")
                else:
                    logger.warning("在线抓取失败: HTTP %s (%s)", resp.status_code, page_url)
                    failures.append(f"{page_url}: HTTP {resp.status_code}")
            except Exception as exc:
                logger.warning("在线抓取异常 %s: %s", page_url, exc)
                failures.append(f"{page_url}: {exc}")

        if not items and failures:
            self.last_fetch_error = self._failure_reason("在线通道", failures)
        return items, failures

    @staticmethod
    def _response_text(response) -> str:
        """优先严格按 UTF-8 原始字节解码，失败时保留响应自身解码结果。"""
        content = getattr(response, "content", None)
        if isinstance(content, (bytes, bytearray)) and content:
            try:
                return bytes(content).decode("utf-8")
            except UnicodeDecodeError:
                pass
        return getattr(response, "text", "") or ""

    def _discover_same_column_json_urls(
        self, soup: BeautifulSoup, page_url: str
    ) -> List[str]:
        """从列表页发现同源、同目录的 JSON 数据地址。"""
        page = urlsplit(page_url)
        page_directory = page.path.rsplit("/", 1)[0] + "/"
        discovered: List[str] = []

        for node in soup.find_all(True):
            values = [
                node.get(attribute)
                for attribute in ("href", "src", "data-url", "data-src")
            ]
            if node.name in {"script", "a", "link"}:
                values.append(node.get_text(" ", strip=True))
            for value in values:
                if isinstance(value, list):
                    value = " ".join(str(part) for part in value)
                if not isinstance(value, str) or ".json" not in value.lower():
                    continue
                compact = re.sub(r"\s+", "", value)
                for match in re.findall(
                    r"(?:https?:)?//[^'\"\s<>]+?\.json(?:\?[^'\"\s<>]*)?|"
                    r"(?:\.{0,2}/)?[^'\"\s<>]+?\.json(?:\?[^'\"\s<>]*)?",
                    compact,
                    flags=re.IGNORECASE,
                ):
                    normalized = self._normalize_url(match, page_url)
                    parsed = urlsplit(normalized)
                    if not self._is_allowed_host(normalized):
                        continue
                    if not (parsed.path or "").lower().endswith(".json"):
                        continue
                    if not (parsed.path or "").startswith(page_directory):
                        continue
                    if normalized not in discovered:
                        discovered.append(normalized)
        return discovered

    def _parse_policy_date(self, value: object) -> Optional[datetime]:
        """解析十堰 JSON 的日期格式，无法识别时返回 None。"""
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        match = re.search(r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}", text)
        if not match:
            return None
        normalized = match.group(0).replace("/", "-").replace(".", "-")
        try:
            return datetime.strptime(normalized, "%Y-%m-%d")
        except ValueError:
            return None

    def _parse_policy_json(
        self, payload: object, source_url: str
    ) -> List[ScrapedItem]:
        """严格解析同源政策 JSON，任一条目不可靠时跳过。"""
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            return []

        items: List[ScrapedItem] = []
        seen = set()
        cutoff = (
            datetime.now() - timedelta(days=self.days_back)
            if self.days_back
            else None
        )
        for record in payload["data"]:
            if not isinstance(record, dict):
                continue
            raw_title = record.get("DOCTITLE") or record.get("doctitle") or ""
            title = self._clean_policy_title(str(raw_title))
            if not title or "解读" in title:
                continue

            raw_url = record.get("url") or record.get("URL") or ""
            if not isinstance(raw_url, str) or not raw_url.strip():
                continue
            policy_url = self._normalize_url(raw_url, source_url)
            if not self._is_valid_policy_url(policy_url):
                continue

            publish_time = (
                self._parse_policy_date(record.get("DocRelTime"))
                or self._parse_policy_date(record.get("PubDate"))
                or self._extract_date_from_url(policy_url)
            )
            if publish_time is None:
                continue
            if cutoff and publish_time < cutoff:
                continue
            if policy_url in seen:
                continue
            seen.add(policy_url)
            items.append(
                ScrapedItem(
                    title=title,
                    url=policy_url,
                    source_name=self.name,
                    government_level=self.level,
                    department="十堰市人民政府",
                    publish_time=publish_time,
                )
            )
        return items

    def _fetch_json_fallback(
        self, html: str, page_url: str
    ) -> Tuple[List[ScrapedItem], Optional[str]]:
        """HTML 无政策时尝试同目录 JSON；无候选时保持原有失败语义。"""
        soup = BeautifulSoup(self._ensure_utf8(html or ""), "lxml")
        json_urls = self._discover_same_column_json_urls(soup, page_url)
        if not json_urls:
            return [], None

        errors: List[str] = []
        for json_url in json_urls:
            try:
                response = self._request(json_url)
                if response.status_code != 200:
                    errors.append(f"JSON HTTP {response.status_code}")
                    continue
                raw_text = self._response_text(response)
                payload = json.loads(raw_text)
                items = self._parse_policy_json(payload, json_url)
                if items:
                    return items, None
                errors.append("JSON 未包含有效政策")
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
                logger.warning("十堰 JSON 解析失败 %s: %s", json_url, exc)
                errors.append("JSON 解析失败")
            except Exception as exc:
                logger.warning("十堰 JSON 请求失败 %s: %s", json_url, exc)
                errors.append("JSON 请求失败")
        return [], "；".join(errors) if errors else None

    def _fetch_from_local(self) -> List[ScrapedItem]:
        """从本地 HTML 文件解析政策列表"""
        data_dir = Path("data")
        keywords = ["shiyan", "十堰", "十堰市"]
        for f in sorted(data_dir.glob("*.html")) + sorted(data_dir.glob("*.htm")):
            fname = f.name.lower()
            if not any(kw in fname for kw in keywords):
                continue
            try:
                html = f.read_text(encoding="utf-8", errors="ignore")
                items = self._parse_html(html)
                if items:
                    logger.info("从本地文件解析到 %s 条政策: %s", len(items), f.name)
                    return items
            except Exception:
                continue
        return []

    def _parse_html(
        self, html: str, base_url: Optional[str] = None
    ) -> List[ScrapedItem]:
        """解析 HTML 列表页面。

        Scrapling 返回的 HTML 偶有 UTF-8 被误读为 latin-1 的情况，
        先做一次编码修复，否则标题会变成 mojibake。
        """
        html = self._ensure_utf8(html or "")
        soup = BeautifulSoup(html, "lxml")
        items = []
        seen = set()
        cutoff = datetime.now() - timedelta(days=self.days_back) if self.days_back else None

        # 优先 list-group 结构；真实页面（Scrapling 渲染后）可能用其他容器，
        # 此时退化为"扫描所有含日期编号的政策链接"。
        # 注意：页面里可能存在用于导航的"空" list-group（只含栏目链接），
        # 真正的政策列表在别处。因此只有当 list-group 内确实含政策链接时
        # 才使用它，否则统一走通用扫描。
        list_group = soup.find("ul", class_="list-group")
        candidates = []
        if list_group and re.search(r"/t20\d{6}_\d+", str(list_group)):
            candidates = list_group.find_all("li", class_="list-group-item")

        if not candidates:
            logger.info("list-group 内无政策条目，改用通用链接扫描")
            candidates = [
                a for a in soup.find_all("a", href=True)
                if re.search(r"/t20\d{6}_\d+", a.get("href", ""))
            ]

        for node in candidates:
            a = node if node.name == "a" else node.find("a", href=True)
            if not a:
                continue

            href = (a.get("href") or "").strip()
            raw_title = a.get("title", "") or a.get_text(" ", strip=True)
            title = self._clean_policy_title(raw_title)

            if not title:
                if raw_title:
                    logger.warning("跳过无法可靠还原的标题: %s", raw_title[:80])
                continue
            if not href or href == "#" or "javascript" in href.lower():
                continue
            if "解读" in title:
                continue

            # 归一化 URL - 以当前栏目地址为基准处理相对路径。
            full_url = self._normalize_url(href, base_url)

            # 严格验证：必须是政策文档 URL
            if not self._is_valid_policy_url(full_url):
                logger.debug("跳过非政策链接: %s - %s", title[:30], full_url)
                continue

            # 从同级 span 获取日期
            pub_time = None
            span = node.find("span", class_=re.compile(r"text-nowrap")) if hasattr(node, "find") else None
            if span is None:
                mark = re.search(r"(20\d{2}-\d{2}-\d{2})", node.get_text(" ", strip=True) if hasattr(node, "get_text") else "")
                span = None
                if mark:
                    try:
                        pub_time = datetime.strptime(mark.group(1), "%Y-%m-%d")
                    except Exception:
                        pub_time = None
            if span:
                date_text = span.get_text(strip=True)
                m = re.search(r"(\d{4}-\d{2}-\d{2})", date_text)
                if m:
                    try:
                        pub_time = datetime.strptime(m.group(1), "%Y-%m-%d")
                    except Exception:
                        pass
            
            # 备选：从 URL 提取日期
            if not pub_time:
                pub_time = self._extract_date_from_url(full_url)

            if cutoff and pub_time and pub_time < cutoff:
                continue

            if full_url in seen:
                continue
            seen.add(full_url)

            items.append(
                ScrapedItem(
                    title=title,
                    url=full_url,
                    source_name=self.name,
                    government_level=self.level,
                    department="十堰市人民政府",
                    publish_time=pub_time,
                )
            )

        if items:
            logger.info("十堰市政府解析到 %s 条政策", len(items))
        return items

    def _is_valid_policy_url(self, url: str) -> bool:
        """判断是否为十堰市政府主站白名单栏目下的政策详情页。"""
        if not self._is_allowed_host(url):
            return False

        try:
            path = urlsplit(url).path
        except ValueError:
            return False

        prefixes = self.config.get("policy_path_prefixes") or []
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        prefixes = [
            prefix.strip()
            for prefix in prefixes
            if isinstance(prefix, str) and prefix.strip()
        ]
        if prefixes and not any(path.startswith(prefix) for prefix in prefixes):
            logger.debug("URL 不在政策栏目白名单: %s", url)
            return False

        if path.endswith("/") or path.endswith((".shtml", ".html")) is False:
            return False
        if re.search(r"/(?:index|list\d*)\.s?html?$", path, re.IGNORECASE):
            return False
        if not re.search(r"/\d{6}/t\d{8}_", path):
            logger.debug("URL 格式不匹配政策文档: %s", url)
            return False
        return True

    def _extract_by_selectors(self, soup, selectors):
        for tag, attrs in selectors:
            try:
                main = soup.find(tag, **attrs)
                if not main:
                    continue
                text = main.get_text(separator="\n", strip=True)
                if text and len(text) >= 50:
                    return text
            except Exception:
                continue
        return None

    def _extract_from_local(self, url: str) -> Optional[str]:
        """从本地文件中提取正文内容"""
        data_dir = Path("data")
        url_id_parts = url.split("/")[-1].replace(".shtml", "").replace(".html", "")
        url_id_digit = re.search(r"(\d+)$", url_id_parts)
        candidates = [url_id_parts]
        if url_id_digit:
            candidates.append(url_id_digit.group(1))
        extra_cands = re.findall(r"\d{4}-\d{2}-\d{2}|\d{8}", url)
        for ec in extra_cands:
            candidates.append(ec)
        keywords = ["shiyan", "十堰", "十堰市"]
        for f in (
            sorted(data_dir.glob("*.html"))
            + sorted(data_dir.glob("*.htm"))
            + sorted(data_dir.glob("*.txt"))
            + sorted(data_dir.glob("*.shtml"))
        ):
            try:
                fname = f.name.lower()
                if not any(kw in fname for kw in keywords):
                    continue
                html = f.read_text(encoding="utf-8", errors="ignore")
                if not any(cand.lower() in html.lower() for cand in candidates):
                    continue
                soup = BeautifulSoup(html, "lxml")
                text = self._extract_by_selectors(
                    soup,
                    [
                        ("div", {"class": "view TRS_UEDITOR trs_paper_default trs_web"}),
                        ("div", {"class": "article_content"}),
                        ("div", {"class": "content"}),
                        ("div", {"class": "article"}),
                        ("div", {"class": "text"}),
                        ("div", {"class": "TRS_Editor"}),
                        ("article", {}),
                        ("main", {}),
                    ],
                )
                if text:
                    logger.info("从本地文件提取正文成功: %s (%s 字符)", f.name, len(text))
                    return text[:12000]
            except Exception:
                continue
        return None

    
    def _try_playwright(self, url):
        try:
            import asyncio
            from playwright.async_api import async_playwright

            async def _extract():
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True)
                    page = await browser.new_page()
                    await page.goto(url, wait_until="networkidle", timeout=30000)

                    selectors = [
                        "div.view.TRS_UEDITOR.trs_paper_default.trs_web",
                        "div.article_content",
                        "div.content",
                        "div.article",
                        "div[id*='content']",
                        "div[class*='content']",
                        "article",
                        "main",
                    ]

                    for sel in selectors:
                        elem = await page.query_selector(sel)
                        if elem:
                            text = await elem.inner_text()
                            if len(text.strip()) >= 100:
                                await browser.close()
                                return text[:12000]

                    body_text = await page.inner_text("body")
                    await browser.close()
                    if len(body_text.strip()) >= 100:
                        return body_text[:12000]
                    return None

            return asyncio.run(_extract())
        except Exception as e:
            logger.debug("Playwright extraction failed: %s", e)
            return None

    def _try_trafilatura(self, url):
        try:
            import trafilatura
            downloaded = trafilatura.fetch_url(url)
            if not downloaded:
                return None
            text = trafilatura.extract(downloaded, output_format="txt",
                                       include_links=False, include_images=False)
            if text and len(text.strip()) >= 50:
                return text[:12000]
        except Exception:
            pass
        return None

    def extract_content(self, url: str) -> Optional[str]:
        """提取正文：在线请求 -> trafilatura -> Playwright -> Tavily API -> 本地文件"""
        logger.info("提取正文: %s", url)

        # 1. trafilatura 提取
        text = self._try_trafilatura(url)
        if text and len(text.strip()) >= 100:
            logger.info("trafilatura 提取正文成功: %s 字符", len(text))
            return text[:12000]

        # 2. Playwright 提取
        text = self._try_playwright(url)
        if text and len(text.strip()) >= 100:
            logger.info("Playwright 提取正文成功: %s 字符", len(text))
            return text[:12000]

        # 3. 在线基础提取
        try:
            resp = self._request(url)
            if resp.status_code == 200:
                resp.encoding = resp.apparent_encoding or "utf-8"
                soup = BeautifulSoup(resp.text, "lxml")
                text = self._extract_by_selectors(
                    soup,
                    [
                        ("div", {"class": "view TRS_UEDITOR trs_paper_default trs_web"}),
                        ("div", {"class": "article_content"}),
                        ("div", {"class": "content"}),
                        ("div", {"class": "article"}),
                        ("div", {"class": "text"}),
                        ("div", {"class": "TRS_Editor"}),
                        ("article", {}),
                        ("main", {}),
                    ],
                )
                if text and len(text) >= 100:
                    text = self._ensure_utf8(text)
                    logger.info("在线提取正文成功: %s 字符", len(text))
                    return text[:12000]
            elif resp.status_code == 412:
                logger.warning("在线提取遇到 412 WAF 拦截")
        except Exception as e:
            logger.warning("在线提取异常: %s", e)

        # 4. Tavily Extract API 兜底
        if TAVILY_API_KEY:
            try:
                logger.info("使用 Tavily extract API 提取正文")
                extract_data = self._tavily_extract(url, "advanced")
                if extract_data and extract_data.get("results"):
                    for r in extract_data["results"]:
                        content = r.get("raw_content", r.get("content", ""))
                        if content and len(content) >= 100:
                            content = self._ensure_utf8(content)
                            logger.info("Tavily extract 正文成功: %s 字符", len(content))
                            return content[:12000]
            except Exception as e:
                logger.warning("Tavily extract 正文失败: %s", e)

        # 5. 本地文件兜底
        text = self._extract_from_local(url)
        if text:
            text = self._ensure_utf8(text)
            return text

        return None
