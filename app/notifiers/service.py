"""统一通知服务
负责渠道选择、长文本切分、失败重试和幂等发送
"""

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import SessionLocal
from app.logger import logger
from app.models.notification_record import NotificationRecord
from app.notifiers.email_notifier import EmailNotifier
from app.notifiers.feishu_notifier import FeishuNotifier
from app.notifiers.safety import redact_sensitive_text, safe_exception_message
from app.notifiers.wechat_notifier import WechatNotifier


CHANNEL_MAX_CHARS = {
    "email": 50000,
    "wechat": 1200,
    "feishu": 4000,
}

NOTIFIERS = {
    "email": EmailNotifier,
    "wechat": WechatNotifier,
    "feishu": FeishuNotifier,
}


class NotificationError(Exception):
    """通知发送失败"""


def _pending_lease_reference(record: NotificationRecord) -> Optional[datetime]:
    """返回 pending 租约最近一次心跳时间。"""
    return record.updated_at or record.created_at


def _is_stale_pending(record: NotificationRecord, now: Optional[datetime] = None) -> bool:
    """判断 pending 记录是否已超过租约；无时间戳的旧记录按失效处理。"""
    if record.status != "pending":
        return False
    reference = _pending_lease_reference(record)
    if reference is None:
        return True
    current = now or datetime.now()
    return reference <= current - timedelta(
        seconds=settings.notification_pending_timeout_seconds
    )


def _split_text(content: str, max_chars: int) -> List[str]:
    """按换行边界切分长文本，避免破坏段落"""
    if not content:
        return []

    chunks: List[str] = []
    current: List[str] = []
    current_length = 0

    for line in content.splitlines(keepends=True):
        if current and current_length + len(line) > max_chars:
            chunks.append("".join(current))
            current = []
            current_length = 0

        if len(line) > max_chars:
            if current:
                chunks.append("".join(current))
                current = []
                current_length = 0
            for index in range(0, len(line), max_chars):
                chunks.append(line[index:index + max_chars])
        else:
            current.append(line)
            current_length += len(line)

    if current:
        chunks.append("".join(current))

    return chunks


