"""
Response Formatter
将Agent内部工具输出转换为用户友好的最终回答
"""

from typing import Dict, List, Any, Optional
from datetime import datetime
from dataclasses import dataclass
from enum import Enum


class ResponseMode(str, Enum):
    """回复模式"""
    SEARCH = "search"           # 搜索结果列表
    ANALYSIS = "analysis"       # 深度分析
    TREND = "trend"             # 趋势分析
    REPORT = "report"           # 报告生成
    DETAIL = "detail"           # 单条政策详情


@dataclass
class FormattedResponse:
    """格式化后的回复"""
    mode: ResponseMode
    summary: str              # 一句话总结
    main_content: str         # 主要内容
    details: List[Dict[str, Any]] = None  # 详细条目
    metadata: Dict[str, Any] = None       # 元数据（来源、时间等）
    suggestions: List[str] = None         # 建议/后续行动


class ResponseFormatter:
    """回答格式化器"""
    
    def __init__(self):
        pass
    
    def format(self, 
               agent_result: Dict[str, Any],
               user_query: str,
               mode: ResponseMode = None) -> FormattedResponse:
        """
        格式化Agent结果
        
        Args:
            agent_result: Agent执行结果（包含tool_calls, search_results, analysis_results等）
            user_query: 用户原始查询
            mode: 强制模式，None则自动推断
        """
        # 自动推断模式
        if mode is None:
            mode = self._infer_mode(agent_result, user_query)
        
        # 提取关键数据
        search_results = agent_result.get("search_results", [])
        analysis_results = agent_result.get("analysis_results", [])
        policy_details = agent_result.get("policy_details", [])
        historical_data = agent_result.get("historical_data", [])
        
        # 根据模式格式化
        if mode == ResponseMode.SEARCH:
            return self._format_search(search_results, user_query, agent_result)
        elif mode == ResponseMode.ANALYSIS:
            return self._format_analysis(analysis_results, policy_details, user_query)
        elif mode == ResponseMode.TREND:
            return self._format_trend(historical_data, analysis_results, search_results, user_query)
        elif mode == ResponseMode.REPORT:
            return self._format_report(agent_result, user_query)
        elif mode == ResponseMode.DETAIL:
            return self._format_detail(policy_details, analysis_results, user_query)
        else:
            return self._format_generic(agent_result, user_query)
    
    def _infer_mode(self, agent_result: Dict, user_query: str) -> ResponseMode:
        """推断回复模式"""
        q = user_query.lower()

        # 报告产物/失败是已经发生的事实，优先级高于用户措辞中的“趋势”。
        if agent_result.get("report_path") or agent_result.get("report_error"):
            return ResponseMode.REPORT
        if any(kw in q for kw in ["趋势", "变化", "历史", "走向", "演变"]):
            return ResponseMode.TREND
        elif any(kw in q for kw in ["分析", "解读", "影响", "深度", "详细"]):
            return ResponseMode.ANALYSIS
        elif any(kw in q for kw in ["报告", "日报", "周报", "月报", "总结"]):
            return ResponseMode.REPORT
        elif any(kw in q for kw in ["全文", "详情", "原文", "具体内容"]):
            return ResponseMode.DETAIL
        else:
            return ResponseMode.SEARCH
    
    #: 时间窗口的中文可读描述（把内部 date_range 转成自然语言）
    _WINDOW_LABELS = {
        "今天": "最近1天内",
        "最近": "最近7天内",
        "最近一周": "最近7天内",
        "最近7天": "最近7天内",
        "最近3天": "最近3天内",
        "最近30天": "最近30天内",
        "最近一个月": "最近30天内",
        "最近90天": "最近90天内",
        "最近3个月": "最近90天内",
        "最近一季度": "最近90天内",
        "最近一年": "最近365天内",
    }

    @classmethod
    def _describe_empty_result(cls, query: str, filters: Dict) -> str:
        """构造可定位的空结果原因说明。

        用户要求：不能只回"未找到"，必须说明查询了什么主题、什么地域、
        什么时间窗口，并给出结论句式。
        """
        region = (filters.get("region") or "").strip()
        keyword = (filters.get("keyword") or "").strip()
        date_range = (filters.get("date_range") or "").strip()

        scope_region = region or "全部地区"
        window = cls._WINDOW_LABELS.get(date_range, date_range or "未限定时间范围")

        # 有明确主题时用「主题」句式；没有关键词（笼统问"最近有什么政策"）
        # 时用"未检索到政策"句式，避免把整句问题当成政策主题。
        has_topic = bool(keyword)

        if has_topic:
            topic_line = f"- 政策主题：{keyword}"
            if window == "未限定时间范围":
                conclusion = f"{scope_region}未发布「{keyword}」相关政策"
            else:
                conclusion = f"在{window}，{scope_region}未发布「{keyword}」相关政策"
        else:
            topic_line = f"- 政策主题：未指定（按全部主题检索）"
            if window == "未限定时间范围":
                conclusion = f"{scope_region}未检索到相关政策"
            else:
                conclusion = f"在{window}，{scope_region}未检索到相关政策"

        lines = [
            "## 未检索到匹配政策",
            "",
            "**检索条件**",
            topic_line,
            f"- 地域/来源：{scope_region}",
            f"- 时间窗口：{window}",
            "",
            "**结论**",
            f"{conclusion}。",
            "",
            "**可能原因**",
            "- 该主题在该时间窗口内确实没有新政策发布",
            "- 政策标题中未包含上述关键词（可换用同义词）",
            "- 该来源近期采集异常或尚未覆盖此地区",
        ]
        return "\n".join(lines)

    def _format_search(self, results: List[Dict], query: str, agent_result: Dict) -> FormattedResponse:
        """格式化搜索结果"""
        if not results:
            filters = agent_result.get("search_filters") or {}
            detail = self._describe_empty_result(query, filters)
            return FormattedResponse(
                mode=ResponseMode.SEARCH,
                summary="未检索到匹配政策（已附检索条件说明）",
                main_content=detail,
                metadata={"query": query, "count": 0, "search_filters": filters}
            )
        
        # 统计
        total = len(results)
        official_count = sum(1 for r in results if r.get("source_confidence", 0) > 0.7)
        policy_count = sum(1 for r in results if r.get("policy_type") == "policy_document")
        
        # 生成摘要
        summary = f"找到 {total} 条相关政策（其中官方来源 {official_count} 条，政策文件 {policy_count} 条）"
        
        # 生成主要内容
        lines = [f"## {query} 相关政策", ""]

        # 输出严格按发布时间倒序（日期未知的置底），与上游评分排序解耦
        def _pub_date(r):
            t = r.get("publish_time")
            if isinstance(t, str) and t:
                try:
                    return datetime.fromisoformat(t[:10])
                except ValueError:
                    pass
            return datetime.min
        sorted_results = sorted(results, key=_pub_date, reverse=True)[:10]

        for i, r in enumerate(sorted_results, 1):
            title = r.get("title", "无标题")
            source = r.get("source_name", "未知来源")
            date = r.get("publish_time", "")[:10] if r.get("publish_time") else "日期未知"
            url = r.get("url", "")
            score = r.get("final_score", r.get("importance_score", 0))
            policy_type = r.get("policy_type", "unknown")
            
            # 类型图标
            type_icons = {
                "policy_document": "📄",
                "meeting": "🏛️",
                "interpretation": "📖",
                "news": "📰",
                "commentary": "💬",
                "announcement": "📢",
            }
            icon = type_icons.get(policy_type, "📋")
            
            lines.append(f"### {icon} {i}. {title}")
            lines.append(f"**发布单位**: {source}  |  **发布时间**: {date}  |  **匹配度**: {score:.0f}")
            if url:
                lines.append(f"**链接**: [{url}]({url})")
            lines.append("")
        
        if len(results) > 10:
            lines.append(f"... 共 {total} 条，仅显示前 10 条")
        
        # 建议
        suggestions = []
        if official_count == 0:
            suggestions.append("结果中无官方来源，建议确认关键词或尝试官方网站搜索")
        if policy_count < total * 0.5:
            suggestions.append("结果包含较多非政策内容，可尝试添加「条例」「办法」「通知」等关键词")
        
        return FormattedResponse(
            mode=ResponseMode.SEARCH,
            summary=summary,
            main_content="\n".join(lines),
            metadata={"query": query, "total": total, "official": official_count, "policy": policy_count},
            suggestions=suggestions
        )
    
    def _format_analysis(self, analysis_results: List[Dict], policy_details: List[Dict], query: str) -> FormattedResponse:
        """格式化深度分析"""
        if not analysis_results and not policy_details:
            return FormattedResponse(
                mode=ResponseMode.ANALYSIS,
                summary="未获取到可分析的政策正文",
                main_content=(
                    "## 无法完成政策分析\n\n"
                    "**缺失内容**：没有检索到可分析的政策记录。\n\n"
                    "**可能原因**：\n"
                    "- 该主题/地区在限定时间窗口内没有匹配政策\n"
                    "- 政策记录存在但正文未抓取成功（仅有标题和链接）\n"
                    "- 检索关键词与政策标题表述差异过大\n\n"
                    "可以换一个更宽的时间范围或更通用的关键词再试。"
                ),
                metadata={"query": query, "missing": ["policy_text"]}
            )
        
        # 取第一个分析结果
        analysis = analysis_results[0] if analysis_results else {}
        policy = dict(policy_details[0]) if policy_details else {}

        # 分析链路里 _bind_analysis_to_source_policy 已把权威 policy_id/url/title
        # 写回分析结果。policy_details 在纯问答场景下通常为空，若不回填，
        # 标题会退化成"未知政策"，读者无法确认分析的是哪份文件。
        for key in ("title", "url", "source_name", "publish_time"):
            if not policy.get(key) and analysis.get(key):
                policy[key] = analysis[key]

        title = policy.get("title") or analysis.get("title") or "（政策标题缺失）"
        summary = analysis.get("一句话总结") or title

        lines = ["## 政策深度分析", "", f"### {title}", ""]

        # 核心信息
        if policy.get("source_name"):
            lines.append(f"**发布单位**: {policy['source_name']}")
        if policy.get("publish_time"):
            lines.append(f"**发布时间**: {str(policy['publish_time'])[:10]}")
        if policy.get("url"):
            lines.append(f"**原文链接**: [{policy['url']}]({policy['url']})")
        lines.append("")

        # 政策文本缺失时明确说明，而不是用标题链接冒充分析
        if not analysis and not policy.get("content"):
            lines.append("> **无法完成深度分析**：未获取到该政策的正文内容，")
            lines.append("> 因此只能提供标题与链接，不能给出实质性影响判断。")
            lines.append("> 常见原因：正文抓取失败、页面需登录、或来源仅提供文件下载。")
            lines.append("")
        
        # 分析内容
        sections = [
            ("核心内容", analysis.get("一句话总结")),
            ("大白话解释", analysis.get("政策大白话解释")),
            ("国家释放的信号", analysis.get("国家释放的信号")),
        ]
        
        for label, content in sections:
            if content:
                lines.append(f"### {label}")
                lines.append(content)
                lines.append("")
        
        # 影响行业
        industries = analysis.get("影响行业", [])
        if industries:
            lines.append("### 影响行业")
            for ind in industries:
                lines.append(f"- **{ind.get('行业', '')}** ({ind.get('影响程度', '')}): {ind.get('原因', '')}")
            lines.append("")
        
        # 未来机会
        opportunities = analysis.get("未来机会", [])
        if opportunities:
            lines.append("### 值得关注的机会")
            for opp in opportunities:
                lines.append(f"- {opp}")
            lines.append("")
        
        # 个人建议
        advice = analysis.get("普通大学生建议", [])
        if advice:
            lines.append("### 行动建议")
            for a in advice:
                lines.append(f"- {a}")
            lines.append("")
        
        # 趋势评分
        score = analysis.get("趋势评分", 0)
        if score:
            lines.append(f"### 趋势评分: **{score}/100**")
            lines.append("")
        
        return FormattedResponse(
            mode=ResponseMode.ANALYSIS,
            summary=summary,
            main_content="\n".join(lines),
            metadata={"query": query, "trend_score": score}
        )
    
    def _format_trend(self, historical: List[Dict], analysis_results: List[Dict], 
                      search_results: List[Dict], query: str) -> FormattedResponse:
        """格式化趋势分析"""
        if not historical and not analysis_results:
            return FormattedResponse(
                mode=ResponseMode.TREND,
                summary="数据不足，无法进行趋势分析",
                main_content="建议先收集更多历史政策数据",
                metadata={"query": query}
            )
        
        summary = f"基于 {len(historical)} 条历史政策的趋势分析"
        
        lines = [f"## {query} 趋势分析", ""]
        
        # 统计
        by_year = {}
        by_category = {}
        for h in historical:
            pub_time = h.get("publish_time")
            if pub_time:
                try:
                    year = pub_time[:4] if isinstance(pub_time, str) else pub_time.year
                    by_year[year] = by_year.get(year, 0) + 1
                except Exception:
                    pass
            
            cat = h.get("category", "未分类")
            by_category[cat] = by_category.get(cat, 0) + 1
        
        # 年度分布
        if by_year:
            lines.append("### 📅 年度政策数量分布")
            for year in sorted(by_year.keys()):
                lines.append(f"- **{year}年**: {by_year[year]} 条")
            lines.append("")
        
        # 分类分布
        if by_category:
            lines.append("### 📂 政策分类分布")
            for cat, count in sorted(by_category.items(), key=lambda x: -x[1]):
                lines.append(f"- **{cat}**: {count} 条")
            lines.append("")
        
        # 关键转折点
        if analysis_results:
            analysis = analysis_results[0]
            lines.append("### 🎯 关键趋势判断")
            lines.append(analysis.get("国家释放的信号", "暂无"))
            lines.append("")
            
            opportunities = analysis.get("未来机会", [])
            if opportunities:
                lines.append("### 💡 潜在机会")
                for opp in opportunities:
                    lines.append(f"- {opp}")
                lines.append("")
        
        # 数据来源说明
        lines.append("---")
        lines.append(f"*数据来源: {len(historical)} 条历史政策记录*")
        
        return FormattedResponse(
            mode=ResponseMode.TREND,
            summary=summary,
            main_content="\n".join(lines),
            metadata={"query": query, "historical_count": len(historical)}
        )
    
    def _format_report(self, agent_result: Dict, query: str) -> FormattedResponse:
        """格式化报告生成结果，并明确区分成功、不完整和失败。

        generate_report 是同步调用；没有 report_path 时并不存在后台任务，
        因此不能回复“生成中”。数据缺失与生成失败也必须分开，否则用户会
        把一份分析不完整的报告误认为完整交付物。
        """
        report_path = agent_result.get("report_path")
        report_error = agent_result.get("report_error")
        degraded = agent_result.get("degraded_analyses") or []
        failures = agent_result.get("analysis_failures") or []
        analysis_count = len(agent_result.get("analysis_results") or [])
        has_data_gap = bool(degraded or failures)

        if report_path:
            lines = [
                f"📄 **报告文件**: {report_path}",
                "",
                "可前往查看完整内容。",
            ]
            if has_data_gap:
                lines.extend([
                    "",
                    "### ⚠️ 数据完整性提示",
                    f"- 本次报告使用了 {analysis_count} 条可信分析。",
                    f"- {len(failures)} 条政策分析失败，未纳入报告。",
                    f"- {len(degraded)} 条政策仅有标题级降级结果，未纳入正式报告。",
                    "- 建议对关键政策查看原文后再作判断。",
                ])
            return FormattedResponse(
                mode=ResponseMode.REPORT,
                summary="报告已生成" + ("（数据不完整）" if has_data_gap else ""),
                main_content="\n".join(lines),
                metadata={
                    "query": query,
                    "report_path": report_path,
                    "analysis_count": analysis_count,
                    "degraded_count": len(degraded),
                    "failure_count": len(failures),
                },
            )

        if report_error:
            return FormattedResponse(
                mode=ResponseMode.REPORT,
                summary="报告生成失败",
                main_content=f"**原因**：{report_error}\n\n请检查数据采集、AI 配置和报告目录权限后重试。",
                metadata={"query": query, "report_error": report_error},
            )

        return FormattedResponse(
            mode=ResponseMode.REPORT,
            summary="报告生成未完成",
            main_content="未产生报告文件，请检查执行记录后重试。",
            metadata={"query": query},
        )

    def _format_detail(self, policy_details: List[Dict], analysis_results: List[Dict], query: str) -> FormattedResponse:
        """格式化单条政策详情"""
        if not policy_details:
            return FormattedResponse(
                mode=ResponseMode.DETAIL,
                summary="未找到政策详情",
                main_content="请提供政策URL或ID",
                metadata={"query": query}
            )
        
        policy = policy_details[0]
        analysis = analysis_results[0] if analysis_results else {}
        
        summary = policy.get("title", "政策详情")
        
        lines = [f"## {summary}", ""]
        
        fields = [
            ("发布单位", "source_name"),
            ("政府级别", "government_level"),
            ("发布部门", "department"),
            ("所在城市", "city"),
            ("发布时间", "publish_time"),
            ("原文链接", "url"),
        ]
        
        for label, key in fields:
            val = policy.get(key)
            if val:
                if key == "url":
                    lines.append(f"**{label}**: [{val}]({val})")
                elif key == "publish_time":
                    lines.append(f"**{label}**: {str(val)[:10]}")
                else:
                    lines.append(f"**{label}**: {val}")
        
        lines.append("")
        
        # 全文内容
        content = policy.get("content", "")
        if content:
            lines.append("### 📄 政策全文")
            lines.append(content[:3000] + ("..." if len(content) > 3000 else ""))
            lines.append("")
        
        # 分析结果
        if analysis:
            lines.append("### 🔍 AI 分析")
            for label, key in [("核心摘要", "一句话总结"), ("大白话解释", "政策大白话解释"), 
                               ("国家信号", "国家释放的信号"), ("风险提示", "风险分析")]:
                val = analysis.get(key)
                if val:
                    lines.append(f"**{label}**: {val}")
                    lines.append("")
        
        return FormattedResponse(
            mode=ResponseMode.DETAIL,
            summary=summary,
            main_content="\n".join(lines),
            metadata={"query": query, "has_analysis": bool(analysis)}
        )
    
    def _format_generic(self, agent_result: Dict, query: str) -> FormattedResponse:
        """通用格式化"""
        return FormattedResponse(
            mode=ResponseMode.SEARCH,
            summary="查询完成",
            main_content=agent_result.get("answer", "处理完成"),
            metadata={"query": query}
        )
    
    def to_markdown(self, response: FormattedResponse) -> str:
        """转换为Markdown字符串"""
        parts = []
        
        # 标题摘要
        parts.append(f"## {response.summary}")
        parts.append("")
        
        # 主要内容
        if response.main_content:
            parts.append(response.main_content)
            parts.append("")
        
        # 详细条目
        if response.details:
            for detail in response.details:
                parts.append(f"- {detail}")
            parts.append("")
        
        # 建议
        if response.suggestions:
            parts.append("### 💡 建议")
            for s in response.suggestions:
                parts.append(f"- {s}")
            parts.append("")
        
        return "\n".join(parts)


# 全局格式化器实例
response_formatter = ResponseFormatter()


def format_agent_response(agent_result: Dict[str, Any], user_query: str) -> str:
    """便捷函数：格式化Agent结果为Markdown"""
    formatted = response_formatter.format(agent_result, user_query)
    return response_formatter.to_markdown(formatted)
