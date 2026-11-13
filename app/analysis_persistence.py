# -*- coding: utf-8 -*-
"""政策分析结果的可信校验、字段映射与持久化。

workflow（CLI/日报）和 Agent 都使用同一套分析 JSON 结构。此前两条链路
各自维护中文字段映射与知识库同步，已经出现过字段名漂移和非可信结果落库。
本模块把这类规则收口到一个位置，避免再次分叉。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from app.analyzers.ai_analyzer import is_trusted_analysis, strip_analysis_metadata
from app.logger import logger
from app.models.policy import Analysis


def prepare_trusted_analysis(result: Any) -> Optional[Dict[str, Any]]:
    """返回可正式落库的干净分析结果；不可信结果返回 None。"""
    if not isinstance(result, dict) or not is_trusted_analysis(result):
        return None
    clean_result = strip_analysis_metadata(result)
    return clean_result if isinstance(clean_result, dict) else None


def serialize_analysis_result(result: Dict[str, Any]) -> str:
    """把分析结果序列化成合法 JSON，供 Analysis.raw_response 使用。"""
    return json.dumps(result, ensure_ascii=False)


def apply_analysis_fields(analysis: Analysis, result: Dict[str, Any]) -> None:
    """把唯一的 LLM 中文字段契约映射到 Analysis 模型字段。"""
    analysis.summary = result.get("一句话总结", "")
    analysis.plain_explanation = result.get("政策大白话解释", "")
    analysis.national_direction = result.get("国家释放的信号", "")
    analysis.industry_impact = result.get("影响行业", [])
    analysis.career_opportunity = result.get("未来机会", [])
    analysis.personal_advice = result.get("普通大学生建议", [])
    analysis.risk_analysis = result.get("风险分析", "")
    analysis.trend_score = result.get("趋势评分", 0) or 0
    analysis.raw_response = serialize_analysis_result(result)


def upsert_analysis(db, policy, result: Any) -> Optional[Analysis]:
    """在调用方事务内新增或更新一条可信分析，不执行 commit。

    Args:
        db: 调用方持有的 SQLAlchemy Session。
        policy: 已解析出的 Policy ORM 对象，不能使用爬虫的原始 id 伪造。
        result: analyze_policy 返回的结果，可带内部 _analysis_* 元数据。

    Returns:
        新增或更新的 Analysis；不可信结果返回 None。
    """
    clean_result = prepare_trusted_analysis(result)
    if clean_result is None:
        return None
    if policy is None or getattr(policy, "id", None) is None:
        return None

    analysis = db.query(Analysis).filter(Analysis.policy_id == policy.id).first()
    if analysis is None:
        analysis = Analysis(policy_id=policy.id)
        db.add(analysis)
    apply_analysis_fields(analysis, clean_result)
    # policy 表是列表、搜索和排序的主要读取入口；Analysis 落库后必须同步
    # 趋势评分，否则会出现详情有评分、列表却始终显示 0 的双份事实。
    policy.importance_score = analysis.trend_score
    return analysis


def sync_analysis_to_knowledge_base(policy, result: Any) -> bool:
    """同步一条可信分析到知识库，失败只记日志并返回 False。

    这里再次做可信校验和元数据剥离，作为跨调用链的防御边界：即使未来新增
    调用方忘记预处理，知识库也不会保存降级/失败结果或 _analysis_* 内部字段。
    """
    clean_result = prepare_trusted_analysis(result)
    if clean_result is None:
        logger.debug("跳过不可信分析结果的知识库同步: %s", getattr(policy, "url", ""))
        return False

    try:
        from app.data_layer import knowledge_base
        from app.data_layer.source import PolicyItem, SourceType

        # add_policy 是 append 语义，按 URL 去重可避免日报重跑和双路径同步产生副本。
        if any(entry.url == policy.url for entry in knowledge_base.entries):
            logger.debug("知识库已存在该政策，跳过: %s", (policy.url or "")[:60])
            return False

        policy_item = PolicyItem(
            title=policy.title,
            url=policy.url,
            source_name=policy.source_name,
            government_level=policy.government_level,
            department=policy.department,
            city=policy.city,
            publish_time=policy.publish_time,
            content=policy.content,
            summary=clean_result.get("一句话总结", ""),
            keywords=",".join(
                item.get("行业", "")
                for item in clean_result.get("影响行业", [])
                if isinstance(item, dict) and item.get("行业")
            ),
            importance_score=clean_result.get("趋势评分", 0),
            source_type=SourceType.LOCAL_DB,
            raw_data={"policy_id": policy.id, "analysis": clean_result},
        )
        knowledge_base.add_policy(policy_item)
        return True
    except Exception as exc:
        logger.warning(
            "知识库同步失败（不影响分析结果）: %s",
            exc,
        )
        return False