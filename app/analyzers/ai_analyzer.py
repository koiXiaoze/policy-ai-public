# -*- coding: utf-8 -*-
"""
AI 政策分析器
使用 OpenAI 兼容 API 进行结构化分析
"""

import json
import re
import time
import random
from typing import Optional

from openai import OpenAI, RateLimitError, APIError, APIConnectionError

from app.config import settings
from app.logger import logger

ANALYSIS_PROMPT = """你是一个政策分析专家，请对给定的政策全文进行深度分析，输出结构化 JSON。

要求：
1. 输出必须是合法的 JSON 对象
2. 不要包含任何额外的文本、解释或 markdown 代码块
3. 所有字段都要填写，不要留空

输出格式：
{
  "一句话总结": "政策核心内容的单句概括",
  "政策大白话解释": "用通俗易懂的语言解释政策含义、背景、影响",
  "国家释放的信号": "国家通过该政策释放了什么战略信号、方向",
  "影响行业": [
    {"行业": "行业名称", "影响程度": "高/中/低", "原因": "具体影响原因"}
  ],
  "未来机会": ["机会1", "机会2", "机会3"],
  "普通大学生建议": ["建议1", "建议2", "建议3"],
  "趋势评分": 0-100
}

重点关注：人工智能、数字经济、新能源、先进制造、网络安全、数据安全、低空经济、机器人、生物医药、新材料、教育、就业、住房、医疗、养老、人才政策、社会保障等领域。
"""

ANALYSIS_TITLE_ONLY_PROMPT = """你是一个政策分析专家，仅根据政策标题进行推测性分析，输出结构化 JSON。

要求：
1. 输出必须是合法的 JSON 对象
2. 明确标注这是基于标题的推测分析
3. 所有字段都要填写

输出格式同完整版。
"""


def _ensure_utf8(text):
    """修复包含 U+FFFD 的典型 UTF-8 误解码文本。

    普通 ASCII 问号 ``?`` 是合法字符，不能把它当作乱码触发转码；
    只有明确出现替换字符 U+FFFD 时才尝试 latin-1 -> UTF-8 修复。
    """
    if not text:
        return text
    if '\ufffd' not in text:
        return text
    try:
        return text.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _runtime_limits():
    """返回经过边界收敛的 AI 时延配置。

    配置错误不应把定时任务拖入数小时的长尾等待：超时至少 1 秒，
    重试最多 5 次，慢请求阈值至少 1 秒。配置本身仍由 pydantic 保证
    基本类型正确，这里负责业务边界。
    """
    timeout = max(1.0, float(settings.ai_request_timeout_seconds))
    max_retries = max(0, min(int(settings.ai_max_retries), 5))
    slow_threshold = max(1.0, float(settings.ai_slow_threshold_seconds))
    return timeout, max_retries, slow_threshold


def _create_openai_client():
    """创建不使用 SDK 内部重试的客户端，避免与业务重试叠加。"""
    timeout, _, _ = _runtime_limits()
    return OpenAI(
        api_key=settings.ai_api_key,
        base_url=settings.ai_api_base,
        timeout=timeout,
        max_retries=0,
    )


def _is_json_mode_unsupported_error(exc):
    """只识别“当前 API 不支持 JSON mode”这一类错误。

    普通超时、限流和服务端错误必须继续走重试；在降级前错误地重试
    JSON mode 会放大时延，因此该判断同时用于立即降级和避免无效重试。
    """
    message = str(exc).lower()
    markers = (
        'response_format',
        'json_object',
        'json mode',
        'json_mode',
    )
    return any(marker in message for marker in markers)


def _call_with_retry(
    client,
    kwargs,
    max_retries=2,
    base_delay=2.0,
    slow_threshold_seconds=60.0,
    operation='AI',
):
    """执行单次请求并显式记录耗时，重试次数只由本函数控制。

    ``max_retries`` 表示首次失败后的额外重试次数，总请求次数为
    ``max_retries + 1``。JSON mode 不受支持时立即抛出，由调用方切换普通
    模式，不能先按普通 API 错误重试多次。
    """
    attempts = max_retries + 1
    for attempt in range(1, attempts + 1):
        started_at = time.perf_counter()
        logger.info('%s 请求开始 (%s/%s)', operation, attempt, attempts)
        try:
            response = client.chat.completions.create(**kwargs)
        except RateLimitError as exc:
            elapsed = time.perf_counter() - started_at
            logger.warning(
                '%s 限流 (429)，耗时 %.2fs (%s/%s): %s',
                operation, elapsed, attempt, attempts, str(exc)[:100],
            )
            if attempt < attempts:
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
                logger.warning(
                    '%s 将在 %.1fs 后重试', operation, delay
                )
                time.sleep(delay)
                continue
            raise
        except (APIError, APIConnectionError) as exc:
            elapsed = time.perf_counter() - started_at
            logger.warning(
                '%s API 错误，耗时 %.2fs (%s/%s): %s',
                operation, elapsed, attempt, attempts, str(exc)[:100],
            )
            if _is_json_mode_unsupported_error(exc):
                raise
            if attempt < attempts:
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
                logger.warning(
                    '%s 将在 %.1fs 后重试', operation, delay
                )
                time.sleep(delay)
                continue
            raise

        elapsed = time.perf_counter() - started_at
        log_method = (
            logger.warning
            if elapsed >= slow_threshold_seconds
            else logger.info
        )
        log_method(
            '%s 请求完成，耗时 %.2fs (%s/%s)',
            operation, elapsed, attempt, attempts,
        )
        return response

    return None


