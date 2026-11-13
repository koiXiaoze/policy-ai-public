"""配置管理模块
从 config.yaml 和 .env 文件加载配置
"""

import os
import yaml
from pathlib import Path
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


# 报告 TOP N 的统一兼容默认值。配置缺失或非法时使用该值，
# 避免 CLI、测试临时目录等场景因相对路径变化直接失败。
DEFAULT_REPORT_TOP_N = 10
MAX_REPORT_TOP_N = 10
DEFAULT_REPORT_OUTPUT_DIR = "reports"
DEFAULT_SOURCE_STALE_AFTER_HOURS = 72.0


class Settings(BaseSettings):
    """环境变量配置（来自 .env 文件）"""
    ai_api_key: str = ""
    ai_api_base: str = "https://api.deepseek.com"
    ai_model: str = "deepseek-chat"
    database_url: str = "sqlite:///data/policies.db"
    smtp_server: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    notify_email: str = ""
    # 通知渠道必须由用户显式开启，避免克隆后读完配置即误发消息。
    notify_channels: str = ""
    feishu_webhook_url: str = ""
    # 飞书自建应用双向消息配置。Webhook 仅用于群通知，不能用于接收消息。
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_allowed_user_ids: str = ""
    feishu_bot_open_id: str = ""
    feishu_command_prefix: str = "/政策"
    # 企业微信"智能机器人"（API 模式，WebSocket 长连接）配置。
    # 与下方自建应用是两套独立产品：智能机器人用 BotID + Secret，
    # 无需公网回调 URL，也无需自行处理消息加解密。
    wecom_bot_id: str = ""
    wecom_bot_secret: str = ""
    wecom_bot_allowed_user_ids: str = ""
    wecom_bot_enabled: bool = False
    # 企业微信自建应用双向消息配置。
    # 群机器人 Webhook 是单向通知，不能用于接收用户消息。
    wecom_corp_id: str = ""
    wecom_corp_secret: str = ""
    wecom_agent_id: int = 0

    @field_validator("wecom_agent_id", mode="before")
    @classmethod
    def _empty_wecom_agent_id_as_zero(cls, value):
        """模板中可选项留空时按未配置处理，避免启动阶段校验失败。"""
        if isinstance(value, str) and not value.strip():
            return 0
        return value
    wecom_token: str = ""
    wecom_encoding_aes_key: str = ""
    wecom_allowed_user_ids: str = ""
    wecom_command_prefix: str = "/政策"
    log_level: str = "INFO"
    tavily_api_key: str = ""
    wechat_webhook_url: str = ""  # 企业微信群机器人 Webhook URL
    # 数据源连续失败告警开关。默认开启；测试环境显式关闭，避免误发真实通知。
    source_health_alert_enabled: bool = True
    # 单次采集任务最多为多少条新政策抓取正文。
    # 正文抓取约 4-18 秒/条，限量可避免 AgentTask 超过超时阈值。
    collect_content_limit: int = 10
    # 批量分析的最多并发数；上限由 Service 层再次收紧到 5。
    analyze_concurrency: int = 3
    # AI 请求的单次超时。重试只由 ai_analyzer 的外层循环执行，
    # 避免 SDK 内部重试与业务重试叠加导致失控的长尾时延。
    ai_request_timeout_seconds: float = 300.0
    ai_max_retries: int = 2
    ai_slow_threshold_seconds: float = 60.0
    # 通知记录处于 pending 的租约时长。进程在发送中途退出后，
    # 超过该时长才允许其他进程接管，避免永久跳过，也避免误抢活跃发送。
    notification_pending_timeout_seconds: int = Field(default=300, gt=0)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


# 全局配置实例
settings = Settings()

# 导出 Tavily API Key 到环境变量
if settings.tavily_api_key:
    os.environ["TAVILY_API_KEY"] = settings.tavily_api_key

def load_sources_config() -> dict:
    """加载 config.yaml 中的政府来源配置"""
    config_path = Path("config.yaml")
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path.resolve()}")
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config


