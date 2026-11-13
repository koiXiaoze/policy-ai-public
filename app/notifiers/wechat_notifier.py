
"""
企业微信群机器人推送
通过 Webhook 发送 Markdown 报告

优化策略：企业微信 Markdown 限制 4096 字符
发送精简版：TOP5 政策标题+评分+一句话总结 + 核心机会/建议
"""

import httpx
from pathlib import Path
from typing import Optional
from app.config import settings
from app.logger import logger
from app.notifiers import BaseNotifier, register_notifier
from app.notifiers.safety import redact_sensitive_text, safe_exception_message


def _build_wechat_summary(report_path: Path) -> str:
    """从完整报告构建微信精简版摘要（< 4000 字符）"""
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return "报告读取失败"

    lines = content.split("\n")
    
    # 提取基本信息
    date_line = ""
    source_line = ""
    for line in lines[:10]:
        if line.startswith("**日期"):
            date_line = line.strip()
        elif line.startswith("**来源"):
            source_line = line.strip()
    
    # 提取 TOP5 政策（从 "### 1." 开始到下一个 "###" 或 "---"）
    top_policies = []
    in_top = False
    current_policy = []
    
    for line in lines:
        if line.startswith("## 今日重要政策 TOP"):
            in_top = True
            continue
        if line.startswith("---") and in_top:
            break
        if in_top and line.startswith("### "):
            if current_policy:
                top_policies.append("\n".join(current_policy))
            current_policy = [line]
        elif in_top and current_policy:
            current_policy.append(line)
    
    if current_policy:
        top_policies.append("\n".join(current_policy))
    
    # 只取前5条，每条只保留标题行、评分行、总结行
    summary_lines = []
    if date_line:
        summary_lines.append(date_line)
    if source_line:
        summary_lines.append(source_line)
    summary_lines.append("")
    
    for i, policy in enumerate(top_policies[:5], 1):
        policy_lines = policy.split("\n")
        # 找标题、评分、总结
        title = ""
        score = ""
        one_line = ""
        for pl in policy_lines:
            if pl.startswith("### "):
                title = pl.replace("### ", "").strip()
            elif "**趋势评分**" in pl:
                score = pl.strip()
            elif "**一句话总结**" in pl:
                one_line = pl.strip()
        if title:
            summary_lines.append(f"**{i}. {title}**")
        if score:
            summary_lines.append(f"  {score}")
        if one_line:
            summary_lines.append(f"  {one_line}")
        summary_lines.append("")
    
    # 提取核心机会和建议（从"值得关注的机会"和"建议"各取前2条）
    opportunities = []
    advices = []
    in_opportunity = False
    in_advice = False
    
    for line in lines:
        if "**值得关注的机会**" in line:
            in_opportunity = True
            in_advice = False
            continue
        elif "**建议**" in line and "大学生" in line:
            in_advice = True
            in_opportunity = False
            continue
        elif line.startswith("### ") or line.startswith("---"):
            in_opportunity = False
            in_advice = False
        
        if in_opportunity and line.strip().startswith("- ") and len(opportunities) < 2:
            opportunities.append(line.strip())
        elif in_advice and line.strip().startswith("- ") and len(advices) < 2:
            advices.append(line.strip())
    
    if opportunities:
        summary_lines.append("**🎯 核心机会**")
        for opp in opportunities:
            summary_lines.append(f"  {opp}")
        summary_lines.append("")
    
    if advices:
        summary_lines.append("**💡 关键建议**")
        for adv in advices:
            summary_lines.append(f"  {adv}")
        summary_lines.append("")
    
    summary_lines.append("*完整报告请查看邮件或后台*")
    
    result = "\n".join(summary_lines)
    # 最终保险截断
    if len(result) > 3800:
        result = result[:3800] + "\n\n... (内容过长已截断)"
    return result


@register_notifier
class WechatNotifier(BaseNotifier):
    """企业微信群机器人通知器"""

    name = "wechat"

    def is_enabled(self) -> bool:
        """检查 Webhook 是否配置"""
        return bool(getattr(settings, "wechat_webhook_url", ""))

    def send_content(self, content: str, metadata: Optional[dict] = None) -> bool:
        """发送文本内容到企业微信群

        Args:
            report_path: Markdown 报告文件路径

        Returns:
            是否成功
        """
        if not self.is_enabled():
            logger.warning("企业微信 Webhook 未配置，跳过推送")
            return False

        try:
            payload = {
                "msgtype": "markdown",
                "markdown": {"content": content}
            }

            with httpx.Client(timeout=30.0) as client:
                resp = client.post(settings.wechat_webhook_url, json=payload)
                resp.raise_for_status()
                result = resp.json()

            if result.get("errcode") == 0:
                logger.info("企业微信推送成功")
                return True
            else:
                logger.error("企业微信推送失败: %s", redact_sensitive_text(result))
                return False

        except Exception as e:
            logger.error("企业微信推送异常: %s", safe_exception_message(e))
            return False

    def send(self, report_path: Path) -> bool:
        """发送报告文件到企业微信群（旧接口兼容）"""
        try:
            content = _build_wechat_summary(report_path)
        except Exception as e:
            logger.error("读取报告失败: %s", safe_exception_message(e))
            return False
        return self.send_content(content)


# 向后兼容：保留原函数名
def send_wechat_report(report_path: Path) -> bool:
    """兼容旧调用方式的封装函数"""
    return WechatNotifier().send(report_path)


# 便于单独测试