ANALYSIS_SOURCE_FULL_TEXT = "full_text"
ANALYSIS_SOURCE_TITLE_ONLY = "title_only"
ANALYSIS_SOURCE_FALLBACK = "fallback"

ANALYSIS_STATUS_SUCCESS = "success"
ANALYSIS_STATUS_DEGRADED = "degraded"
ANALYSIS_STATUS_FAILED = "failed"


def _fallback_result(title):
    """最后兜底：构造最小有效结构，保留给人工查看，但不能作为正式分析。"""
    return {
        '一句话总结': '政策标题：' + title,
        '政策大白话解释': 'AI 分析暂时不可用，请人工查阅原文',
        '国家释放的信号': '待人工分析',
        '影响行业': [],
        '未来机会': [],
        '普通大学生建议': ['关注官方原文', '留意后续解读'],
        '趋势评分': 0,
    }


def _with_analysis_metadata(result, source, status, error=None):
    """附加内部可信度元数据，不改变业务 JSON 的顶层约定。

    _analysis_* 字段只在进程内用于区分正式分析、标题推测和失败兜底；
    落库、知识库和用户可见输出前必须由 strip_analysis_metadata 剥离。
    """
    payload = dict(result) if isinstance(result, dict) else _fallback_result('')
    payload['_analysis_source'] = source
    payload['_analysis_status'] = status
    if error:
        payload['_analysis_error'] = str(error)[:500]
    return payload


def strip_analysis_metadata(result):
    """返回不含内部元数据的浅拷贝，避免修改调用方传入的字典。"""
    if not isinstance(result, dict):
        return result
    return {key: value for key, value in result.items()
            if not str(key).startswith('_analysis_')}


def is_trusted_analysis(result):
    """旧版无元数据结果按全文成功处理；只有全文成功可进入正式链路。"""
    if not isinstance(result, dict) or result.get('error'):
        return False
    status = result.get('_analysis_status', ANALYSIS_STATUS_SUCCESS)
    source = result.get('_analysis_source', ANALYSIS_SOURCE_FULL_TEXT)
    return status == ANALYSIS_STATUS_SUCCESS and source == ANALYSIS_SOURCE_FULL_TEXT


def _parse_json_object(raw):
    """从模型输出中提取完整 JSON 对象。

    模型经常在 JSON 前后附带解释文字或 markdown 代码围栏。不能用正则
    匹配到第一个右花括号，否则 ``影响行业`` 这类嵌套数组会让对象被截断。
    这里先用标准 JSON 解码器解析完整文本，再逐个尝试代码围栏和正文中的
    ``{`` 起点，最终只接受顶层为对象的 JSON。
    """
    if not raw:
        return None, 'AI 返回为空'
    raw = _ensure_utf8(raw).strip()
    if not raw:
        return None, 'AI 返回为空'
    decoder = json.JSONDecoder()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    else:
        if isinstance(parsed, dict):
            return parsed, None
        return None, f'AI 返回 JSON 顶层类型无效: {type(parsed).__name__}'

    # 代码围栏内通常就是完整 JSON；逐个尝试，避免某个无效围栏导致整体失败。
    for match in re.finditer(r'```(?:json)?\s*(.*?)\s*```', raw,
                             re.DOTALL | re.IGNORECASE):
        candidate = match.group(1).strip()
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            try:
                parsed, _ = decoder.raw_decode(candidate)
            except json.JSONDecodeError:
                continue
        if isinstance(parsed, dict):
            return parsed, None

    # 正文夹杂解释时，从每个可能的对象起点尝试完整解码。raw_decode 会
    # 按 JSON 层级消费到匹配的右花括号，因此不会截断嵌套对象。
    last_error = None
    for index, char in enumerate(raw):
        if char != '{':
            continue
        try:
            parsed, _ = decoder.raw_decode(raw, index)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(parsed, dict):
            return parsed, None

    if last_error:
        return None, f'AI 返回 JSON 解析失败: {last_error}'
    return None, 'AI 返回非 JSON 对象'


