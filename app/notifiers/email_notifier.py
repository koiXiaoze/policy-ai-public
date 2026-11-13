
"""
邮件推送模块
通过 SMTP 发送邮件报告
"""

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional
from app.config import settings
from app.logger import logger
from app.notifiers import BaseNotifier, register_notifier
from app.notifiers.safety import safe_exception_message


@register_notifier
class EmailNotifier(BaseNotifier):
    """邮件推送通知器"""

    name = "email"

    def is_enabled(self) -> bool:
        """检查 SMTP 配置是否完整"""
        return bool(settings.smtp_server and settings.smtp_user and settings.notify_email)

    def send_content(self, content: str, metadata: Optional[dict] = None) -> bool:
        """发送文本内容

        Args:
            report_path: 报告文件路径

        Returns:
            是否成功
        """
        if not self.is_enabled():
            logger.warning("邮件配置不完整，跳过邮件推送")
            return False

        try:
            metadata = metadata or {}
            task_type = metadata.get("task_type", "")
            chunk_index = metadata.get("chunk_index")
            chunk_total = metadata.get("chunk_total")
            suffix = f" ({chunk_index}/{chunk_total})" if chunk_index and chunk_total else ""
            base_subject = metadata.get("title") or f"政策通知 - {task_type}"
            subject = f"{base_subject}{suffix}"
            # 构建邮件
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = settings.smtp_user
            msg["To"] = settings.notify_email

            # 将 Markdown 转为纯文本（简单处理）
            text_part = MIMEText(content, "plain", "utf-8")
            msg.attach(text_part)

            # 发送
            if settings.smtp_port == 465:
                server = smtplib.SMTP_SSL(settings.smtp_server, settings.smtp_port, timeout=30)
            else:
                server = smtplib.SMTP(settings.smtp_server, settings.smtp_port, timeout=30)
                server.starttls()

            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.smtp_user, [settings.notify_email], msg.as_string())
            server.quit()

            logger.info("邮件推送成功")
            return True

        except Exception as e:
            logger.error("邮件推送失败: %s", safe_exception_message(e))
            return False

    def send(self, report_path: Path) -> bool:
        """通过邮件发送报告文件（旧接口兼容）"""
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.error("读取报告失败: %s", safe_exception_message(e))
            return False
        return self.send_content(content, {"title": f"政策趋势日报 - {report_path.stem}"})


# 向后兼容：保留原函数名
def send_email_report(report_path: Path) -> bool:
    """兼容旧调用方式的封装函数"""
    return EmailNotifier().send(report_path)

