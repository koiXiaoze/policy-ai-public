"""武汉市人民政府政策采集器

武汉门户主站列表页是 JS 渲染（静态 HTML 无政策链接），Tavily Extract 抓到的是
新闻栏目。实测可用路径：

1. 发现：Tavily Search（site:wuhan.gov.cn + 政策关键词）能稳定返回
   /zwgk/xxgk/zfwj/ 下的真实政策文件页。
2. 正文：www.wuhan.gov.cn 的政策详情页可直连抓取（HTTP 200，正文完整），
   因此正文不走 Tavily，减少 API 消耗。

不修改现有采集器，为武汉单独实现。
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from app.logger import logger
from app.scrapers.base import BaseScraper, ScrapedItem
from app.scrapers.tavily_client import TavilyErrorType, create_tavily_client


TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
TAVILY_API_BASE = "https://api.tavily.com"

# 只接受武汉市政策文件栏目，避免把新闻/图解/公示当成政策
POLICY_PATH_PATTERNS = [
    r"/zwgk/xxgk/zfwj/",       # 市政府文件（规范性文件/部门文件/政府文件）
    r"/gfxwj/",                # 规范性文件
]

# 标题必须命中的政策特征词，进一步排除新闻和会议报道
POLICY_TITLE_KEYWORDS = [
    "通知", "意见", "办法", "规定", "决定", "方案", "规划",
    "条例", "细则", "公告", "政策措施", "实施细则", "行动计划",
    "实施意见", "管理办法",
]


class WuhanScraper(BaseScraper):
    """武汉市政策采集器：Tavily 发现 + 直连抓正文"""

    # Scrapling 直连时优先访问实际含政策链接的子栏目。
    # 主入口 /zfwj/ 的列表由脚本二次加载，实测没有可直接解析的政策链接。
    SCRAPLING_LIST_URLS = [
        "https://www.wuhan.gov.cn/zwgk/xxgk/zfwj/gfxwj/",
    ]

    SEARCH_QUERIES = [
        # 按政策文件库的三个子栏目分别检索，命中率与近期覆盖率最高
        # （实测 5 个查询共发现 69 条政策页，最新到 2026-10-16）
        "site:wuhan.gov.cn/zwgk/xxgk/zfwj/gfxwj 通知",   # 规范性文件
        "site:wuhan.gov.cn/zwgk/xxgk/zfwj/bgtwj 通知",   # 办公厅文件
        "site:wuhan.gov.cn/zwgk/xxgk/zfwj/szfwj 通知",   # 市政府文件
        "site:wuhan.gov.cn/zwgk/xxgk/zfwj 2026 印发",
        "site:wuhan.gov.cn/zwgk/xxgk/zfwj 2026 意见 方案",
    ]

    def __init__(self, source_config: dict):
        super().__init__(source_config)
        self.days_back: Optional[int] = source_config.get("days_back")
        self._tavily_live_success = False
        self._tavily_error: Optional[str] = None
        self._tavily_error_type: Optional[str] = None
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

    # ---------- Tavily ----------

    def _tavily_search(self, query: str, max_results: int = 20) -> Optional[dict]:
        client = create_tavily_client(TAVILY_API_KEY, timeout=90, max_retries=3, logger=logger)
        result = client.call_with_details(
            "/search",
            {
                "query": query,
                "max_results": max_results,
                "search_depth": "advanced",
            },
            timeout=90,
            max_retries=3,
        )
        self._tavily_error_type = result.error_type if not result.ok else None
        return result.data

    # ---------- 发现 ----------

    @staticmethod
    def _is_policy_url(url: str) -> bool:
        """只接受政策文件页（.shtml）。

        排除 Tavily 返回的 PDF 附件（如 /202003/P0201910226...pdf）：
        这类文件是历史政策的附件，路径日期与真实发布日期不符，
        且没有可靠发布日期，混入后会污染日报。
        """
        if not url:
            return False
        if not re.search(r"/t\d{8}_\d+\.s?html?$", url):
            return False
        return any(re.search(p, url) for p in POLICY_PATH_PATTERNS)

    @staticmethod
    def _is_policy_title(title: str) -> bool:
        if not title:
            return False
        return any(kw in title for kw in POLICY_TITLE_KEYWORDS)

    @staticmethod
    def _extract_publish_time(url: str) -> Optional[datetime]:
        # 优先从 /tYYYYMMDD_ 提取精确日期
        m = re.search(r"/t(\d{4})(\d{2})(\d{2})_", url)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
        return None

    @staticmethod
    def _clean_title(title: str) -> str:
        # Tavily 常返回 "标题 - 武汉市人民政府" / "标题_武汉市人民政府" 形式
        return re.split(r"\s*[-_|]\s*武汉市?人民政府.*$", title.strip())[0].strip()

    def _fetch_via_tavily(self) -> List[ScrapedItem]:
        self._tavily_live_success = False
        self._tavily_error = None
        self._tavily_error_type = None
        if not TAVILY_API_KEY:
            self._tavily_error = "Tavily API Key 未配置"
            self._tavily_error_type = TavilyErrorType.AUTH
            logger.warning("武汉采集: Tavily Key 未配置，无法发现政策列表")
            return []

        cutoff = None
        if self.days_back is not None:
            cutoff = datetime.now().date() - timedelta(days=self.days_back)

        items: List[ScrapedItem] = []
        seen = set()

        for query in self.SEARCH_QUERIES:
            data = self._tavily_search(query, 20)
            if data is None:
                self._tavily_error = "Tavily 搜索请求失败"
            else:
                # 空 results 是正常在线响应，代表近期无匹配政策。
                self._tavily_live_success = True
            results = (data or {}).get("results", []) or []
            for row in results:
                url = (row.get("url") or "").split("#", 1)[0]
                title = self._clean_title(row.get("title") or "")
                if not url or not title or url in seen:
                    continue
                if url.startswith("http://"):
                    url = "https://" + url[len("http://"):]
                if not self._is_policy_url(url):
                    continue
                if not self._is_policy_title(title) or len(title) < 8:
                    continue

                publish_time = self._extract_publish_time(url)
                if not publish_time:
                    # 兜底：从 Tavily 返回内容里抓日期
                    m = re.search(r"(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})",
                                  row.get("content") or "")
                    if m:
                        try:
                            publish_time = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                        except ValueError:
                            publish_time = None

                if cutoff and publish_time and publish_time.date() < cutoff:
                    continue

                seen.add(url)
                items.append(ScrapedItem(
                    title=title,
                    url=url,
                    source_name=self.name,
                    government_level=self.level,
                    department=self.config.get("department", "武汉市人民政府"),
                    city=self.city or "武汉",
                    publish_time=publish_time,
                ))

        items.sort(key=lambda i: i.publish_time or datetime.min, reverse=True)
        logger.info("武汉 Tavily 发现政策: %s 条", len(items))
        return items

    def _parse_list_html(self, html: str, base_url: str) -> List[ScrapedItem]:
        """解析武汉政策列表页，复用 Tavily 通道的筛选和日期规则。"""
        if not html:
            return []

        cutoff = None
        if self.days_back is not None:
            cutoff = datetime.now().date() - timedelta(days=self.days_back)

        soup = BeautifulSoup(html, "lxml")
        items: List[ScrapedItem] = []
        seen = set()
        for link in soup.find_all("a", href=True):
            title = self._clean_title(" ".join(link.get_text(" ", strip=True).split()))
            url = urljoin(base_url, link["href"].strip())
            if url.startswith("http://"):
                url = "https://" + url[len("http://"):]
            if not title or len(title) < 8 or url in seen:
                continue
            if not self._is_policy_url(url) or not self._is_policy_title(title):
                continue

            publish_time = self._extract_publish_time(url)
            if cutoff and publish_time and publish_time.date() < cutoff:
                continue

            seen.add(url)
            items.append(ScrapedItem(
                title=title,
                url=url,
                source_name=self.name,
                government_level=self.level,
                department=self.config.get("department", "武汉市人民政府"),
                city=self.city or "武汉",
                publish_time=publish_time,
            ))

        items.sort(key=lambda i: i.publish_time or datetime.min, reverse=True)
        return items

    def _fetch_via_scrapling(self) -> List[ScrapedItem]:
        """Tavily 请求失败后，直接从武汉官网抓取政策列表。"""
        try:
            from app.scrapers.scrapling_fetcher import ScraplingFetcher

            all_items: List[ScrapedItem] = []
            seen = set()
            for page_url in self.SCRAPLING_LIST_URLS:
                html = ScraplingFetcher.fetch_html(
                    page_url, mode="stealth", retries=1
                )
                for item in self._parse_list_html(html or "", page_url):
                    if item.url in seen:
                        continue
                    seen.add(item.url)
                    all_items.append(item)

            all_items.sort(
                key=lambda i: i.publish_time or datetime.min, reverse=True
            )
            logger.info("武汉 Scrapling 发现政策: %s 条", len(all_items))
            return all_items
        except Exception as exc:
            logger.warning("武汉 Scrapling 通道失败: %s", exc)
            return []

    def fetch(self) -> List[ScrapedItem]:
        logger.info("开始采集: %s", self.name)
        items = self._fetch_via_tavily()
        if items:
            self._mark_live_success()
            return items

        if not TAVILY_API_KEY and self._tavily_error is None:
            self._tavily_error = "Tavily API Key 未配置"

        if getattr(self, "_tavily_live_success", False):
            # Tavily 正常响应但没有匹配政策，属于“无更新”，不能误判为故障。
            self._mark_empty()
            return []

        tavily_reason = getattr(self, "_tavily_error", None) or "Tavily 不可用"

        # 第二通道：直接抓取官网；只有 Tavily 请求失败才启用。
        logger.warning("武汉 Tavily 抓取失败，尝试 Scrapling 直连: %s", tavily_reason)
        items = self._fetch_via_scrapling()
        if items:
            self._mark_live_success()
            return items

        # 最终降级：所有实时通道失败后，才使用库内旧数据。
        reason = f"Tavily: {tavily_reason}; Scrapling: 未取得数据"
        logger.warning("武汉实时通道均失败，回退到库内已有政策: %s", reason)
        items = self._fetch_from_database()
        if items:
            self._mark_degraded(f"在线通道失败，使用数据库旧数据: {reason}", self._tavily_error_type)
            return items

        self._mark_failed(f"在线通道失败且数据库无可用旧数据: {reason}", self._tavily_error_type)
        return []

    def _fetch_from_database(self) -> List[ScrapedItem]:
        """从本地库回填武汉政策，作为 Tavily 不可用时的降级来源。"""
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
                logger.info("库内回退获取武汉政策: %s 条", len(rows))
                return [
                    ScrapedItem(
                        title=p.title,
                        url=p.url,
                        source_name=p.source_name,
                        government_level=p.government_level,
                        department=p.department,
                        city=p.city,
                        publish_time=p.publish_time,
                        content=p.content,
                    )
                    for p in rows
                ]
            finally:
                db.close()
        except Exception as exc:
            logger.warning("武汉库内回退失败: %s", exc)
            return []

    # ---------- 正文 ----------

    def extract_content(self, url: str) -> Optional[str]:
        """直连抓取武汉政策详情页正文。"""
        try:
            resp = requests.get(url, headers=self.headers, timeout=30)
            if resp.status_code != 200:
                logger.warning("武汉正文抓取状态码 %s: %s", resp.status_code, url[:80])
                return None
            resp.encoding = resp.apparent_encoding or "utf-8"
            html = resp.text
            # 主站软 404 会把任意错误 URL 返回为首页（固定体积），需识别
            if "武汉市人民政府门户网站" in html and "政策" not in html[:2000] and len(html) > 190000:
                logger.debug("武汉正文疑似回退首页，跳过: %s", url[:80])
                return None

            soup = BeautifulSoup(html, "lxml")
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()

            for selector in (
                "div.content", "div#content", "div.TRS_Editor",
                "div.article", "div.detail", "div.view", "div.zoom",
                "div.xxgk_content", "div.con",
            ):
                node = soup.select_one(selector)
                if node:
                    text = node.get_text("\n", strip=True)
                    if len(text) >= 80:
                        return text[:10000]

            text = soup.get_text("\n", strip=True)
            return text[:10000] if len(text) >= 80 else None
        except Exception as exc:
            logger.warning("武汉正文抓取异常: %s - %s", url[:80], exc)
            return None
