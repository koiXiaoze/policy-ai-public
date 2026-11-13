"""政策正文与链接清洗工具。

本模块是所有采集器共用的数据质量入口。它只做可预测、
可验证的清洗：解码 HTML 实体、统一 Unicode、移除不可见控制字符、
规范化 URL 去重键，并从链接中提取严格、真实且不在未来的日期。

设计原则：
- 不根据上下文调整政策内容，只清理确定性的编码/不可见字符噪声；
- U+FFFD 表示原始字符已经丢失，必须保留并交给人工复核；
- 标题截断（如 ...）不自动补全，避免伪造信息；
- 正文仍需满足最小长度，否则视为提取失败。
"""

from __future__ import annotations

import html
import posixpath
import re
import unicodedata
from datetime import date, datetime
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


NOISE_PATTERNS = (
    r"\u672c\u7ad9PC\u7248[(\uff08]\u7535\u8111\u7248[)\uff09]",
    r"\u8bf7\u786e\u4fdd\u60a8\u7684\u6d4f\u89c8\u5668\u6ca1\u6709\u653e\u5927\u663e\u793a",
    r"\u5f53\u524d\u4e3a\u79fb\u52a8\u7aef\u663e\u793a\u6a21\u5f0f",
    r"\u8bf7\u9002\u5f53\u7f29\u653e\u6d4f\u89c8\u5668\u663e\u793a\u5927\u5c0f",
    r"\u5efa\u8bae\u4f7f\u7528IE\d+",
    r"\u8bf7\u4f7f\u75281024\*768\u4ee5\u4e0a\u5206\u8fa8\u7387",
)

CONTENT_START_MARKERS = (
    r"\*\*\u5206\s*\u7c7b[\uff1a:]",
    r"\*\*\u53d1\u5e03\u673a\u6784[\uff1a:]",
    r"\*\*\u540d\s*\u79f0[\uff1a:]",
    r"\u6587\s*\u53f7[\uff1a:]",
    r"^#\s",
    r"^\u5173\u4e8e",
    r"^[\u4e00-\u9fa5]{1,8}\u653f\u53d1",
    r"^[\u4e00-\u9fa5]{1,8}\u653f\u529e\u53d1",
)

MIN_CONTENT_LENGTH = 100
_INVISIBLE_RE = re.compile(
    r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f"
    r"\u200b-\u200f\u202a-\u202e\u2060\ufeff]"
)
_TITLE_SUFFIX_RE = re.compile(
    r"\s*[-_|]\s*(?:\u6e56\u5317\u7701\u4eba\u6c11\u653f\u5e9c|"
    r"\u6b66\u6c49\u5e02\u4eba\u6c11\u653f\u5e9c|\u5341\u5830\u5e02\u4eba\u6c11\u653f\u5e9c|"
    r"\u4e2d\u56fd\u653f\u5e9c\u7f51)\s*$"
)
_TRACKING_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "source", "share_token", "share_source", "scene",
}
_URL_DATE_RE = re.compile(r"/t(\d{4})(\d{2})(\d{2})(?=[_.\-/]|$)", re.IGNORECASE)


def normalize_text(text: Optional[str]) -> Optional[str]:
    """清洗一般文本，保留换行与 U+FFFD。"""
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return None

    cleaned = html.unescape(text)
    cleaned = unicodedata.normalize("NFC", cleaned)
    cleaned = _INVISIBLE_RE.sub("", cleaned)
    cleaned = cleaned.replace("\\r\\n", "\\n").replace("\\r", "\\n")
    cleaned = re.sub(r"[^\S\n]+", " ", cleaned)
    cleaned = "\n".join(line.strip() for line in cleaned.split("\n"))
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned or None


def clean_title(title: Optional[str]) -> Optional[str]:
    """清洗标题，仅删除明确的政府网站后缀。"""
    cleaned = normalize_text(title)
    if not cleaned:
        return None
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = _TITLE_SUFFIX_RE.sub("", cleaned).strip()
    return cleaned or None


def normalize_url_for_dedupe(url: Optional[str]) -> str:
    """生成去重键；原始 URL 仍由入库层保留。"""
    if not url or not isinstance(url, str):
        return ""
    value = html.unescape(url).strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return ""
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port and not ((parsed.scheme.lower() == "http" and port == 80)
                     or (parsed.scheme.lower() == "https" and port == 443)):
        hostname = f"{hostname}:{port}"

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = posixpath.normpath(path)
        if parsed.path.endswith("/") and not path.endswith("/"):
            path += "/"
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_QUERY_KEYS
        and not key.lower().startswith("utm_")
    ]
    query.sort()
    return urlunsplit(("https", hostname, path or "/", urlencode(query), ""))


def extract_date_from_url(
    url: Optional[str], *, today: Optional[date] = None
) -> Optional[datetime]:
    """从 /tYYYYMMDD_ 严格提取真实且不在未来的日期。"""
    if not url:
        return None
    match = _URL_DATE_RE.search(str(url))
    if not match:
        return None
    try:
        value = datetime(
            int(match.group(1)), int(match.group(2)), int(match.group(3))
        )
    except ValueError:
        return None
    current = today or date.today()
    if isinstance(current, datetime):
        current = current.date()
    return value if value.date() <= current else None


def strip_site_noise(text: Optional[str]) -> Optional[str]:
    """剔离正文开头的站点提示，清洗后过短则返回 None。"""
    cleaned = normalize_text(text)
    if not cleaned:
        return None

    head = cleaned[:600]
    has_noise = any(re.search(pattern, head) for pattern in NOISE_PATTERNS)
    if has_noise:
        best_index = None
        for marker in CONTENT_START_MARKERS:
            match = re.search(marker, cleaned[:1500], flags=re.MULTILINE)
            if match and (best_index is None or match.start() < best_index):
                best_index = match.start()
        if best_index is not None and best_index > 0:
            cleaned = cleaned[best_index:].strip()

    return cleaned if len(cleaned) >= MIN_CONTENT_LENGTH else None
