"""
国务院网站采集器 - 支持日期范围筛选
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import List, Optional

import requests

from app.scrapers.base import BaseScraper, ScrapedItem
from app.logger import logger


class GovCnScraper(BaseScraper):
    def __init__(self, source_config: dict):
        super().__init__(source_config)
        self.data_url = source_config.get("data_url", "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json")
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json,text/html",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        self.days_back: Optional[int] = source_config.get("days_back")
        self._online_error: Optional[str] = None

    def fetch(self) -> List[ScrapedItem]:
        items = self._fetch_from_json()
        if items:
            logger.info("JSON 获取 %s 条（日期筛选后）", len(items))
            self._mark_live_success()
            return items

        if self._online_error is None:
            # 请求成功但日期筛选后无记录，不能当作网络故障，也不应回填旧数据。
            logger.info("JSON 在线请求成功，但日期筛选后无记录")
            self._mark_empty()
            return []

        items = self._fetch_from_local_json()
        if items:
            logger.info("本地 JSON 获取 %s 条（日期筛选后）", len(items))
            self._mark_degraded(f"在线 JSON 请求失败，使用本地数据: {self._online_error}")
            return items

        self._mark_failed(self._online_error)
        return items

    def _fetch_from_json(self) -> List[ScrapedItem]:
        try:
            resp = requests.get(self.data_url, headers=self.headers, timeout=30)
            resp.encoding = "utf-8"
            resp.raise_for_status()
            items = self._parse_json_data(resp.json())
            self._online_error = None
            return items
        except Exception as e:
            logger.warning("JSON API 请求失败: %s", e)
            self._online_error = str(e) or e.__class__.__name__
            return []

    def _fetch_from_local_json(self) -> List[ScrapedItem]:
        data_dir = Path("data")
        for f in sorted(data_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                items = self._parse_json_data(data)
                if items:
                    logger.info("从本地 JSON 解析: %s", f.name)
                    return items
            except Exception as e:
                logger.debug("JSON 解析失败 %s: %s", f.name, e)
        return []

    def _parse_json_data(self, data: list) -> List[ScrapedItem]:
        items = []
        seen = set()

        # 使用日期（不含时间）作为截止点
        cutoff_date = None
        if self.days_back is not None:
            cutoff_date = date.today() - timedelta(days=self.days_back)
            logger.info("日期筛选: 最近 %s 天（>= %s）", self.days_back, cutoff_date)

        for entry in data:
            url = (entry.get("URL") or "").strip()
            title = (entry.get("TITLE") or "").strip()
            pub_str = (entry.get("DOCRELPUBTIME") or "").strip()

            if not url or not title:
                continue
            if not url.startswith("http"):
                url = "https://www.gov.cn" + url if url.startswith("/") else "https://www.gov.cn/" + url
            if url in seen:
                continue
            seen.add(url)

            # 日期筛选（使用 date 比较，不含时间）
            if cutoff_date and pub_str:
                try:
                    pub_date = datetime.strptime(pub_str[:10], "%Y-%m-%d").date()
                    if pub_date < cutoff_date:
                        continue
                except Exception:
                    continue

            publish_time = None
            if pub_str:
                try:
                    publish_time = datetime.strptime(pub_str[:10], "%Y-%m-%d")
                except Exception:
                    pass

            items.append(ScrapedItem(
                title=title, url=url,
                source_name=self.name,
                government_level=self.level,
                department="国务院",
                publish_time=publish_time,
            ))

        return items

    def extract_content(self, url: str) -> Optional[str]:
        """提取政策正文内容 - 优先使用 Playwright 渲染 JS"""
        # 先尝试 trafilatura
        text = self._try_trafilatura(url)
        if text and len(text.strip()) >= 100:
            return text

        # 尝试 Playwright 渲染 JavaScript（gov.cn 内容由 JS 动态加载）
        text = self._try_playwright(url)
        if text and len(text.strip()) >= 100:
            return text

        # 回退到 requests + BeautifulSoup
        try:
            resp = requests.get(url, headers=self.headers, timeout=30, allow_redirects=True)
            resp.encoding = resp.apparent_encoding or "utf-8"
            resp.raise_for_status()
        except Exception as e:
            logger.warning("requests 请求失败: %s", e)
            return None

        from bs4 import BeautifulSoup
        import re
        soup = BeautifulSoup(resp.text, "lxml")

        pipe = chr(124)
        selectors = [
            ("div", {"class_": "pages_content"}), ("div", {"class_": "article_content"}),
            ("div", {"class_": "content"}), ("div", {"id": "UCAP-CONTENT"}),
            ("div", {"id": re.compile(r"content" + pipe + "zoom" + pipe + "zoomcon", re.I)}),
            ("article", {}), ("main", {}),
            ("div", {"class_": "article"}), ("div", {"class_": "text"}),
            ("div", {"class_": "TRS_Editor"}), ("div", {"class_": "Custom_UnionStyle"}),
        ]
        main = None
        for tag, attrs in selectors:
            main = soup.find(tag, **attrs) if attrs else soup.find(tag)
            if main and main.get_text(strip=True):
                break

        if main:
            text = main.get_text(separator="\n", strip=True)
            if len(text) >= 50:
                return text[:12000]

        body = soup.find("body")
        if body:
            text = body.get_text(separator="\n", strip=True)
            if len(text) >= 50:
                return text[:12000]
        return None

    def _try_playwright(self, url: str) -> Optional[str]:
        """使用 Playwright 渲染 JavaScript 并提取内容"""
        try:
            import asyncio
            from playwright.async_api import async_playwright

            async def _extract():
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True)
                    page = await browser.new_page()
                    await page.goto(url, wait_until="networkidle", timeout=30000)

                    # 尝试多种选择器（gov.cn TRS CMS 常用结构）
                    selectors = [
                        "div.pages_content",
                        "div.article_content",
                        "div.content",
                        "#UCAP-CONTENT",
                        "div[id*='content']",
                        "div[class*='content']",
                        "article",
                        "main",
                        "div.TRS_Editor",
                        "div.Custom_UnionStyle",
                    ]

                    for sel in selectors:
                        elem = await page.query_selector(sel)
                        if elem:
                            text = await elem.inner_text()
                            if len(text.strip()) >= 100:
                                await browser.close()
                                return text[:12000]

                    # 如果所有选择器都失败，尝试获取 body 文本
                    body_text = await page.inner_text("body")
                    await browser.close()
                    if len(body_text.strip()) >= 100:
                        return body_text[:12000]
                    return None

            return asyncio.run(_extract())
        except Exception as e:
            logger.debug("Playwright 提取失败: %s", e)
            return None

    def _try_trafilatura(self, url: str) -> Optional[str]:
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
