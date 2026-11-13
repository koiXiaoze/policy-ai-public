"""数据源健康服务

职责：
1. 在采集时记录每个来源的健康状态（ok / empty / degraded / failed）；
2. 汇总最新健康快照，供 /health 与日报使用；
3. 识别连续失败来源并输出告警摘要。

设计原则：健康记录失败绝不能影响采集本身，所有写入都做异常隔离。
"""

from datetime import datetime
import re
from typing import Any, Dict, List, Optional

from sqlalchemy import func

from app.config import get_enabled_sources, get_source_stale_after_hours
from app.database import SessionLocal
from app.logger import logger
from app.models.policy import Policy
from app.models.source_health import SourceHealth


#: 连续失败达到该次数视为需要关注的来源
FAILURE_ALERT_THRESHOLD = 3
VALID_STATUSES = {"ok", "empty", "degraded", "failed"}
SUCCESS_STATUSES = {"ok", "empty"}
FAILURE_STATUSES = {"degraded", "failed"}
SUCCESS_WINDOW_CHECKS = 20
ERROR_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class SourceHealthService:
    """数据源健康状态读写入口"""

    @staticmethod
    def record(
        source_name: str,
        item_count: int,
        newest_publish_time: Optional[datetime] = None,
        error: Optional[str] = None,
        task_id: str = "",
        status: Optional[str] = None,
        error_type: Optional[str] = None,
        suppress_alert: bool = False,
    ) -> None:
        """记录一次来源检查结果，并维护连续失败计数。

        status 是采集器提供的实时通道结果。未传时沿用旧推断逻辑，
        从而兼容尚未迁移的第三方采集器和历史调用。
        """
        if not source_name:
            return

        if status is None:
            if error:
                status = "failed"
            elif item_count > 0:
                status = "ok"
            else:
                # 调用成功但没抓到内容：可能是来源本身近期无更新，而非故障
                status = "empty"
        elif status not in VALID_STATUSES:
            raise ValueError(f"非法来源状态: {status!r}，仅允许 {sorted(VALID_STATUSES)}")

        if error_type and not ERROR_TYPE_PATTERN.fullmatch(error_type):
            raise ValueError(f"非法错误类型: {error_type!r}，必须是 1-40 位小写字母、数字或下划线")
        if status == "failed":
            # 保留原始异常；degraded 的错误仅用于说明旧数据回退原因。
            error = error or "采集失败"
        if status in SUCCESS_STATUSES:
            error = None
            error_type = None

        db = SessionLocal()
        try:
            previous = (
                db.query(SourceHealth)
                .filter(SourceHealth.source_name == source_name)
                .order_by(SourceHealth.id.desc())
                .first()
            )
            previous_failures = previous.consecutive_failures if previous else 0
            # 降级同样代表本次没有取得新的在线数据，必须延续连续失败计数。
            consecutive = (
                previous_failures + 1 if status in FAILURE_STATUSES else 0
            )

            db.add(SourceHealth(
                source_name=source_name,
                status=status,
                item_count=item_count,
                newest_publish_time=newest_publish_time,
                error=error,
                error_type=error_type,
                consecutive_failures=consecutive,
                task_id=task_id[:50] or None,
            ))
            db.commit()

            if consecutive >= FAILURE_ALERT_THRESHOLD:
                logger.error(
                    "数据源连续失败告警: %s 已连续 %s 次失败, 类型=%s, 最近错误=%s",
                    source_name, consecutive, error_type or "unknown", (error or "")[:120],
                )
                # 仅在恰好达到阈值时推送一次，避免此后每轮重复打扰。
                # 巡检探针仍应写入健康记录，但不能触发面向用户的生产告警。
                if consecutive == FAILURE_ALERT_THRESHOLD and not suppress_alert:
                    SourceHealthService._send_alert(
                        source_name, consecutive, error, error_type
                    )
        except Exception as exc:
            logger.warning("数据源健康记录写入失败 %s: %s", source_name, exc)
            db.rollback()
        finally:
            db.close()

    @staticmethod
    def _source_policy_times(db, source_names: List[str]) -> Dict[str, Optional[datetime]]:
        """查询各来源最近一次政策入库时间，单来源失败不拖垮整体。"""
        result: Dict[str, Optional[datetime]] = {name: None for name in source_names}
        for source_name in source_names:
            try:
                result[source_name] = (
                    db.query(func.max(Policy.created_time))
                    .filter(Policy.source_name == source_name)
                    .scalar()
                )
            except Exception as exc:
                logger.warning("查询来源最近政策入库时间失败 %s: %s", source_name, exc)
                # 单来源 SQL 失败可能让当前 Session 进入不可继续执行事务的状态；
                # 回滚后其他来源仍能计算新鲜度，而不是整批退化为 unknown。
                try:
                    db.rollback()
                except Exception as rollback_exc:
                    logger.warning(
                        "回滚来源新鲜度查询失败 %s: %s", source_name, rollback_exc
                    )
        return result

    @staticmethod
    def _freshness_status(
        last_new_policy_at: Optional[datetime],
        checked_status: str,
        stale_threshold_hours: float,
        *,
        now: Optional[datetime] = None,
    ) -> tuple[str, Optional[float]]:
        """返回 (新鲜度状态, 距最近入库小时数)，判定顺序不可交换。"""
        if last_new_policy_at is None:
            return "unknown", None
        current = now or datetime.now()
        try:
            age_hours = (current - last_new_policy_at).total_seconds() / 3600
        except (TypeError, ValueError, OverflowError):
            return "unknown", None
        if age_hours < 0:
            return "unknown", None
        if age_hours > stale_threshold_hours:
            return "stale", round(age_hours, 1)
        if checked_status not in SUCCESS_STATUSES:
            return "unknown", round(age_hours, 1)
        return "fresh", round(age_hours, 1)

    @staticmethod
    def latest_snapshot(*, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """返回最新健康记录及数据新鲜度，旧字段保持兼容。"""
        db = SessionLocal()
        try:
            rows = db.query(SourceHealth).order_by(SourceHealth.id.desc()).all()
            try:
                enabled_sources = get_enabled_sources()
            except Exception as exc:
                logger.warning("读取启用来源配置失败，新鲜度按默认阈值计算: %s", exc)
                enabled_sources = []
            stale_thresholds = {
                str(source.get("name", "")).strip(): get_source_stale_after_hours(source)
                for source in enabled_sources
                if isinstance(source, dict) and str(source.get("name", "")).strip()
            }
            policy_times = SourceHealthService._source_policy_times(
                db, sorted({row.source_name for row in rows})
            )
        except Exception as exc:
            logger.warning("数据源健康快照查询失败: %s", exc)
            return []
        finally:
            db.close()

        def iso(value: Optional[datetime]) -> Optional[str]:
            return value.isoformat() if value else None

        rows_by_source: Dict[str, List[SourceHealth]] = {}
        for row in rows:
            rows_by_source.setdefault(row.source_name, []).append(row)

        snapshot: List[Dict[str, Any]] = []
        for source_rows in rows_by_source.values():
            latest = source_rows[0]
            success_rows = [item for item in source_rows if item.status in SUCCESS_STATUSES]
            recent = source_rows[:SUCCESS_WINDOW_CHECKS]
            recent_success_count = sum(1 for item in recent if item.status in SUCCESS_STATUSES)
            recent_check_count = len(recent)
            last_recovery = next(
                (
                    item.checked_at
                    for index, item in enumerate(source_rows[:-1])
                    if item.status in SUCCESS_STATUSES
                    and source_rows[index + 1].status in FAILURE_STATUSES
                ),
                None,
            )
            threshold = stale_thresholds.get(
                latest.source_name, get_source_stale_after_hours({})
            )
            last_new_policy_at = policy_times.get(latest.source_name)
            freshness_status, stale_hours = SourceHealthService._freshness_status(
                last_new_policy_at, latest.status, threshold, now=now
            )
            snapshot.append({
                "source_name": latest.source_name,
                "status": latest.status,
                "item_count": latest.item_count,
                "newest_publish_time": iso(latest.newest_publish_time),
                "error": latest.error,
                "error_type": latest.error_type,
                "consecutive_failures": latest.consecutive_failures,
                "checked_at": iso(latest.checked_at),
                "last_live_success_at": iso(
                    success_rows[0].checked_at if success_rows else None
                ),
                "recent_check_count": recent_check_count,
                "recent_success_count": recent_success_count,
                "success_rate": (
                    round(recent_success_count / recent_check_count * 100, 1)
                    if recent_check_count
                    else None
                ),
                "last_recovery_at": iso(last_recovery),
                "last_new_policy_at": iso(last_new_policy_at),
                "stale_hours": stale_hours,
                "freshness_status": freshness_status,
                "stale_threshold_hours": threshold,
            })
        return snapshot

    @staticmethod
    def _send_alert(
        source_name: str,
        consecutive: int,
        error: Optional[str],
        error_type: Optional[str] = None,
    ) -> None:
        """通过既有通知渠道推送来源告警。

        告警失败只记日志，绝不影响采集；测试环境通过
        SOURCE_HEALTH_ALERT_ENABLED=false 关闭，避免误发真实通知。
        """
        try:
            from app.config import settings
            from app.notifiers.service import NotificationService

            if not getattr(settings, "source_health_alert_enabled", True):
                logger.info("数据源告警已禁用，跳过推送: %s", source_name)
                return

            channels = [
                item.strip()
                for item in settings.notify_channels.split(",")
                if item.strip()
            ]
            if not channels:
                logger.warning("未配置通知渠道，数据源告警仅记录日志: %s", source_name)
                return

            content = (
                "## 数据源采集告警\n\n"
                f"- **来源**：{source_name}\n"
                f"- **连续失败**：{consecutive} 次\n"
                f"- **错误类型**：{error_type or '未知'}\n"
                f"- **最近错误**：{(error or '未知')[:200]}\n\n"
                "该来源已连续多轮采集失败，可能导致日报内容缺失，请检查网络或来源可用性。"
            )
            results = NotificationService.notify_channels(
                channels=channels,
                content=content,
                metadata={
                    "task_id": f"source_alert:{source_name}",
                    "task_type": "source_health_alert",
                    "trigger_type": "system",
                },
            )
            logger.info("数据源告警推送结果 %s: %s", source_name, results)
        except Exception as exc:
            logger.warning("数据源告警推送失败 %s: %s", source_name, exc)

    @staticmethod
    def summary(*, now: Optional[datetime] = None) -> Dict[str, Any]:
        """汇总健康状态与新鲜度，供 /health 与日报摘要使用。"""
        snapshot = SourceHealthService.latest_snapshot(now=now)
        failing = [
            s for s in snapshot
            if s["consecutive_failures"] >= FAILURE_ALERT_THRESHOLD
        ]
        degraded = [s for s in snapshot if s["status"] == "degraded"]
        fresh = [s for s in snapshot if s.get("freshness_status") == "fresh"]
        stale = [s for s in snapshot if s.get("freshness_status") == "stale"]
        unknown_freshness = [
            s for s in snapshot if s.get("freshness_status") == "unknown"
        ]
        return {
            "total": len(snapshot),
            "ok": sum(1 for s in snapshot if s["status"] == "ok"),
            "empty": sum(1 for s in snapshot if s["status"] == "empty"),
            "degraded": len(degraded),
            "failed": sum(1 for s in snapshot if s["status"] == "failed"),
            "failing_sources": [s["source_name"] for s in failing],
            "degraded_sources": [s["source_name"] for s in degraded],
            "fresh_sources": [s["source_name"] for s in fresh],
            "stale_sources": [s["source_name"] for s in stale],
            "unknown_freshness_sources": [
                s["source_name"] for s in unknown_freshness
            ],
            "fresh": len(fresh),
            "stale": len(stale),
            "unknown_freshness": len(unknown_freshness),
            "sources": snapshot,
        }


source_health_service = SourceHealthService()