def _by_title_failed(title, message):
    return _with_analysis_metadata(
        _fallback_result(title), ANALYSIS_SOURCE_FALLBACK,
        ANALYSIS_STATUS_FAILED, message,
    )


def _analyze_by_title(title):
    """仅根据标题进行分析；成功也只能标记为降级结果。"""
    if not title or len(title) < 5:
        logger.warning('标题太短无法分析')
        return _by_title_failed(title, '标题过短，无法进行标题推测分析')

    title = _ensure_utf8(title)

    try:
        _, max_retries, slow_threshold = _runtime_limits()
        client = _create_openai_client()
        logger.info('标题分析: %s...', title[:30])

        kwargs = {
            'model': settings.ai_model,
            'messages': [
                {'role': 'system', 'content': ANALYSIS_TITLE_ONLY_PROMPT},
                {'role': 'user', 'content': '政策标题：' + title},
            ],
            'temperature': 0.3,
        }

        try:
            kwargs['response_format'] = {'type': 'json_object'}
            response = _call_with_retry(
                client,
                kwargs,
                max_retries=max_retries,
                slow_threshold_seconds=slow_threshold,
                operation='标题分析',
            )
        except Exception as json_err:
            if not _is_json_mode_unsupported_error(json_err):
                raise
            logger.info('API 不支持 JSON mode，标题分析退回普通模式')
            kwargs.pop('response_format', None)
            response = _call_with_retry(
                client,
                kwargs,
                max_retries=max_retries,
                slow_threshold_seconds=slow_threshold,
                operation='标题分析(普通模式)',
            )

        raw = response.choices[0].message.content
        result, parse_error = _parse_json_object(raw)
        if parse_error:
            logger.error('标题分析失败: %s - %s', title[:30], parse_error)
            return _by_title_failed(title, parse_error)

        logger.info('标题分析完成: %s (评分 %s)', title[:30], result.get('趋势评分', 0))
        return _with_analysis_metadata(
            result, ANALYSIS_SOURCE_TITLE_ONLY, ANALYSIS_STATUS_DEGRADED,
        )

    except Exception as e:
        logger.error('标题分析失败: %s - %s', title[:30], e)
        return _by_title_failed(title, f'标题分析异常: {e}')


def _full_text_failed_to_title(title, reason):
    """全文失败后退到标题分析，并把本次全文失败原因附到结果上。"""
    logger.warning('全文分析失败，降级为标题分析: %s - %s', title[:30], reason)
    result = _analyze_by_title(title)
    existing_error = result.get('_analysis_error')
    combined = f'全文分析失败: {reason}'
    if existing_error:
        combined += f'；{existing_error}'
    result['_analysis_error'] = combined[:500]
    return result


def analyze_policy(content, title=''):
    """分析政策全文，返回带内部可信度元数据的结构化字典（绝不返回 None）。"""
    if not content or len(content.strip()) < 50:
        logger.warning('正文过短，转标题推测分析: %s', title[:30])
        return _analyze_by_title(title)

    content = _ensure_utf8(content)
    title = _ensure_utf8(title)

    try:
        _, max_retries, slow_threshold = _runtime_limits()
        client = _create_openai_client()
        logger.info('正在分析: %s...', title[:30])
        logger.info('API: %s | 模型: %s', settings.ai_api_base, settings.ai_model)

        kwargs = {
            'model': settings.ai_model,
            'messages': [
                {'role': 'system', 'content': ANALYSIS_PROMPT},
                {'role': 'user', 'content': '政策标题：' + title + '\n\n政策全文：' + content[:8000]},
            ],
            'temperature': 0.3,
        }

        try:
            kwargs['response_format'] = {'type': 'json_object'}
            response = _call_with_retry(
                client,
                kwargs,
                max_retries=max_retries,
                slow_threshold_seconds=slow_threshold,
                operation='全文分析',
            )
        except Exception as json_err:
            if not _is_json_mode_unsupported_error(json_err):
                raise
            logger.info('API 不支持 JSON mode，全文分析退回普通模式')
            kwargs.pop('response_format')
            response = _call_with_retry(
                client,
                kwargs,
                max_retries=max_retries,
                slow_threshold_seconds=slow_threshold,
                operation='全文分析(普通模式)',
            )

        raw = response.choices[0].message.content
        result, parse_error = _parse_json_object(raw)
        if parse_error:
            return _full_text_failed_to_title(title, parse_error)

        logger.info('分析成功: %s', title[:30])
        return _with_analysis_metadata(
            result, ANALYSIS_SOURCE_FULL_TEXT, ANALYSIS_STATUS_SUCCESS,
        )

    except Exception as e:
        logger.error('AI 分析失败: %s - %s', title[:30], e)
        return _full_text_failed_to_title(title, f'全文分析异常: {e}')
