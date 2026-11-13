"""按渠道名获取适配器实例

长任务在后台完成后需要主动把结果发回原会话，因此必须能只凭渠道名
重新拿到一个可发送的适配器，而不是持有某个事件处理器里的实例。

这里只做"渠道名 -> 适配器"的解析，不含任何业务逻辑。
"""

from typing import Any, Callable, Dict

from app.channels.base import BaseChannel, ChannelType
from app.logger import logger


class ChannelNotSupportedError(Exception):
    """请求了尚未接入的渠道"""


def _build_feishu() -> BaseChannel:
    """延迟导入，避免 channels 包与 feishu 适配器之间形成循环导入。"""
    from app.channels.feishu import FeishuChannel

    return FeishuChannel()


def _build_wecom() -> BaseChannel:
    """延迟导入，避免 channels 包与 wecom 适配器之间形成循环导入。"""
    from app.channels.wecom import WecomChannel

    return WecomChannel()


_FACTORIES: Dict[str, Callable[[], Any]] = {
    ChannelType.FEISHU.value: _build_feishu,
    ChannelType.WECOM.value: _build_wecom,
}


def get_channel(channel: str) -> BaseChannel:
    """按渠道名创建适配器；未接入的渠道明确报错而不是静默丢弃消息。"""
    factory = _FACTORIES.get(str(channel))
    if factory is None:
        raise ChannelNotSupportedError(f"渠道尚未接入主动发送: {channel}")

    try:
        return factory()
    except Exception as exc:
        logger.error(f"创建渠道适配器失败 channel={channel}: {exc}")
        raise


def is_supported(channel: str) -> bool:
    """判断渠道是否已接入主动发送"""
    return str(channel) in _FACTORIES
