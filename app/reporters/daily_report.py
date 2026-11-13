"""
日报/周报/月报/季报 生成模块
"""

from datetime import datetime, timedelta
from typing import List, Optional
from pathlib import Path
import yaml
from app.logger import logger


REPORT_MODE_META = {
    "daily": {
        "title": "政策趋势日报",
        "period_label": "日期",
        "section_label": "今日重要政策",
        "empty_text": "今日无重要政策更新。",
        "period_note": "",
    },
    "weekly": {
        "title": "政策趋势周报",
        "period_label": "周期",
        "section_label": "本周重要政策",
        "empty_text": "本周期无重要政策更新。",
        "period_note": "> 说明：当前为周期政策汇总，趋势聚合尚未完整实现。以下内容按本周期政策重要性排序，不代表完整的跨期趋势结论。",
    },
    "monthly": {
        "title": "政策趋势月报",
        "period_label": "周期",
        "section_label": "本月重要政策",
        "empty_text": "本月无重要政策更新。",
        "period_note": "> 说明：当前为周期政策汇总，趋势聚合尚未完整实现。以下内容按本周期政策重要性排序，不代表完整的跨期趋势结论。",
    },
    "quarterly": {
        "title": "政策趋势季报",
        "period_label": "周期",
        "section_label": "本季度重要政策",
        "empty_text": "本季度无重要政策更新。",
        "period_note": "> 说明：当前为周期政策汇总，趋势聚合尚未完整实现。以下内容按本周期政策重要性排序，不代表完整的跨期趋势结论。",
    },
}


def _report_period(now: datetime, mode: str) -> str:
    """按报告模式返回人类可读的统计周期。"""
    if mode == "weekly":
        monday = now - timedelta(days=now.weekday())
        sunday = monday + timedelta(days=6)
        return f"{monday:%Y年%m月%d日} - {sunday:%Y年%m月%d日}"
    if mode == "monthly":
        first = now.replace(day=1)
        next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        last = next_month - timedelta(days=1)
        return f"{first:%Y年%m月%d日} - {last:%Y年%m月%d日}"
    if mode == "quarterly":
        quarter = (now.month - 1) // 3 + 1
        return f"{now.year}年第{quarter}季度"
    return now.strftime("%Y年%m月%d日")


