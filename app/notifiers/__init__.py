
"""
通知器统一入口
提供 BaseNotifier 基类、通知器注册表和动态加载函数
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional
from app.logger import logger


class BaseNotifier(ABC):
    """通知器抽象基类
    每个推送渠道（邮件/微信/飞书等）实现一个子类
    """

    #: 通知器唯一标识，如 email / wechat / feishu
    name: str = "base"

    def is_enabled(self) -> bool:
        """判断该通知器是否已配置可用
        子类可覆盖此方法，默认返回 True
        """
        return True

    @abstractmethod
    def send(self, report_path: Path) -> bool:
        """发送报告

        Args:
            report_path: 报告文件路径

        Returns:
            是否发送成功
        """
        pass

    @abstractmethod
    def send_content(self, content: str, metadata: Optional[dict] = None) -> bool:
        """发送文本内容

        统一通知服务使用该接口；send() 保持旧报告文件接口兼容。
        """


# 通知器注册表：name -> 类
NOTIFIER_REGISTRY: Dict[str, type] = {}


def register_notifier(cls: type) -> type:
    """装饰器：将通知器类注册到注册表"""
    name = getattr(cls, "name", cls.__name__.lower())
    NOTIFIER_REGISTRY[name] = cls
    return cls


def get_enabled_notifiers() -> List[BaseNotifier]:
    """根据配置动态创建启用的通知器实例"""
    from app.config import settings
    from app.notifiers.email_notifier import EmailNotifier
    from app.notifiers.wechat_notifier import WechatNotifier
    from app.notifiers.feishu_notifier import FeishuNotifier

    configured_channels = {
        item.strip().lower()
        for item in settings.notify_channels.split(",")
        if item.strip()
    }

    # 所有可用通知器候选
    candidates = [EmailNotifier(), WechatNotifier(), FeishuNotifier()]

    enabled = []
    for notifier in candidates:
        try:
            if notifier.name not in configured_channels:
                logger.debug("通知渠道未在 NOTIFY_CHANNELS 中启用，跳过: %s", notifier.name)
            elif notifier.is_enabled():
                enabled.append(notifier)
            else:
                logger.debug("通知器未配置，跳过: %s", notifier.name)
        except Exception as e:
            logger.error("通知器初始化失败: %s - %s", getattr(notifier, "name", "unknown"), e)

    return enabled


__all__ = [
    "BaseNotifier",
    "NOTIFIER_REGISTRY",
    "register_notifier",
    "get_enabled_notifiers",
]