def get_enabled_sources() -> List[dict]:
    """获取所有启用的政府来源"""
    config = load_sources_config()
    return [s for s in config.get("sources", []) if s.get("enabled", False)]


def get_source_stale_after_hours(
    source_config: dict,
    *,
    default: float = DEFAULT_SOURCE_STALE_AFTER_HOURS,
) -> float:
    """返回来源新鲜度的最大允许小时数。

    优先使用来源级 ``stale_after_hours``；未配置时按采集周期推导，
    至少保留 72 小时冗余，避免正常低频更新被误报为陈旧。
    配置非法时记录告警并回退，绝不阻断采集或健康检查。
    """
    fallback = default
    if not isinstance(fallback, (int, float)) or isinstance(fallback, bool) or fallback <= 0:
        fallback = DEFAULT_SOURCE_STALE_AFTER_HOURS

    if not isinstance(source_config, dict):
        return fallback

    explicit = source_config.get("stale_after_hours")
    if explicit is not None:
        if isinstance(explicit, (int, float)) and not isinstance(explicit, bool) and explicit > 0:
            return float(explicit)
        from app.logger import logger

        logger.warning(
            "来源 %s 的 stale_after_hours 配置非法，使用默认值 %s: %r",
            source_config.get("name", "<unknown>"),
            fallback,
            explicit,
        )
        return fallback

    interval = source_config.get("scrape_interval_hours")
    if isinstance(interval, (int, float)) and not isinstance(interval, bool) and interval > 0:
        return max(float(interval) * 3, 72.0)

    if interval is not None:
        from app.logger import logger

        logger.warning(
            "来源 %s 的 scrape_interval_hours 配置非法，使用默认值 %s: %r",
            source_config.get("name", "<unknown>"),
            fallback,
            interval,
        )
    return fallback


def get_report_top_n() -> int:
    """读取正式报告统一使用的 TOP N。

    该值是调度计划、Agent 工具和报告渲染三者共用的唯一业务配置。
    配置缺失时回退到兼容默认值，配置非法时也使用默认值并发出告警，
    避免一个配置错误导致所有定时报告无法生成。
    """
    try:
        config = load_sources_config()
        raw_value = (config.get("daily_report") or {}).get(
            "top_n", DEFAULT_REPORT_TOP_N
        )
        value = int(raw_value)
        if not 1 <= value <= MAX_REPORT_TOP_N:
            raise ValueError(
                f"daily_report.top_n 必须在 1..{MAX_REPORT_TOP_N} 之间，"
                f"当前为 {raw_value}"
            )
        return value
    except FileNotFoundError:
        return DEFAULT_REPORT_TOP_N
    except (TypeError, ValueError) as exc:
        from app.logger import logger

        logger.warning("读取报告 TOP N 失败，使用默认值 %s: %s", DEFAULT_REPORT_TOP_N, exc)
        return DEFAULT_REPORT_TOP_N


def get_report_output_dir() -> str:
    """读取正式日报的输出目录。

    配置缺失、为空或类型非法时回退到 reports，避免定时任务因路径配置
    问题完全无法产出报告。返回值保持为字符串，交给 save_report 统一创建目录。
    """
    try:
        config = load_sources_config()
        raw_value = (config.get("daily_report") or {}).get(
            "output_dir", DEFAULT_REPORT_OUTPUT_DIR
        )
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise ValueError(f"daily_report.output_dir 必须是非空字符串，当前为 {raw_value!r}")
        return raw_value.strip()
    except FileNotFoundError:
        return DEFAULT_REPORT_OUTPUT_DIR
    except (TypeError, ValueError) as exc:
        from app.logger import logger

        logger.warning(
            "读取报告输出目录失败，使用默认值 %s: %s",
            DEFAULT_REPORT_OUTPUT_DIR,
            exc,
        )
        return DEFAULT_REPORT_OUTPUT_DIR
