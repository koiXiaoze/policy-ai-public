"""统一消息抽象层

所有入口（CLI、飞书、企业微信、Scheduler）都先转换为统一消息，
再交给同一个 Policy Agent Core 执行。
"""

from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar, Dict, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ChannelType(str, Enum):
    """系统支持的消息入口类型"""

    CLI = "cli"
    FEISHU = "feishu"
    WECOM = "wecom"
    SCHEDULER = "scheduler"


class TriggerType(str, Enum):
    """Agent 任务触发类型

    该类型会直接传给统一的 Agent 调用入口，用于日志和任务隔离。
    """

    CLI = "cli"
    FEISHU = "feishu"
    WECOM = "wecom"
    SCHEDULED = "scheduled"


_CHANNEL_TRIGGER_MAP: Dict[ChannelType, TriggerType] = {
    ChannelType.CLI: TriggerType.CLI,
    ChannelType.FEISHU: TriggerType.FEISHU,
    ChannelType.WECOM: TriggerType.WECOM,
    ChannelType.SCHEDULER: TriggerType.SCHEDULED,
}


class IncomingMessage(BaseModel):
    """进入系统的标准化消息"""

    model_config = ConfigDict(validate_assignment=True)

    channel: ChannelType
    user_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    timestamp: datetime = Field(default_factory=datetime.now)
    trigger_type: TriggerType
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _derive_trigger_type(cls, data: Any) -> Any:
        """未显式传入触发类型时，根据渠道自动推导"""
        if not isinstance(data, Mapping):
            return data

        normalized = dict(data)
        if normalized.get("trigger_type"):
            return normalized
        if "channel" not in normalized:
            return normalized

        try:
            channel = ChannelType(normalized["channel"])
        except ValueError:
            return normalized

        normalized["trigger_type"] = _CHANNEL_TRIGGER_MAP[channel]
        return normalized

    @field_validator("channel", "user_id", "conversation_id", "message_id", mode="before")
    @classmethod
    def _strip_required_text(cls, value: Any) -> Any:
        """去除平台透传字符串中的首尾空白，避免空ID通过校验"""
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("content", mode="before")
    @classmethod
    def _clean_content(cls, value: str) -> str:
        """先清理首尾空白，再执行非空和长度校验"""
        if not isinstance(value, str):
            return value
        return value.strip()

    @model_validator(mode="after")
    def _set_trigger_type(self) -> "IncomingMessage":
        """强制入口和触发类型一致，避免日志归属混乱"""
        expected_trigger = _CHANNEL_TRIGGER_MAP[self.channel]
        if self.trigger_type != expected_trigger:
            raise ValueError("trigger_type 必须与 channel 保持一致")
        return self

    def to_payload(self) -> Dict[str, Any]:
        """输出JSON安全字典，便于写入数据库、日志或队列"""
        return self.model_dump(mode="json")


class OutgoingMessage(BaseModel):
    """系统准备发送到具体渠道的标准化回复"""

    model_config = ConfigDict(validate_assignment=True)

    channel: ChannelType
    conversation_id: str = Field(min_length=1)
    content: str
    reply_to: Optional[str] = Field(default=None)
    timestamp: datetime = Field(default_factory=datetime.now)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("channel", "conversation_id", "reply_to", mode="before")
    @classmethod
    def _strip_optional_text(cls, value: Any) -> Any:
        """标准化平台相关字符串字段"""
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("content")
    @classmethod
    def _clean_content(cls, value: str) -> str:
        """回复内容允许为空，但去掉首尾空白便于渠道判断"""
        return value.strip()

    @classmethod
    def for_reply(
        cls,
        incoming: IncomingMessage,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "OutgoingMessage":
        """基于原始消息生成回复，确保渠道和会话不串线"""
        return cls(
            channel=incoming.channel,
            conversation_id=incoming.conversation_id,
            content=content,
            reply_to=incoming.message_id,
            metadata=metadata or {},
        )

    def to_payload(self) -> Dict[str, Any]:
        """输出JSON安全字典，便于日志和发送适配器复用"""
        return self.model_dump(mode="json")


class BaseChannel(ABC):
    """渠道适配器基础接口

    子类只负责平台协议转换：
    1. 将平台原始消息转换为 IncomingMessage；
    2. 将 OutgoingMessage 发送到对应平台。
    不允许在子类中实现政策搜索、LLM分析或报告生成逻辑。
    """

    channel_name: ClassVar[ChannelType]

    def parse_incoming(self, payload: Mapping[str, Any]) -> IncomingMessage:
        """将平台原始payload转换为统一消息"""
        normalized_payload = dict(payload)
        normalized_payload["channel"] = self.channel_name
        return IncomingMessage.model_validate(normalized_payload)

    @abstractmethod
    def send(self, message: OutgoingMessage) -> None:
        """将统一回复发送到具体平台"""
        raise NotImplementedError