class NotificationService:
    """统一通知入口"""

    @staticmethod
    def preview(
        report_path: Path,
        channels: List[str],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """校验报告并计算各渠道发送计划，不创建台账也不访问网络。"""
        report_file = Path(report_path)
        try:
            content = report_file.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise NotificationError(f"报告文件不存在: {report_file}") from exc
        except UnicodeDecodeError as exc:
            raise NotificationError(f"报告文件不是有效的 UTF-8 文本: {report_file}") from exc
        except OSError as exc:
            raise NotificationError(
                f"读取报告文件失败: {report_file} ({safe_exception_message(exc)})"
            ) from exc

        if not content.strip():
            raise NotificationError(f"报告内容为空: {report_file}")

        normalized_channels: List[str] = []
        for channel in channels:
            if channel not in normalized_channels:
                normalized_channels.append(channel)

        channel_results: Dict[str, Dict[str, Any]] = {}
        for channel in normalized_channels:
            if channel not in NOTIFIERS:
                raise NotificationError(f"未知通知渠道: {channel}")
            try:
                enabled = NOTIFIERS[channel]().is_enabled()
            except Exception as exc:
                raise NotificationError(
                    f"检查通知渠道配置失败 {channel}: {safe_exception_message(exc)}"
                ) from exc
            channel_results[channel] = {
                "configured": enabled,
                "chars": len(content),
                "max_chars": CHANNEL_MAX_CHARS[channel],
                "estimated_chunks": len(_split_text(content, CHANNEL_MAX_CHARS[channel])),
            }

        return {
            "report_path": str(report_file.resolve()),
            "chars": len(content),
            "channels": channel_results,
            "metadata": metadata or {},
        }

    @staticmethod
    def _get_or_create_record(
        task_id: str,
        channel: str,
        idempotency_key: str,
        max_retries: int,
        metadata: Dict[str, Any],
    ) -> tuple[Optional[NotificationRecord], bool]:
        """查询或创建通知记录"""
        db = SessionLocal()
        try:
            record = db.query(NotificationRecord).filter(
                NotificationRecord.idempotency_key == idempotency_key
            ).first()
            if record:
                return record, False

            record = NotificationRecord(
                task_id=task_id,
                channel=channel,
                idempotency_key=idempotency_key,
                status="pending",
                chunks=0,
                sent_chunks=0,
                retry_count=0,
                max_retries=max_retries,
                meta=metadata,
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            return record, True
        except IntegrityError:
            # 并发调用可能同时通过上面的存在性检查。唯一键冲突说明另一
            # 调用已创建同一任务，回滚后取回其记录，由调用方按 pending /
            # success 状态跳过，避免重复向渠道发送。
            db.rollback()
            record = db.query(NotificationRecord).filter(
                NotificationRecord.idempotency_key == idempotency_key
            ).first()
            if record:
                return record, False
            logger.error("通知记录唯一键冲突后未能取回记录: %s", idempotency_key)
            return None, False
        except Exception as exc:
            logger.error("创建通知记录失败: %s", safe_exception_message(exc))
            db.rollback()
            return None, False
        finally:
            db.close()

    @staticmethod
    def _update_record(record_id: int, **fields) -> None:
        """更新通知记录。"""
        db = SessionLocal()
        try:
            record = db.query(NotificationRecord).filter(
                NotificationRecord.id == record_id
            ).first()
            if record:
                for key, value in fields.items():
                    setattr(record, key, value)
                db.commit()
        except Exception as exc:
            logger.error("更新通知记录失败 id=%s: %s", record_id, safe_exception_message(exc))
            db.rollback()
        finally:
            db.close()

    @staticmethod
    def _try_claim_record(
        record: NotificationRecord,
        max_retries: int,
    ) -> bool:
        """原子接管 failed 或租约过期的 pending 记录。

        条件更新必须同时匹配原状态和原更新时间，确保多个进程只有一个
        能取得发送权。成功返回 True，被别人抢先接管返回 False。
        """
        db = SessionLocal()
        try:
            query = db.query(NotificationRecord).filter(
                NotificationRecord.id == record.id,
                NotificationRecord.status == record.status,
            )
            if record.updated_at is None:
                query = query.filter(NotificationRecord.updated_at.is_(None))
            else:
                query = query.filter(NotificationRecord.updated_at == record.updated_at)

            now = datetime.now()
            claimed = query.update(
                {
                    "status": "pending",
                    "max_retries": max_retries,
                    "updated_at": now,
                },
                synchronize_session=False,
            )
            db.commit()
            if claimed != 1:
                return False

            record.status = "pending"
            record.max_retries = max_retries
            record.updated_at = now
            return True
        except Exception as exc:
            db.rollback()
            logger.error(
                "接管通知记录失败 id=%s: %s",
                record.id,
                safe_exception_message(exc),
            )
            return False
        finally:
            db.close()

    @staticmethod
    def get_status(task_id: str) -> Dict[str, Any]:
        """按任务 ID 查询通知台账，不包含可能含敏感信息的 meta 字段。"""
        normalized_task_id = str(task_id or "").strip()
        if not normalized_task_id:
            raise NotificationError("任务 ID 不能为空")

        db = SessionLocal()
        try:
            records = (
                db.query(NotificationRecord)
                .filter(NotificationRecord.task_id == normalized_task_id)
                .order_by(NotificationRecord.created_at.asc(), NotificationRecord.id.asc())
                .all()
            )
            now = datetime.now()
            items = [
                {
                    "record_id": record.id,
                    "channel": record.channel,
                    "status": (
                        "stale"
                        if _is_stale_pending(record, now)
                        else record.status
                    ),
                    "chunks": record.chunks,
                    "sent_chunks": record.sent_chunks,
                    "retry_count": record.retry_count,
                    "max_retries": record.max_retries,
                    "error_message": (
                        redact_sensitive_text(record.error_message)
                        if record.error_message
                        else None
                    ),
                    "created_at": record.created_at.isoformat() if record.created_at else None,
                    "updated_at": record.updated_at.isoformat() if record.updated_at else None,
                    "sent_at": record.sent_at.isoformat() if record.sent_at else None,
                }
                for record in records
            ]
        except Exception as exc:
            logger.error("查询通知状态失败: %s", safe_exception_message(exc))
            raise NotificationError("查询通知状态失败，请检查数据库") from exc
        finally:
            db.close()

        statuses = {item["status"] for item in items}
        if not items:
            overall_status = "not_found"
        elif statuses <= {"success"}:
            overall_status = "success"
        elif "failed" in statuses:
            overall_status = "failed"
        elif "stale" in statuses:
            overall_status = "stale"
        elif "pending" in statuses:
            overall_status = "pending"
        else:
            overall_status = "unknown"

        return {
            "task_id": normalized_task_id,
            "status": overall_status,
            "record_count": len(items),
            "records": items,
        }

    @staticmethod
    def notify(
        channel: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        max_retries: int = 2,
    ) -> Dict[str, Any]:
        """发送通知；同一幂等键成功后不再重复发送"""
        if channel not in NOTIFIERS:
            raise NotificationError(f"未知通知渠道: {channel}")
        if not content.strip():
            return {"status": "skipped", "reason": "通知内容为空"}

        metadata = metadata or {}
        task_id = str(metadata.get("task_id") or uuid4())
        idempotency_key = str(metadata.get("idempotency_key") or f"{task_id}:{channel}")
        notifier = NOTIFIERS[channel]()

        if not notifier.is_enabled():
            return {"status": "skipped", "reason": f"{channel} 未配置"}

        record, created = NotificationService._get_or_create_record(
            task_id=task_id,
            channel=channel,
            idempotency_key=idempotency_key,
            max_retries=max_retries,
            metadata=metadata,
        )
        if record is None:
            # 台账不可用时必须拒绝发送。否则唯一键无法提供幂等保护，
            # 重试或并发请求可能造成真实重复通知。
            return {
                "status": "failed",
                "channel": channel,
                "error": "创建通知记录失败，已阻止发送",
            }

        if record.status == "success":
            return {
                "status": "skipped",
                "reason": "通知已发送",
                "record_id": record.id,
            }

        if not created:
            if record.status == "pending" and not _is_stale_pending(record):
                return {
                    "status": "skipped",
                    "reason": "通知正在发送",
                    "record_id": record.id,
                }
            if record.status not in {"pending", "failed"}:
                return {
                    "status": "skipped",
                    "reason": f"通知记录状态不可发送: {record.status}",
                    "record_id": record.id,
                }
            if not NotificationService._try_claim_record(record, max_retries):
                return {
                    "status": "skipped",
                    "reason": "通知已由其他进程接管",
                    "record_id": record.id,
                }

        chunks = _split_text(content, CHANNEL_MAX_CHARS[channel])
        sent_chunks = record.sent_chunks
        if sent_chunks >= len(chunks):
            # 进程可能在最后一个分片发送后、写入 success 前退出。
            # 此时只补记完成状态，不能再发送任何分片。
            NotificationService._update_record(
                record.id,
                status="success",
                chunks=len(chunks),
                sent_chunks=sent_chunks,
                error_message=None,
                sent_at=datetime.now(),
            )
            return {"status": "success", "chunks": len(chunks), "sent_chunks": sent_chunks}

        NotificationService._update_record(
            record.id,
            status="pending",
            chunks=len(chunks),
            max_retries=max_retries,
            error_message=None,
        )

        attempts = max_retries + 1
        last_error: Optional[str] = None

        for attempt in range(1, attempts + 1):
            failed = False
            for index, chunk in enumerate(chunks[sent_chunks:], sent_chunks + 1):
                chunk_metadata = {
                    **metadata,
                    "chunk_index": index,
                    "chunk_total": len(chunks),
                }
                try:
                    success = notifier.send_content(chunk, chunk_metadata)
                except Exception as exc:
                    success = False
                    last_error = safe_exception_message(exc)
                    logger.error(
                        "通知发送异常 channel=%s chunk=%s: %s",
                        channel,
                        index,
                        last_error,
                    )

                if not success:
                    failed = True
                    last_error = last_error or f"渠道返回失败 chunk={index}"
                    logger.error(f"通知发送失败 channel={channel} chunk={index}")
                    break

                sent_chunks = index
                NotificationService._update_record(
                    record.id,
                    sent_chunks=sent_chunks,
                    chunks=len(chunks),
                )

            if not failed:
                NotificationService._update_record(
                    record.id,
                    status="success",
                    chunks=len(chunks),
                    sent_chunks=sent_chunks,
                    retry_count=attempt - 1,
                    error_message=None,
                    sent_at=datetime.now(),
                )
                return {
                    "status": "success",
                    "channel": channel,
                    "chunks": len(chunks),
                    "sent_chunks": sent_chunks,
                }

            if attempt < attempts:
                # 退避期间仍持有租约，避免其他进程把瞬时失败当成最终失败
                # 并提前接管，导致同一分片被重复发送。
                NotificationService._update_record(
                    record.id,
                    status="pending",
                    retry_count=attempt,
                    error_message=last_error,
                )
                wait_seconds = min(2 ** (attempt - 1), 8)
                logger.warning(f"通知将在 {wait_seconds} 秒后重试 channel={channel}")
                time.sleep(wait_seconds)
            else:
                NotificationService._update_record(
                    record.id,
                    status="failed",
                    retry_count=attempt,
                    error_message=last_error,
                )

        return {
            "status": "failed",
            "channel": channel,
            "error": last_error,
            "chunks": len(chunks),
            "sent_chunks": sent_chunks,
        }

    @staticmethod
    def notify_channels(
        channels: List[str],
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        max_retries: int = 2,
    ) -> Dict[str, Dict[str, Any]]:
        """批量发送到多个渠道，单个渠道失败不影响其他渠道"""
        results: Dict[str, Dict[str, Any]] = {}
        for channel in channels:
            try:
                results[channel] = NotificationService.notify(
                    channel=channel,
                    content=content,
                    metadata=metadata,
                    max_retries=max_retries,
                )
            except Exception as exc:
                safe_error = safe_exception_message(exc)
                logger.error("通知渠道执行失败 channel=%s: %s", channel, safe_error)
                results[channel] = {"status": "failed", "error": safe_error}
        return results
