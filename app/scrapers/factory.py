"""
采集器工厂
根据配置动态创建对应的采集器实例
"""

from typing import Dict
from app.scrapers.base import BaseScraper
from app.scrapers.gov_scraper import GovCnScraper
from app.scrapers.hubei_scraper import HubeiScraper
from app.scrapers.shiyan_scraper import ShiyanScraper
from app.scrapers.mohrss_scraper import MohrssScraper
from app.scrapers.wuhan_scraper import WuhanScraper
from app.logger import logger


SCRAPER_MAP: Dict[str, type] = {
    "gov_cn": GovCnScraper,
    "provincial": HubeiScraper,
    "city": GovCnScraper,
    "shiyan": ShiyanScraper,
    "mohrss": MohrssScraper,
    "wuhan": WuhanScraper,
}


def create_scraper(source_config: dict) -> BaseScraper:
    scraper_type = source_config.get("scraper_type", "")
    scraper_class = SCRAPER_MAP.get(scraper_type)
    if not scraper_class:
        raise ValueError(f"未知的采集器类型: {scraper_type}")
    logger.info("创建采集器: %s (%s)", source_config.get("name"), scraper_type)
    return scraper_class(source_config)


def run_all_scrapers(
    days_back: int = None,
    *,
    suppress_health_alerts: bool = False,
) -> list:
    from app.config import get_enabled_sources
    sources = get_enabled_sources()
    if not sources:
        logger.warning("没有启用的政府来源，请检查 config.yaml")
        return []
    all_items = []
    for source in sources:
        name = source.get("name", "")
        scraper = None
        try:
            scraper = create_scraper(source)
            items = scraper.fetch()
            all_items.extend(items)
            # 记录来源健康状态：0 条记为 empty（来源可能只是近期无更新），
            # 异常记为 failed 并累计连续失败次数。
            newest = None
            dates = [i.publish_time for i in items if i.publish_time]
            if dates:
                newest = max(dates)
            fetch_status = getattr(scraper, "last_fetch_status", None)
            # unknown 只表示采集器未声明状态，交给健康服务按旧规则推断。
            if fetch_status == "unknown":
                fetch_status = None
            _record_health(
                name,
                len(items),
                newest_publish_time=newest,
                status=fetch_status,
                error=getattr(scraper, "last_fetch_error", None),
                error_type=getattr(scraper, "last_fetch_error_type", None),
                suppress_alert=suppress_health_alerts,
            )
        except Exception as e:
            logger.error("运行采集器失败: %s - %s", name, e)
            _record_health(
                name,
                0,
                error=str(e),
                status="failed",
                error_type=getattr(scraper, "last_fetch_error_type", None),
                suppress_alert=suppress_health_alerts,
            )
    logger.info("所有采集器完成，共 %s 条", len(all_items))
    return all_items


def _record_health(
    source_name,
    item_count,
    newest_publish_time=None,
    error=None,
    status=None,
    error_type=None,
    suppress_alert=False,
):
    """写入来源健康记录；健康模块故障不得影响采集。"""
    try:
        from app.services.source_health import source_health_service
        source_health_service.record(
            source_name=source_name,
            item_count=item_count,
            newest_publish_time=newest_publish_time,
            error=error,
            status=status,
            error_type=error_type,
            suppress_alert=suppress_alert,
        )
    except Exception as exc:
        logger.warning("记录来源健康失败 %s: %s", source_name, exc)
