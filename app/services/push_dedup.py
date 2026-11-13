"""推送去重服务

通知层的幂等键是 task_id，只防「同一天重复发送」；本模块按政策内容指纹
去重，防的是「不同天反复推送同一批政策」——即用户实际看到的推送重复。

指纹优先级：URL（清洗 query/尾斜杠后）> 归一化标题。URL 缺失时退化到标题，
两者都缺失的条目不做去重（避免误伤）。
"""

import hashlib
import re
from typing import Any, Dict, Iterable, List

from app.database import SessionLocal
from app.logger import logger
from app.models.pushed_policy import PushedPolicy


def _normalize_url(url: str) -> str:
    """去掉协议、www 前缀、query/fragment 和尾斜杠，降低同一政策的 URL 差异。"""
    if not url:
        return ""
    text = url.strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"^www\.", "", text)
    text = text.split("#", 1)[0].split("?", 1)[0]
    return text.rstrip("/")


def _normalize_title(title: str) -> str:
    """标题归一化：去掉空白与常见标点差异后再比较。"""
    if not title:
        return ""
    text = re.sub(r"[\s\u3000]+", "", str(title))
    return re.sub(r"[《》〈〉「」『』（）()\[\]【】\"'`、,。.:：;；\-—_~·]", "", text)


def fingerprint(policy: Dict[str, Any]) -> str:
    """计算单条政策的内容指纹；无可用标识时返回空串。"""
    url_key = _normalize_url(policy.get("url") or "")
    if url_key:
        material = "url:" + url_key
    else:
        title_key = _normalize_title(policy.get("title") or "")
        if not title_key:
            return ""
        material = "title:" + title_key
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class PushDedupService:
    """已推送政策台账的读写入口"""

    @staticmethod
    def filter_unpushed(policies: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """过滤掉已经推送过的政策，保留从未推送过的条目。"""
        policies = list(policies or [])
        if not policies:
            return []

        fingerprints = [fp for fp in (fingerprint(p) for p in policies) if fp]
        if not fingerprints:
            return policies

        db = SessionLocal()
        try:
            rows = db.query(PushedPolicy.fingerprint).filter(
                PushedPolicy.fingerprint.in_(fingerprints)
            ).all()
            pushed = {row[0] for row in rows}
        except Exception as exc:
            # 去重表异常不应阻断报告生成，退化为不去重。
            logger.warning("推送去重查询失败，本次跳过去重: %s", exc)
            return policies
        finally:
            db.close()

        kept = [p for p in policies if fingerprint(p) not in pushed]
        skipped = len(policies) - len(kept)
        if skipped:
            logger.info("推送去重: 过滤已推送政策 %s 条，保留 %s 条", skipped, len(kept))
        return kept

    @staticmethod
    def split_by_pushed(policies: Iterable[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """把候选政策拆成 (未推送, 已推送)。

        分级推送需要两者：未推送进入主区；已推送作为背景参考，而不是
        直接丢弃——否则无新增时报告会退化为失败文案。
        """
        policies = list(policies or [])
        if not policies:
            return [], []

        fingerprints = [fp for fp in (fingerprint(p) for p in policies) if fp]
        if not fingerprints:
            return policies, []

        db = SessionLocal()
        try:
            rows = db.query(PushedPolicy.fingerprint).filter(
                PushedPolicy.fingerprint.in_(fingerprints)
            ).all()
            pushed = {row[0] for row in rows}
        except Exception as exc:
            logger.warning("推送分级查询失败，本次全部视为主区: %s", exc)
            return policies, []
        finally:
            db.close()

        unpushed = [p for p in policies if fingerprint(p) not in pushed]
        previously = [p for p in policies if fingerprint(p) in pushed]
        return unpushed, previously

    @staticmethod
    def mark_pushed(policies: Iterable[Dict[str, Any]], task_id: str = "") -> int:
        """把本次真正推送成功的政策写入台账，重复写入自动忽略。"""
        policies = list(policies or [])
        if not policies:
            return 0

        db = SessionLocal()
        recorded = 0
        try:
            for policy in policies:
                fp = fingerprint(policy)
                if not fp:
                    continue
                exists = db.query(PushedPolicy.id).filter(
                    PushedPolicy.fingerprint == fp
                ).first()
                if exists:
                    continue
                db.add(PushedPolicy(
                    fingerprint=fp,
                    url=(policy.get("url") or "")[:1000] or None,
                    title=(policy.get("title") or "")[:500] or None,
                    source_name=(policy.get("source_name") or "")[:100] or None,
                    task_id=task_id[:50] or None,
                ))
                recorded += 1
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("推送去重台账写入失败: %s", exc)
            return 0
        finally:
            db.close()

        if recorded:
            logger.info("推送去重台账新增 %s 条 task_id=%s", recorded, task_id)
        return recorded


push_dedup_service = PushDedupService()
