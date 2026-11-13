"""企业微信"智能机器人"WebSocket 长连接适配器

与 app/channels/wecom.py 的区别：
- wecom.py 走"自建应用"链路：CorpID + AgentId + 公网回调 + 消息加解密；
- 本模块走"智能机器人 API 模式"：BotID + Secret + WebSocket 长连接，
  无需公网回调 URL，也无需自行加解密。

当前实现范围：
- 建立长连接并鉴权；
- 接收单聊/群聊文本消息；
- 通过白名单校验后接入 PolicyAgent 与会话服务；
- 将分析回复转换为企业微信支持的流式消息。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict, Optional

from app.agent.policy_agent import policy_agent
from app.channels.base import ChannelType, IncomingMessage, OutgoingMessage
from app.config import settings
from app.conversations import ConversationService
from app.logger import logger


def _parse_allowed_users(raw: str) -> set:
    """解析逗号分隔的 userid 白名单；空表示拒绝所有。"""
    return {item.strip() for item in (raw or "").split(",") if item.strip()}


class WecomBotClient:
    """智能机器人长连接客户端（最小闭环）"""

    def __init__(
        self,
        bot_id: Optional[str] = None,
        secret: Optional[str] = None,
        allowed_user_ids: Optional[str] = None,
        agent: Any = None,
        conversations: Optional[ConversationService] = None,
    ) -> None:
        self.bot_id = (bot_id if bot_id is not None else settings.wecom_bot_id) or ""
        self.secret = (secret if secret is not None else settings.wecom_bot_secret) or ""
        raw_allowed = (
            allowed_user_ids
            if allowed_user_ids is not None
            else settings.wecom_bot_allowed_user_ids
        )
        self.allowed_user_ids = _parse_allowed_users(raw_allowed)
        self.agent = agent or policy_agent
        self.conversations = conversations or ConversationService()
        self._client = None

    # ---------- 配置校验 ----------

    def validate_config(self) -> None:
        """启动前校验必需配置，避免连上后才失败。"""
        missing = []
        if not self.bot_id:
            missing.append("WECOM_BOT_ID")
        if not self.secret:
            missing.append("WECOM_BOT_SECRET")
        if missing:
            raise ValueError("企业微信智能机器人配置缺失: " + ", ".join(missing))

    def is_user_allowed(self, user_id: str) -> bool:
        """白名单校验：留空表示拒绝所有人（安全默认）。"""
        if not user_id:
            return False
        return user_id in self.allowed_user_ids

    # ---------- 消息解析 ----------

    @staticmethod
    def parse_text_message(frame: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """从回调帧中提取发送者、会话与文本内容。

        官方结构：
        body.from.userid / body.chatid / body.chattype / body.text.content
        群聊消息文本通常带 "@机器人名 " 前缀，这里统一剥离。
        """
        body = (frame or {}).get("body") or {}
        if body.get("msgtype") != "text":
            return None

        user_id = ((body.get("from") or {}).get("userid") or "").strip()
        content = ((body.get("text") or {}).get("content") or "").strip()
        if not user_id or not content:
            return None

        # 群聊中形如 "@RobotA hello"，去掉首个 @xxx 标记
        if content.startswith("@"):
            parts = content.split(maxsplit=1)
            if len(parts) == 2:
                content = parts[1].strip()
            elif len(parts) == 1:
                content = ""

        if not content:
            return None

        chat_id = (body.get("chatid") or "").strip()
        chat_type = (body.get("chattype") or "single").strip()
        return {
            "user_id": user_id,
            "conversation_id": chat_id or user_id,
            "chat_type": chat_type,
            "message_id": (body.get("msgid") or "").strip() or str(uuid.uuid4())[:16],
            "content": content,
        }

    # ---------- 消息编排 ----------

    def _build_incoming(self, parsed: Dict[str, str]) -> IncomingMessage:
        """把解析结果转成统一消息模型，复用既有会话与去重逻辑。"""
        return IncomingMessage(
            channel=ChannelType.WECOM,
            user_id=parsed["user_id"],
            conversation_id=parsed["conversation_id"],
            message_id=parsed["message_id"],
            content=parsed["content"],
            metadata={
                "source": "wecom_bot",
                "chat_type": parsed.get("chat_type", "single"),
            },
        )

    #: 纯寒暄/无政策语义的短消息，直接回欢迎语，不进入检索链路。
    GREETING_WORDS = {
        "你好", "您好", "hi", "hello", "在吗", "在么", "哈喽",
        "早上好", "中午好", "下午好", "晚上好", "谢谢", "感谢",
        "帮助", "help", "怎么用", "能做什么", "你是谁",
    }

    @classmethod
    def is_greeting(cls, text: str) -> bool:
        """判断是否为纯寒暄消息（避免被当成政策检索关键词）。"""
        normalized = (text or "").strip().lower().rstrip("。！？!?.~ ")
        if not normalized:
            return False
        if normalized in cls.GREETING_WORDS:
            return True
        # 极短且不含政策语义的输入（如单个"嗯""好"）也按寒暄处理
        return len(normalized) <= 3 and normalized in {"嗯", "好", "好的", "ok", "收到"}

    @staticmethod
    def build_welcome_reply() -> str:
        """智能机器人欢迎语，说明能力边界与示例问法。"""
        return (
            "## 政策智能助手\n\n"
            "我可以帮你分析中国各级政府的政策文件。试试这样问：\n\n"
            "- 最近有哪些人工智能相关政策？\n"
            "- 湖北在数字经济方面有什么新政策？\n"
            "- 分析一下武汉最新的人才政策\n"
            "- 生成最近一周的政策报告\n\n"
            "直接发送你的问题即可。"
        )

    def _run_agent(self, incoming: IncomingMessage) -> str:
        """同步执行 Agent：记录输入、取上下文、调用 PolicyAgent、记录回复。

        该函数会被放到线程池执行，避免阻塞 WebSocket 事件循环。
        """
        self.conversations.record_incoming(incoming)
        context = [
            {"role": item["role"], "content": item["content"]}
            for item in self.conversations.get_messages(
                channel=incoming.channel.value,
                conversation_id=incoming.conversation_id,
                user_id=incoming.user_id,
                limit=20,
            )
            if item["message_id"] != incoming.message_id
        ][-10:]

        task_id = "wecom-bot-" + uuid.uuid4().hex[:16]
        result = self.agent.run(
            incoming.content,
            trigger_type="wecom",
            task_id=task_id,
            task_type="user_query",
            context=context,
        )
        answer = result.answer or ""

        reply = OutgoingMessage.for_reply(
            incoming, answer, metadata={"task_id": task_id, "success": result.success}
        )
        try:
            self.conversations.record_reply(
                reply,
                user_id=incoming.user_id,
                message_id=f"agent-{incoming.message_id}",
                meta={"task_id": task_id},
            )
        except Exception as exc:
            logger.warning("智能机器人会话写入失败: %s", exc)

        return answer

    # ---------- 运行 ----------

    async def run(self) -> None:
        """建立长连接并持续接收消息。"""
        self.validate_config()
        try:
            from aibot import WSClient, WSClientOptions
        except ImportError as exc:
            raise RuntimeError(
                "缺少企业微信智能机器人 SDK，请先安装 wecom-aibot-python-sdk"
            ) from exc

        client = WSClient(
            WSClientOptions(
                bot_id=self.bot_id,
                secret=self.secret,
                max_reconnect_attempts=-1,
            )
        )
        self._client = client

        @client.on("authenticated")
        def _on_authenticated() -> None:
            logger.info("企业微信智能机器人长连接鉴权成功")

        @client.on("disconnected")
        def _on_disconnected(reason: Any = None) -> None:
            logger.warning("企业微信智能机器人连接断开: %s", reason)

        @client.on("message.text")
        async def _on_text(frame: Dict[str, Any]) -> None:
            await self._handle_text(client, frame)

        # 图片/语音/文件/图文混排：当前不解析，但必须给用户明确反馈。
        # 静默无响应会让用户以为机器人坏了。
        for _event in ("message.image", "message.voice",
                       "message.file", "message.mixed"):
            @client.on(_event)
            async def _on_unsupported(frame: Dict[str, Any]) -> None:
                await self._handle_unsupported(client, frame)

        logger.info("企业微信智能机器人长连接启动中（bot_id=%s）", self.bot_id[:8])
        await client.connect()
        # connect() 返回后保持运行，由 SDK 内部维持连接与重连
        while True:
            await asyncio.sleep(3600)

    async def _handle_unsupported(self, client: Any, frame: Dict[str, Any]) -> None:
        """对暂不支持的消息类型给出明确提示，避免静默无响应。"""
        body = (frame or {}).get("body") or {}
        msgtype = body.get("msgtype") or "未知"
        user_id = ((body.get("from") or {}).get("userid") or "").strip()

        if user_id and not self.is_user_allowed(user_id):
            logger.warning("智能机器人收到未授权用户消息，已忽略 user_id=%s", user_id)
            return

        logger.info("智能机器人收到不支持的消息类型: %s user_id=%s", msgtype, user_id)
        await self._send_plain(
            client, frame,
            "当前仅支持文字提问。请用文字描述你的问题，"
            "例如「分析湖北最新人工智能政策」。",
        )

    async def _send_plain(self, client: Any, frame: Dict[str, Any], text: str) -> None:
        """发送一条简短提示（流式协议，一次完成）。"""
        try:
            stream_id = str(uuid.uuid4())
            await client.reply_stream(frame, stream_id, text, finish=False)
            await client.reply_stream(frame, stream_id, text, finish=True)
        except Exception as exc:
            logger.warning("智能机器人提示发送失败: %s", exc)

    async def _handle_text(self, client: Any, frame: Dict[str, Any]) -> None:
        """处理一条文本消息：白名单校验 -> 生成回复 -> 流式发送。"""
        parsed = self.parse_text_message(frame)
        if not parsed:
            return

        if not self.is_user_allowed(parsed["user_id"]):
            logger.warning("智能机器人收到未授权用户消息，已忽略 user_id=%s", parsed["user_id"])
            await self._send_plain(
                client, frame,
                "你暂无使用权限。如需开通，请联系管理员把你的企业微信账号加入白名单。",
            )
            return

        logger.info(
            "智能机器人收到消息 user_id=%s conversation=%s len=%s",
            parsed["user_id"], parsed["conversation_id"], len(parsed["content"]),
        )

        incoming = self._build_incoming(parsed)
        stream_id = str(uuid.uuid4())

        # 寒暄消息不进入检索链路：否则"你好"会被当成政策关键词，
        # 返回一堆不相关结果，体验很差。
        if self.is_greeting(parsed["content"]):
            reply_text = self.build_welcome_reply()
        else:
            # Agent 是同步阻塞调用，放到线程池，避免卡住 WebSocket 心跳。
            try:
                reply_text = await asyncio.to_thread(self._run_agent, incoming)
            except Exception as exc:
                logger.exception("智能机器人 Agent 执行失败: %s", exc)
                reply_text = "处理失败，请稍后重试。"
        if not reply_text:
            reply_text = "没有生成回复内容。"

        try:
            await client.reply_stream(frame, stream_id, reply_text, finish=False)
            await client.reply_stream(frame, stream_id, reply_text, finish=True)
        except Exception as exc:
            logger.error("智能机器人回复失败: %s", exc)


def main() -> None:
    """命令行入口：python -m app.channels.wecom_bot"""
    from app.logger import logger as _logger

    try:
        asyncio.run(WecomBotClient().run())
    except KeyboardInterrupt:
        _logger.info("智能机器人长连接已手动停止")
    except Exception as exc:
        _logger.error("智能机器人启动失败: %s", exc)
        raise


if __name__ == "__main__":
    main()
