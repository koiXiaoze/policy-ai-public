"""来源传输层与网页结构诊断服务。

与 ``source_health`` 分离：source_health 判断“本轮是否取得业务数据”，
本服务判断 DNS/TCP/HTTP/TLS/WAF/DOM 本身是否仍可用。诊断结果会落库，
供 CLI、调度任务和运维复盘使用。
"""

from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import json
import socket
import ssl
import time
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from cryptography import x509
import httpx
from sqlalchemy.orm import Session

from app.config import get_enabled_sources
from app.database import SessionLocal
from app.logger import logger
from app.models.source_diagnostic import SourceDiagnostic
from app.notifiers.safety import redact_sensitive_text


DIAGNOSTIC_ALERT_THRESHOLD = 3
DEFAULT_TLS_WARNING_DAYS = 21
DEFAULT_TIMEOUT_SECONDS = 20.0
VALID_STATUSES = {
    "ok",
    "waf_challenge",
    "tls_expiring",
    "tls_invalid",
    "dom_changed",
    "schema_changed",
    "unconfigured",
    "not_checked",
    "timeout",
    "dns_error",
    "connection_error",
    "http_error",
}
ANOMALY_STATUSES = VALID_STATUSES - {"ok", "unconfigured"}
WAF_HTTP_STATUSES = {403, 412, 429}
WAF_BODY_MARKERS = (
    "$_ss",
    "eo_bot_ssid",
    "tencentedgeone",
    "人机验证",
    "访问验证",
    "安全验证",
    "验证码",
    "captcha",
    "cf-challenge",
)


def _utcnow_naive() -> datetime:
    """数据库统一保存无时区 UTC，避免 SQLite 与 PostgreSQL 语义漂移。"""

    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_url(url: str) -> str:
    """删除 URL 中可能存在的用户名和密码。"""

    try:
        parts = urlsplit(url)
        if parts.username or parts.password:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            return parts._replace(netloc=host).geturl()
    except ValueError:
        pass
    return url


def _certificate_hostname_valid(cert: x509.Certificate, hostname: str) -> bool:
    """校验证书 SAN 是否覆盖主机名，不把“证书可信”误当成“主机名匹配”。"""

    host = hostname.rstrip(".")
    san_values: list[tuple[str, str]] = []
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        san_values.extend(("DNS", value) for value in san.get_values_for_type(x509.DNSName))
        san_values.extend(("IP Address", str(value)) for value in san.get_values_for_type(x509.IPAddress))
    except x509.ExtensionNotFound:
        return False

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None

    for kind, value in san_values:
        if kind == "IP Address" and address is not None:
            if ipaddress.ip_address(value) == address:
                return True
        elif kind == "DNS" and address is None:
            pattern = value.lower().rstrip(".")
            candidate = host.lower()
            if pattern == candidate:
                return True
            # 通配符只覆盖一级子域，与 RFC 6125/浏览器行为保持一致。
            if pattern.startswith("*.") and candidate.count(".") == pattern.count("."):
                if candidate.endswith(pattern[1:]):
                    return True
    return False


