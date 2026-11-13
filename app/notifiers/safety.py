"""通知日志安全工具。

真实通知链路会接触邮箱、SMTP 凭据和机器人 Webhook。异常文本可能携带
这些信息，因此统一在写日志或写入数据库前做脱敏处理。
"""

import re
from typing import Iterable

from app.config import settings


_EMAIL_PATTERN = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_URL_PATTERN = re.compile(r"(?i)(https?://[^\s?#]+)(?:\?[^\s#]*)?(?:#[^\s]*)?")
_SECRET_PATTERN = re.compile(
    r"(?i)\b(password|passwd|secret|token|key|signature|sig)=([^\s&,;]+)"
)


def _known_secrets() -> Iterable[str]:
    """返回当前配置中不能进入日志或数据库错误字段的值。"""
    return (
        settings.smtp_password,
        settings.smtp_user,
        settings.notify_email,
        settings.wechat_webhook_url,
        settings.feishu_webhook_url,
        settings.wecom_bot_secret,
        settings.wecom_corp_secret,
        settings.feishu_app_secret,
    )


def redact_sensitive_text(value: object, extra_secrets: Iterable[str] = ()) -> str:
    """移除常见凭据、邮箱和 URL 查询参数，并限制异常文本长度。"""
    text = str(value or "")
    for secret in (*_known_secrets(), *extra_secrets):
        if secret:
            text = text.replace(str(secret), "[REDACTED]")

    text = _EMAIL_PATTERN.sub("[EMAIL]", text)
    text = _URL_PATTERN.sub(lambda match: f"{match.group(1)}?[REDACTED]", text)
    text = _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = " ".join(text.split())
    return text[:300]


def safe_exception_message(exc: BaseException) -> str:
    """生成可安全写入日志和通知台账的异常摘要。"""
    return f"{type(exc).__name__}: {redact_sensitive_text(exc)}"


__all__ = ["redact_sensitive_text", "safe_exception_message"]
