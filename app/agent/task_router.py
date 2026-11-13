"""长任务识别与回执文案

长任务异步执行的第一步是判断"这条消息要不要走后台"。
本模块提供纯函数判断，不触碰 Agent Core、Scheduler 或任何工具。

判定规则保持静态可读：意图关键词 + 中文时间跨度关键词。
这样在没有 LLM、没有网络的降级环境下也能工作。
"""

from typing import Iterable, Optional


#: 命中即视为长任务。趋势类和报告类天然需要跨年份检索与多次 LLM 分析。
LONG_TASK_KEYWORDS = (
    "趋势",
    "报告",
    "深度分析",
    "汇总",
    "历年",
    "过去三年",
    "最近三年",
    "过去五年",
    "最近五年",
    "全量",
    "全部政策",
)

#: Planner 输出的意图与长任务的映射，供未来把判断下沉到 Agent Core 使用。
LONG_TASK_INTENTS = frozenset({"trend", "report"})


def is_long_task(content: str, intents: Optional[Iterable[str]] = None) -> bool:
    """判断一条自然语言消息是否属于长任务

    Args:
        content: 用户原始消息文本。
        intents: 可选的已解析意图；给出时按意图判定，未给出时退回关键词。
    """
    if intents:
        return bool(set(intents) & set(LONG_TASK_INTENTS))

    text = (content or "").lower()
    return any(keyword in text for keyword in LONG_TASK_KEYWORDS)


def build_ack_message(task_id: str) -> str:
    """构造立即回执，让用户知道任务已进入后台队列"""
    return (
        f"已收到，正在后台分析（任务编号 {task_id}）。\n"
        "分析完成后我会把结果直接发在这个会话里。"
    )


def build_rejected_message(reason: str) -> str:
    """构造排队失败的回执，让用户知道为什么没有开始执行"""
    return f"任务暂时无法开始：{reason}\n请稍后再发送一次。"
