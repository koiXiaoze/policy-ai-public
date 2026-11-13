"""
采集器基类
定义所有采集器的公共接口和通用方法
"""

from abc import ABC, abstractmethod
from typing import List, Optional
from datetime import datetime
from dataclasses import dataclass

from app.logger import logger


@dataclass
class ScrapedItem:
    """采集到的一条政策数据"""
    title: str
    url: str
    department: Optional[str] = None
    source_name: Optional[str] = None
    government_level: Optional[str] = None
    city: Optional[str] = None
    publish_time: Optional[datetime] = None
    content: Optional[str] = None
    summary: Optional[str] = None
    keywords: Optional[str] = None


class BaseScraper(ABC):
    """采集器抽象基类
    每个政府来源实现一个子类，覆盖 fetch() 方法即可。
    """

    def __init__(self, source_config: dict):
        self.name = source_config.get("name", "")
        self.website = source_config.get("website", "")
        self.level = source_config.get("level", "")
        self.city = source_config.get("city", "")
        self.config = source_config
        # fetch() 继续返回列表以兼容既有调用方；工厂读取这两个字段，
        # 区分“在线成功但无数据”和“在线失败后用旧数据回填”。
        self.last_fetch_status = "unknown"
        self.last_fetch_error: Optional[str] = None
        self.last_fetch_error_type: Optional[str] = None

    def _mark_live_success(self) -> None:
        """在线通道成功且取得数据。"""
        self.last_fetch_status = "ok"
        self.last_fetch_error = None
        self.last_fetch_error_type = None

    def _mark_empty(self) -> None:
        """在线通道成功，但本次确实没有匹配数据。"""
        self.last_fetch_status = "empty"
        self.last_fetch_error = None
        self.last_fetch_error_type = None

    def _mark_degraded(self, reason: str, error_type: Optional[str] = None) -> None:
        """在线通道不可用时使用了本地文件或数据库旧数据。"""
        self.last_fetch_status = "degraded"
        self.last_fetch_error = reason or "在线通道不可用，使用旧数据"
        self.last_fetch_error_type = error_type

    def _mark_failed(self, error: object, error_type: Optional[str] = None) -> None:
        """所有可用采集路径均失败。"""
        self.last_fetch_status = "failed"
        self.last_fetch_error = str(error) if error else "采集失败"
        self.last_fetch_error_type = error_type

    @abstractmethod
    def fetch(self) -> List[ScrapedItem]:
        """采集政策列表，返回 ScrapedItem 列表
        子类必须实现此方法。
        """
        pass

    def extract_content(self, url: str) -> Optional[str]:
        """提取政策正文内容
        默认使用 trafilatura 库提取网页正文。
        子类可覆盖此方法实现自定义提取逻辑。
        """
        import trafilatura
        try:
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                return trafilatura.extract(
                    downloaded,
                    output_format="text",
                    include_links=False,
                    include_images=False,
                )
        except Exception as e:
            logger.warning(f"正文提取失败: {url} - {e}")
        return None
