"""飞书群机器人推送"""

from pathlib import Path
from typing import Optional

import httpx

from app.config import settings
from app.logger import logger
from app.notifiers import BaseNotifier, register_notifier
from app.notifiers.safety import redact_sensitive_text, safe_exception_message


@register_notifier
class FeishuNotifier(BaseNotifier):
    """飞书群机器人通知器"""

    name = "feishu"

    def is_enabled(self) -> bool:
        """检查 Webhook 是否配置"""
        return bool(settings.feishu_webhook_url)

    def send_content(self, content: str, metadata: Optional[dict] = None) -> bool:
        """发送文本内容到飞书群"""
        if not self.is_enabled():
            logger.warning("飞书 Webhook 未配置，跳过推送")
            return False

        try:
            payload = {
                "msg_type": "text",
                "content": {"text": content},
            }
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(settings.feishu_webhook_url, json=payload)
                resp.raise_for_status()
                result = resp.json()

            code = result.get("code", result.get("StatusCode", 0))
            if code == 0:
                logger.info("飞书推送成功")
                return True

            logger.error("飞书推送失败: %s", redact_sensitive_text(result))
            return False
        except Exception as exc:
            logger.error("飞书推送异常: %s", safe_exception_message(exc))
            return False

    def send(self, report_path: Path) -> bool:
        """兼容旧报告文件接口"""
        try:
            content = report_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.error("读取报告失败: %s", safe_exception_message(exc))
            return False
        return self.send_content(content, {"title": f"政策趋势日报 - {report_path.stem}"})