def load_keywords() -> dict:
    with open("config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config.get("keywords", {})


def build_source_health_section() -> str:
    """生成日报末尾的来源健康摘要。

    失败时返回空串，绝不影响正常报告生成。
    """
    try:
        from app.services.source_health import source_health_service
        summary = source_health_service.summary()
    except Exception as exc:
        logger.warning("读取来源健康摘要失败: %s", exc)
        return ""

    sources = summary.get("sources") or []
    if not sources:
        return ""

    status_label = {
        "ok": "正常",
        "empty": "无更新",
        "degraded": "降级（使用旧数据）",
        "failed": "失败",
    }
    freshness_label = {
        "fresh": "数据新鲜",
        "stale": "数据陈旧",
        "unknown": "新鲜度未知",
    }

    def _age_text(hours):
        if hours is None:
            return "—"
        hours = float(hours)
        if hours >= 24:
            return f"{hours / 24:.1f} 天"
        return f"{hours:.1f} 小时"

    lines = ["---", "## 数据源采集状态", ""]
    for item in sources:
        status = status_label.get(item.get("status"), item.get("status") or "未知")
        freshness = freshness_label.get(
            item.get("freshness_status"),
            item.get("freshness_status") or "新鲜度未知",
        )
        newest = item.get("newest_publish_time")
        newest_text = newest[:10] if newest else "—"
        last_new = item.get("last_new_policy_at")
        last_new_text = last_new[:10] if last_new else "—"
        line = (
            f"- **{item.get('source_name')}**：{status}；{freshness}"
            f"（{item.get('item_count', 0)} 条，政策日期 {newest_text}）"
        )
        line += f"；最近发现新政策 {last_new_text}"
        if item.get("stale_hours") is not None:
            line += f"（距今 {_age_text(item['stale_hours'])}）"
        threshold = item.get("stale_threshold_hours")
        if threshold is not None:
            line += f"；陈旧阈值 {float(threshold):g} 小时"
        if item.get("consecutive_failures"):
            line += f"，连续失败 {item['consecutive_failures']} 次"
        error_type = item.get("error_type")
        if error_type:
            line += f"；错误类型：{error_type}"
        error = item.get("error")
        if error:
            line += f"；原因：{error}"
        recent_checks = item.get("recent_check_count")
        success_rate = item.get("success_rate")
        if recent_checks and success_rate is not None:
            line += f"；最近 {recent_checks} 次成功率 {success_rate}%"
        last_live_success = item.get("last_live_success_at")
        if last_live_success:
            line += f"；最近在线成功 {last_live_success[:10]}"
        last_recovery = item.get("last_recovery_at")
        if last_recovery:
            line += f"；最近恢复 {last_recovery[:10]}"
        lines.append(line)

    failing = summary.get("failing_sources") or []
    degraded = summary.get("degraded_sources") or []
    stale = summary.get("stale_sources") or []
    if stale:
        lines.append("")
        lines.append(f"> ⚠️ 以下来源长期未发现新政策，内容可能陈旧：{'、'.join(stale)}")
    if failing:
        lines.append("")
        lines.append(f"> ⚠️ 以下来源连续失败，内容可能缺失：{'、'.join(failing)}")
    if degraded:
        lines.append("")
        lines.append(f"> ⚠️ 以下来源在线采集失败，本次使用旧数据：{'、'.join(degraded)}")
    lines.append("")
    return "\n".join(lines)


def _build_background_section(background_policies: List[dict],
                              primary_policies: Optional[List[dict]] = None) -> str:
    """背景参考区：简要列出此前已推送过的政策，避免重复展开。

    primary_policies 用于去重：同一政策若已进入主区，就不得再出现在背景区，
    否则读者会看到同一条政策被展示两遍。
    """
    primary_keys = set()
    for item in primary_policies or []:
        policy = item.get("policy", item) if isinstance(item, dict) else item
        if not isinstance(policy, dict):
            continue
        key = (policy.get("url") or "").strip() or (policy.get("title") or "").strip()
        if key:
            primary_keys.add(key)

    rows = []
    seen = set()
    for item in background_policies or []:
        policy = item.get("policy", item) if isinstance(item, dict) else item
        if not isinstance(policy, dict):
            continue
        title = policy.get("title") or ""
        url = policy.get("url") or ""
        if not title:
            continue
        key = url.strip() or title.strip()
        if key in primary_keys or key in seen:
            continue
        seen.add(key)
        rows.append((title, url))

    if not rows:
        return ""

    lines = ["---", "## 背景参考（此前已推送）", ""]
    lines.append("以下政策已在此前日报中推送过，本轮仅保留索引，不重复展开：")
    lines.append("")
    for title, url in rows[:10]:
        if url:
            lines.append(f"- [{title}]({url})")
        else:
            lines.append(f"- {title}")
    if len(rows) > 10:
        lines.append(f"- 其余 {len(rows) - 10} 条略")
    lines.append("")
    return "\n".join(lines)


def generate_daily_report(policies_with_analysis: List[dict], source_name: str = "国务院",
                          include_source_health: bool = True,
                          background_policies: Optional[List[dict]] = None,
                          mode: str = "daily",
                          top_n: Optional[int] = None) -> str:
    """生成日报、周报、月报或季报。

    函数名为兼容既有 CLI/workflow 保留；mode 决定标题、周期与空状态，
    top_n 决定正式展示上限。未传 top_n 时读取统一配置。
    """
    def field(policy, name, default=""):
        """兼容两种入参：ORM Policy 对象（CLI / workflow）与 dict（Agent Service）。"""
        if isinstance(policy, dict):
            return policy.get(name, default)
        return getattr(policy, name, default)

    from app.config import MAX_REPORT_TOP_N, get_report_top_n

    normalized_mode = mode if mode in REPORT_MODE_META else "daily"
    meta = REPORT_MODE_META[normalized_mode]
    requested_top_n = get_report_top_n() if top_n is None else int(top_n)
    report_top_n = max(1, min(requested_top_n, MAX_REPORT_TOP_N))
    now = datetime.now()
    period_text = _report_period(now, normalized_mode)
    scored = [p for p in policies_with_analysis if p.get("analysis")]
    scored.sort(key=lambda x: x["analysis"].get("趋势评分", 0) or 0, reverse=True)

    lines = []
    lines.append(f"# {meta['title']} - {source_name}")
    lines.append("")
    lines.append(f"**{meta['period_label']}：{period_text}**")
    lines.append(f"**来源：{source_name}**")
    lines.append("")
    if meta["period_note"]:
        lines.append(meta["period_note"])
        lines.append("")
    lines.append("---")
    lines.append("")

    top = scored[:report_top_n]
    lines.append(f"## {meta['section_label']} TOP {len(top)}")
    lines.append("")
    if not top:
        lines.append(meta["empty_text"])
    else:
        for i, item in enumerate(top, 1):
            policy = item["policy"]
            analysis = item["analysis"]
            title = field(policy, "title")
            score = analysis.get("趋势评分", 0) if analysis else 0
            summary = analysis.get("一句话总结", "") if analysis else ""
            lines.append(f"### {i}. [{title}]({field(policy, 'url')})")
            lines.append("")
            lines.append(f"- **趋势评分：** {score}/100")
            lines.append(f"- **一句话总结：** {summary}")
            lines.append("")

    lines.append("---")
    lines.append("## 政策影响分析")
    lines.append("")
    for item in top[:5]:
        analysis = item["analysis"]
        if not analysis:
            continue
        title = field(item["policy"], "title")
        explain = analysis.get("政策大白话解释", "")
        direction = analysis.get("国家释放的信号", "")
        lines.append(f"### {title}")
        lines.append("")
        lines.append(f"**大白话解释：** {explain}")
        lines.append("")
        lines.append(f"**国家信号：** {direction}")
        lines.append("")

    lines.append("---")
    lines.append("## 未来产业方向")
    lines.append("")
    for item in scored:
        analysis = item.get("analysis", {})
        if not analysis:
            continue
        for ind in analysis.get("影响行业", []):
            name = ind.get("行业", "")
            if name:
                level = ind.get("影响程度", "中性")
                lines.append(f"- **{name}** - 影响：{level}")
    lines.append("")

    lines.append("---")
    lines.append("## 对大学生的影响与建议")
    lines.append("")
    for item in top[:5]:
        analysis = item.get("analysis", {})
        if not analysis:
            continue
        title = field(item["policy"], "title")
        advice = analysis.get("普通大学生建议", [])
        opportunities = analysis.get("未来机会", [])
        lines.append(f"### 来自《{title}》")
        if opportunities:
            lines.append("**值得关注的机会：**")
            for opp in opportunities:
                lines.append(f"- {opp}")
        if advice:
            lines.append("**建议：**")
            for adv in advice:
                lines.append(f"- {adv}")
        lines.append("")

    background_section = _build_background_section(
        background_policies or [], policies_with_analysis
    )
    if background_section:
        lines.append(background_section)

    if include_source_health:
        section = build_source_health_section()
        if section:
            lines.append(section)

    lines.append("---")
    lines.append("*报告由 Policy Intelligence Agent 自动生成*")
    lines.append(f"*生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

    return "\n".join(lines)


def save_report(report_text: str, output_dir: str = "reports", mode: str = "daily", source_name: str = None) -> Path:
    """
    保存报告，按模式自动命名，包含来源名称。

    Args:
        report_text: Markdown 报告内容
        output_dir: 输出目录
        mode: daily / weekly / monthly / quarterly
        source_name: 来源名称（如 国务院、湖北省人民政府），用于文件名区分
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    now = datetime.now()

    # 来源前缀
    prefix = f"{source_name}_" if source_name else ""

    if mode == "daily":
        filename = f"{prefix}{now.month}.{now.day}日政策日报.md"
    elif mode == "weekly":
        monday = now - timedelta(days=now.weekday())
        sunday = monday + timedelta(days=6)
        filename = f"{prefix}{now.year}.{monday.month}.{monday.day}-{now.year}.{sunday.month}.{sunday.day}日政策周报.md"
    elif mode == "monthly":
        filename = f"{prefix}{now.year}.{now.month}月政策月报.md"
    elif mode == "quarterly":
        quarter = (now.month - 1) // 3 + 1
        filename = f"{prefix}{now.year}年第{quarter}季度政策季报.md"
    else:
        filename = f"{prefix}政策趋势日报_{now.strftime('%Y%m%d')}.md"

    filepath = output_path / filename
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(report_text)
    logger.info(f"报告已保存: {filepath}")
    return filepath