def _inspect_tls(url: str, timeout_seconds: float, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """检查 TLS 证书有效期、主机名和信任链。"""

    parts = urlsplit(url)
    hostname = parts.hostname
    if parts.scheme.lower() != "https" or not hostname:
        return {
            "applicable": False,
            "not_after": None,
            "days_remaining": None,
            "hostname_valid": None,
            "valid": True,
            "error": None,
        }

    port = parts.port or 443
    current = now or _utcnow_naive()
    if current.tzinfo is not None:
        current = current.astimezone(timezone.utc).replace(tzinfo=None)

    # 第一段连接不校验证书，用于即使信任链失败也能读取证书到期时间和 SAN。
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((hostname, port), timeout=timeout_seconds) as raw:
            with context.wrap_socket(raw, server_hostname=hostname) as tls_socket:
                der = tls_socket.getpeercert(binary_form=True)
    except socket.gaierror:
        raise
    except (TimeoutError, socket.timeout) as exc:
        raise TimeoutError(f"TLS 握手超时: {exc}") from exc
    except OSError as exc:
        raise ConnectionError(f"TLS 连接失败: {exc}") from exc

    if not der:
        raise ConnectionError("TLS 服务未返回证书")
    cert = x509.load_der_x509_certificate(der)
    not_after = cert.not_valid_after_utc.replace(tzinfo=None)
    days_remaining = int((not_after - current).total_seconds() // 86400)
    hostname_valid = _certificate_hostname_valid(cert, hostname)

    # 第二段使用系统信任库并执行主机名校验，同时验证信任链。
    verified_context = ssl.create_default_context()
    try:
        with socket.create_connection((hostname, port), timeout=timeout_seconds) as raw:
            with verified_context.wrap_socket(raw, server_hostname=hostname):
                pass
        valid = True
        verify_error = None
    except ssl.SSLCertVerificationError as exc:
        valid = False
        verify_error = exc.verify_message or str(exc)
    except (TimeoutError, socket.timeout) as exc:
        raise TimeoutError(f"TLS 验证连接超时: {exc}") from exc
    except OSError as exc:
        raise ConnectionError(f"TLS 验证连接失败: {exc}") from exc

    return {
        "applicable": True,
        "not_after": not_after,
        "days_remaining": days_remaining,
        "hostname_valid": hostname_valid,
        "valid": valid and hostname_valid,
        "error": verify_error if not (valid and hostname_valid) else None,
    }


def _is_waf_response(status_code: int, headers: Any, body: str) -> bool:
    """识别常见 WAF 拦截，不使用随机 UA 绕过，只记录事实。"""

    if status_code in WAF_HTTP_STATUSES:
        return True
    header_text = " ".join(f"{key}:{value}" for key, value in headers.items()).lower()
    body_lower = (body or "")[:200_000].lower()
    combined = f"{header_text}\n{body_lower}"
    return any(marker in combined for marker in WAF_BODY_MARKERS)


def _fetch_http(url: str, timeout_seconds: float) -> Dict[str, Any]:
    """抓取用于诊断的页面；证书由 SSL 检查单独判定。"""

    started = time.perf_counter()
    with httpx.Client(
        follow_redirects=True,
        timeout=timeout_seconds,
        verify=False,
        headers={"User-Agent": "PolicyIntelligenceAgent/1.0 source-diagnostic"},
    ) as client:
        response = client.get(url)
    return {
        "status_code": response.status_code,
        "headers": response.headers,
        "body": response.text,
        "final_url": str(response.url),
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


def _probe_config(source_config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    probe = source_config.get("health_probe")
    if not isinstance(probe, dict) or not probe.get("enabled", False):
        return None
    url = str(probe.get("url") or source_config.get("list_url") or source_config.get("website") or "").strip()
    if not url:
        return None
    timeout = probe.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    warning = probe.get("tls_warning_days", DEFAULT_TLS_WARNING_DAYS)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        timeout = DEFAULT_TIMEOUT_SECONDS
    if not isinstance(warning, int) or isinstance(warning, bool) or warning < 0:
        warning = DEFAULT_TLS_WARNING_DAYS
    selector = probe.get("selector")
    if selector is not None:
        selector = str(selector).strip() or None

    probe_format = str(probe.get("format") or "html").strip().lower()
    if probe_format not in {"html", "json"}:
        raise ValueError(f"health_probe.format 不受支持: {probe_format!r}")

    raw_required_keys = probe.get("json_required_keys") or []
    if isinstance(raw_required_keys, str):
        raw_required_keys = raw_required_keys.split(",")
    if not isinstance(raw_required_keys, (list, tuple)):
        raise ValueError("health_probe.json_required_keys 必须是字符串列表")
    required_keys: list[str] = []
    for value in raw_required_keys:
        key = str(value).strip()
        if key and key not in required_keys:
            required_keys.append(key)

    return {
        "url": url,
        "timeout_seconds": float(timeout),
        "tls_warning_days": warning,
        "selector": selector,
        "format": probe_format,
        "json_required_keys": required_keys,
    }


def _status_result(source_name: str, target_url: str, status: str, **values: Any) -> Dict[str, Any]:
    result = {
        "source_name": source_name,
        "target_url": _safe_url(target_url),
        "status": status,
        "http_status": None,
        "latency_ms": None,
        "tls_not_after": None,
        "tls_days_remaining": None,
        "tls_hostname_valid": None,
        "selector": None,
        "selector_found": None,
        "probe_format": None,
        "structure_target": None,
        "structure_valid": None,
        "error": None,
        "checked_at": _utcnow_naive(),
        "consecutive_anomalies": 0,
        "recorded": False,
        "persistence_error": None,
    }
    result.update(values)
    return result


def _count_consecutive_anomalies(rows: Iterable[SourceDiagnostic]) -> int:
    count = 0
    for row in rows:
        if row.status not in ANOMALY_STATUSES:
            break
        count += 1
    return count


class SourceDiagnosticService:
    """来源稳定性探针的读写入口。"""

    def probe_source(
        self,
        source_config: Dict[str, Any],
        suppress_alert: bool = False,
    ) -> Dict[str, Any]:
        source_name = str(source_config.get("name") or "").strip()
        if not source_name:
            raise ValueError("来源配置缺少 name")
        config = _probe_config(source_config)
        if not config:
            result = _status_result(
                source_name,
                str(source_config.get("list_url") or source_config.get("website") or ""),
                "unconfigured",
                error="未配置 health_probe.url，不能判定来源稳定性",
            )
            return self._record(result, suppress_alert=suppress_alert)

        target_url = config["url"]
        result = _status_result(source_name, target_url, "ok")
        result["selector"] = config["selector"]
        result["probe_format"] = config["format"]
        if config["format"] == "json":
            result["structure_target"] = ",".join(config["json_required_keys"])
        try:
            response = _fetch_http(target_url, config["timeout_seconds"])
        except httpx.TimeoutException as exc:
            result.update(status="timeout", error=redact_sensitive_text(str(exc))[:500])
            return self._record(result, suppress_alert=suppress_alert)
        except (socket.gaierror, httpx.ConnectError) as exc:
            message = str(exc)
            dns_failure = isinstance(exc, socket.gaierror) or any(
                marker in message.lower()
                for marker in ("name resolution", "nodename", "getaddrinfo", "name or service not known")
            )
            result.update(
                status="dns_error" if dns_failure else "connection_error",
                error=redact_sensitive_text(message)[:500],
            )
            return self._record(result, suppress_alert=suppress_alert)
        except Exception as exc:
            result.update(status="connection_error", error=redact_sensitive_text(str(exc))[:500])
            return self._record(result, suppress_alert=suppress_alert)

        result["http_status"] = response["status_code"]
        result["latency_ms"] = response["latency_ms"]

        try:
            tls = _inspect_tls(target_url, config["timeout_seconds"])
            result["tls_not_after"] = tls["not_after"]
            result["tls_days_remaining"] = tls["days_remaining"]
            result["tls_hostname_valid"] = tls["hostname_valid"]
            tls_invalid = tls["applicable"] and not tls["valid"]
            tls_error = tls["error"]
        except (socket.gaierror, httpx.ConnectError) as exc:
            result.update(status="dns_error", error=redact_sensitive_text(str(exc))[:500])
            return self._record(result, suppress_alert=suppress_alert)
        except TimeoutError as exc:
            result.update(status="timeout", error=redact_sensitive_text(str(exc))[:500])
            return self._record(result, suppress_alert=suppress_alert)
        except (ConnectionError, OSError, ssl.SSLError, ValueError) as exc:
            result.update(status="connection_error", error=redact_sensitive_text(str(exc))[:500])
            return self._record(result, suppress_alert=suppress_alert)

        if tls_invalid:
            result.update(status="tls_invalid", error=f"TLS 证书校验失败: {tls_error}"[:500])
            return self._record(result, suppress_alert=suppress_alert)

        if _is_waf_response(response["status_code"], response["headers"], response["body"]):
            result.update(status="waf_challenge", error="检测到 WAF 拦截或人机验证页面")
            return self._record(result, suppress_alert=suppress_alert)

        if response["status_code"] >= 400:
            result.update(
                status="http_error",
                error=f"HTTP {response['status_code']}",
            )
            return self._record(result, suppress_alert=suppress_alert)

        days = result.get("tls_days_remaining")
        if isinstance(days, int) and days <= config["tls_warning_days"]:
            result.update(
                status="tls_expiring",
                error=f"TLS 证书仅剩 {days} 天（预警阈值 {config['tls_warning_days']} 天）",
            )
            return self._record(result, suppress_alert=suppress_alert)

        if config["format"] == "json":
            required_keys = config["json_required_keys"]
            if not required_keys:
                result.update(
                    status="unconfigured",
                    error="JSON 探针未配置 json_required_keys，结构稳定性无法判定",
                )
                return self._record(result, suppress_alert=suppress_alert)
            try:
                payload = json.loads(response["body"])
            except (TypeError, ValueError) as exc:
                result.update(
                    status="schema_changed",
                    structure_valid=False,
                    error=f"JSON 响应无法解析: {redact_sensitive_text(str(exc))}"[:500],
                )
                return self._record(result, suppress_alert=suppress_alert)

            if not isinstance(payload, list) or not payload:
                result.update(
                    status="schema_changed",
                    structure_valid=False,
                    error="JSON 顶层结构应为非空数组",
                )
                return self._record(result, suppress_alert=suppress_alert)

            invalid_records = [
                index
                for index, item in enumerate(payload)
                if not isinstance(item, dict)
                or any(
                    key not in item or item.get(key) in (None, "")
                    for key in required_keys
                )
            ]
            if invalid_records:
                sample = ", ".join(str(index) for index in invalid_records[:5])
                result.update(
                    status="schema_changed",
                    structure_valid=False,
                    error=(
                        f"JSON 记录缺少必需字段 {required_keys}；异常记录索引: {sample}"
                    )[:500],
                )
                return self._record(result, suppress_alert=suppress_alert)

            result["structure_valid"] = True
            return self._record(result, suppress_alert=suppress_alert)

        selector = config["selector"]
        if not selector:
            result.update(
                status="unconfigured",
                error="未配置已验证的 DOM selector，结构稳定性无法判定",
            )
            return self._record(result, suppress_alert=suppress_alert)

        try:
            selector_found = BeautifulSoup(response["body"], "lxml").select_one(selector) is not None
        except Exception as exc:
            result.update(
                status="dom_changed",
                selector_found=False,
                structure_valid=False,
                error=f"DOM selector 无法执行: {redact_sensitive_text(str(exc))}"[:500],
            )
            return self._record(result, suppress_alert=suppress_alert)

        result["selector_found"] = selector_found
        result["structure_valid"] = selector_found
        if not selector_found:
            result.update(status="dom_changed", error="配置的 DOM selector 未命中，网页结构可能已改版")
            return self._record(result, suppress_alert=suppress_alert)
        return self._record(result, suppress_alert=suppress_alert)

    def probe_all(self, suppress_alert: bool = False) -> Dict[str, Any]:
        sources = get_enabled_sources()
        results = []
        for source in sources:
            try:
                results.append(self.probe_source(source, suppress_alert=suppress_alert))
            except Exception as exc:
                # 单个来源的配置错误不能阻断其他来源巡检。
                name = str(source.get("name") or "<unknown>")
                logger.exception("来源稳定性探针异常: %s", name)
                failed = _status_result(
                    name,
                    str(source.get("list_url") or source.get("website") or ""),
                    "connection_error",
                    error=f"探针程序异常: {redact_sensitive_text(str(exc))}"[:500],
                    persistence_error="探针程序异常，结果未写入诊断表",
                )
                results.append(failed)
        return self.summary(results=results)

    def _record(self, result: Dict[str, Any], *, suppress_alert: bool) -> Dict[str, Any]:
        """持久化一次探针结果，并显式报告落库是否成功。

        探针完成不等于验收完成：若结果没有写入审计表，CLI 必须以程序错误
        退出，而不是把一次未留痕的检查显示为通过。
        """

        db: Optional[Session] = None
        try:
            db = SessionLocal()
            row = SourceDiagnostic(
                source_name=result["source_name"],
                target_url=result["target_url"],
                status=result["status"],
                http_status=result.get("http_status"),
                latency_ms=result.get("latency_ms"),
                tls_not_after=result.get("tls_not_after"),
                tls_days_remaining=result.get("tls_days_remaining"),
                tls_hostname_valid=result.get("tls_hostname_valid"),
                selector=result.get("selector"),
                selector_found=result.get("selector_found"),
                error=result.get("error"),
                checked_at=result["checked_at"],
            )
            db.add(row)
            db.commit()
            result["recorded"] = True
            result["persistence_error"] = None
        except Exception as exc:
            logger.warning("来源诊断记录写入失败 %s: %s", result.get("source_name"), exc)
            result["recorded"] = False
            result["persistence_error"] = redact_sensitive_text(str(exc))[:500]
            if db is not None:
                db.rollback()
            return result

        # 统计和告警失败不能反向宣称“未落库”；主记录此时已提交。
        try:
            recent = (
                db.query(SourceDiagnostic)
                .filter(SourceDiagnostic.source_name == result["source_name"])
                .order_by(SourceDiagnostic.id.desc())
                .limit(100)
                .all()
            )
            consecutive = _count_consecutive_anomalies(recent)
            result["consecutive_anomalies"] = consecutive
            if (
                consecutive == DIAGNOSTIC_ALERT_THRESHOLD
                and not suppress_alert
                and result["status"] in ANOMALY_STATUSES
            ):
                self._send_alert(result)
        except Exception as exc:
            logger.warning("来源诊断后处理失败 %s: %s", result.get("source_name"), exc)
        finally:
            if db is not None:
                db.close()
        return result

    def _send_alert(self, result: Dict[str, Any]) -> None:
        """诊断告警失败只记日志，不改变探针结果。"""

        try:
            from app.config import settings
            from app.notifiers.service import NotificationService

            if not getattr(settings, "source_health_alert_enabled", True):
                logger.info("来源诊断告警已禁用，跳过推送: %s", result["source_name"])
                return
            channels = [item.strip() for item in settings.notify_channels.split(",") if item.strip()]
            if not channels:
                logger.warning("未配置通知渠道，来源诊断告警仅记录日志")
                return
            content = (
                "## 来源稳定性告警\n\n"
                f"- **来源**：{result['source_name']}\n"
                f"- **连续异常**：{result['consecutive_anomalies']} 次\n"
                f"- **状态**：{result['status']}\n"
                f"- **URL**：{result['target_url']}\n"
                f"- **详情**：{(result.get('error') or '未知')[:300]}\n\n"
                "请检查 WAF、TLS 证书、DNS 或网页 DOM 结构变化。"
            )
            NotificationService.notify_channels(
                channels=channels,
                content=content,
                metadata={
                    "task_id": f"source_diagnostic:{result['source_name']}",
                    "task_type": "source_diagnostic_alert",
                    "trigger_type": "system",
                },
            )
        except Exception as exc:
            logger.warning("来源诊断告警推送失败: %s", exc)

    def latest_snapshot(self) -> list[Dict[str, Any]]:
        """返回所有当前启用来源的最新状态。

        没有历史记录的来源必须显示为 ``not_checked``（或 ``unconfigured``），
        不能因为查询结果为空就误显示为通过。
        """

        db: Session = SessionLocal()
        try:
            rows = db.query(SourceDiagnostic).order_by(SourceDiagnostic.id.desc()).all()
        finally:
            db.close()

        grouped: Dict[str, list[SourceDiagnostic]] = {}
        for row in rows:
            grouped.setdefault(row.source_name, []).append(row)

        snapshot: list[Dict[str, Any]] = []
        enabled_names: set[str] = set()
        for source in get_enabled_sources():
            name = str(source.get("name") or "").strip()
            if not name:
                continue
            enabled_names.add(name)
            source_rows = grouped.get(name, [])
            config = _probe_config(source)
            if source_rows:
                latest = source_rows[0]
                snapshot.append({
                    "source_name": latest.source_name,
                    "target_url": latest.target_url,
                    "status": latest.status,
                    "http_status": latest.http_status,
                    "latency_ms": latest.latency_ms,
                    "tls_not_after": latest.tls_not_after.isoformat() if latest.tls_not_after else None,
                    "tls_days_remaining": latest.tls_days_remaining,
                    "tls_hostname_valid": latest.tls_hostname_valid,
                    "selector": latest.selector,
                    "selector_found": latest.selector_found,
                    "error": latest.error,
                    "checked_at": latest.checked_at.isoformat() if latest.checked_at else None,
                    "consecutive_anomalies": _count_consecutive_anomalies(source_rows),
                    "recorded": True,
                    "persistence_error": None,
                    "enabled": True,
                })
                continue

            target_url = (
                config["url"] if config else
                str(source.get("list_url") or source.get("website") or "")
            )
            status = "not_checked" if config else "unconfigured"
            error = (
                "尚无诊断记录，请运行 source-diagnostics --probe"
                if config else
                "未配置 health_probe.url，不能判定来源稳定性"
            )
            snapshot.append({
                "source_name": name,
                "target_url": _safe_url(target_url),
                "status": status,
                "http_status": None,
                "latency_ms": None,
                "tls_not_after": None,
                "tls_days_remaining": None,
                "tls_hostname_valid": None,
                "selector": config["selector"] if config else None,
                "selector_found": None,
                "error": error,
                "checked_at": None,
                "consecutive_anomalies": 0,
                "recorded": False,
                "persistence_error": None,
                "enabled": True,
            })

        # 配置已移除但仍有历史记录时保留只读视图，便于审计来源变更。
        for name, source_rows in grouped.items():
            if name in enabled_names:
                continue
            latest = source_rows[0]
            snapshot.append({
                "source_name": latest.source_name,
                "target_url": latest.target_url,
                "status": latest.status,
                "http_status": latest.http_status,
                "latency_ms": latest.latency_ms,
                "tls_not_after": latest.tls_not_after.isoformat() if latest.tls_not_after else None,
                "tls_days_remaining": latest.tls_days_remaining,
                "tls_hostname_valid": latest.tls_hostname_valid,
                "selector": latest.selector,
                "selector_found": latest.selector_found,
                "error": latest.error,
                "checked_at": latest.checked_at.isoformat() if latest.checked_at else None,
                "consecutive_anomalies": _count_consecutive_anomalies(source_rows),
                "recorded": True,
                "persistence_error": None,
                "enabled": False,
            })
        return sorted(snapshot, key=lambda item: item["source_name"])

    def summary(self, *, results: Optional[list[Dict[str, Any]]] = None) -> Dict[str, Any]:
        sources = results if results is not None else self.latest_snapshot()
        unhealthy = [item for item in sources if item.get("status") != "ok"]
        return {
            "total": len(sources),
            "ok": sum(1 for item in sources if item.get("status") == "ok"),
            "unhealthy": len(unhealthy),
            "waf_challenge": sum(1 for item in sources if item.get("status") == "waf_challenge"),
            "tls_issues": sum(1 for item in sources if item.get("status") in {"tls_invalid", "tls_expiring"}),
            "dom_issues": sum(1 for item in sources if item.get("status") == "dom_changed"),
            "schema_issues": sum(1 for item in sources if item.get("status") == "schema_changed"),
            "unconfigured": sum(1 for item in sources if item.get("status") == "unconfigured"),
            "not_checked": sum(1 for item in sources if item.get("status") == "not_checked"),
            "transport_errors": sum(
                1 for item in sources
                if item.get("status") in {"timeout", "dns_error", "connection_error", "http_error"}
            ),
            "storage_errors": sum(1 for item in sources if item.get("persistence_error")),
            "unhealthy_sources": [item["source_name"] for item in unhealthy],
            "sources": sources,
        }


source_diagnostic_service = SourceDiagnosticService()


def run_source_diagnostics() -> Dict[str, Any]:
    """调度器入口：执行全部启用来源探针并返回摘要。"""

    return source_diagnostic_service.probe_all(suppress_alert=False)
