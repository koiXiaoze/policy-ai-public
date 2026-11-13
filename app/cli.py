"""
命令行入口
"""

import sys
import json as _json
from datetime import datetime, timedelta
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import click
from app.logger import logger
from app.database import init_db, SessionLocal
from app.analyzers.ai_analyzer import analyze_policy
from app.reporters.daily_report import generate_daily_report, save_report
from app.workflow import _dedupe_and_save_policies, _fill_content_and_analyze, _collect_local_paths, _import_local_files
from app.models.policy import Policy, Analysis
from app.scrapers.factory import create_scraper, run_all_scrapers
from app.config import get_enabled_sources


def _configure_utf8_stdio() -> None:
    """真实 CLI 入口统一 UTF-8，避免 Windows 重定向后 JSON/中文变成 GBK。"""

    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            # Click 的隔离流或嵌入式调用可能不支持重配置，不能因此阻断命令。
            continue


@click.group()
def cli():
    """政策情报系统"""
    pass


def main() -> None:
    """安装包与 main.py 共用的真实命令行入口。"""

    _configure_utf8_stdio()
    cli()


@cli.command()
def init():
    init_db()
    click.echo("数据库初始化完成")


@cli.command()
@click.option("--force", is_flag=True)
@click.option("--source", default=None, help="来源名称，如 国务院 / 湖北省人民政府")
def daily(force, source):
    """每日政策分析（按来源分开生成报告）"""
    _run("daily", 1, force, source)


@cli.command(name="weekly")
@click.option("--force", is_flag=True)
@click.option("--source", default=None, help="来源名称")
def weekly(force, source):
    """每周政策分析"""
    _run("weekly", 7, force, source)


@cli.command(name="monthly")
@click.option("--force", is_flag=True)
@click.option("--source", default=None, help="来源名称")
def monthly(force, source):
    """近30天政策分析"""
    _run("monthly", 30, force, source)


@cli.command(name="quarterly")
@click.option("--force", is_flag=True)
@click.option("--source", default=None, help="来源名称")
def quarterly(force, source):
    """近90天（季度）政策分析"""
    _run("quarterly", 90, force, source)


@cli.command(name="update-json")
@click.option("--output", default="data/ZUIXINZHENGCE.json")
def update_json(output):
    """从国务院网站更新最新政策 JSON 数据"""
    url = "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json"
    click.echo(f"正在下载: {url}")
    try:
        import requests
        resp = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }, timeout=30)
        resp.encoding = "utf-8"
        resp.raise_for_status()
        data = resp.json()
        out_path = Path(output)
        out_path.parent.mkdir(exist_ok=True)
        out_path.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        dates = [e.get("DOCRELPUBTIME", "")[:10] for e in data if e.get("DOCRELPUBTIME")]
        click.echo(f"成功! 共 {len(data)} 条政策")
        click.echo(f"日期范围: {min(dates)} ~ {max(dates)}")
        click.echo(f"已保存: {out_path.resolve()}")
    except Exception as e:
        click.echo(f"下载失败: {e}")
        raise click.ClickException("更新失败，请检查网络连接")


@cli.command(name="scheduler")
def scheduler():
    """启动定时调度器，由调度器触发 Agent Task"""
    from app.agent.scheduler import start_scheduled_tasks

    start_scheduled_tasks()


@cli.command(name="import-html")
@click.argument("paths", nargs=-1, required=True)
@click.option("--source", default="国务院")
@click.option("--analyze/--no-analyze", default=True)
@click.option("--force", is_flag=True)
@click.option("--days", type=int, default=None)
@click.option("--mode", type=click.Choice(["daily", "weekly", "monthly", "quarterly"]), default="daily")
def import_html(paths, source, analyze, force, days, mode):
    """从本地 JSON/HTML 文件导入"""
    from bs4 import BeautifulSoup
    from app.scrapers.base import ScrapedItem

    raw_items = []
    seen_urls = set()

    for p in paths:
        fp = Path(p)
        if not fp.exists():
            fp2 = Path("data") / p
            if fp2.exists():
                fp = fp2
        if not fp.exists():
            click.echo(f"跳过: {p}")
            continue
        if fp.is_dir():
            for child in sorted(fp.iterdir()):
                if child.suffix.lower() in (".json", ".html", ".htm"):
                    _parse_file(child, source, raw_items, seen_urls, days)
        elif fp.suffix.lower() == ".json":
            _parse_json_file(fp, source, raw_items, seen_urls, days)
        elif fp.suffix.lower() in (".html", ".htm"):
            _parse_file(fp, source, raw_items, seen_urls, days)

    if not raw_items:
        click.echo("未找到政策链接")
        return

    _save_and_analyze(raw_items, source, analyze, force, mode, days)


@cli.command(name="generate-report")
@click.option("--limit", default=10)
@click.option("--mode", type=click.Choice(["daily", "weekly", "monthly", "quarterly"]), default="daily")
@click.option("--source", default=None, help="来源名称，如 国务院")
def generate_report(limit, mode, source):
    """从数据库已有分析结果重新生成报告"""
    init_db()
    db = SessionLocal()
    try:
        q = db.query(Analysis, Policy).join(Policy, Analysis.policy_id == Policy.id)
        if source:
            q = q.filter(Policy.source_name == source)
        items = q.order_by(Analysis.trend_score.desc()).limit(limit).all()

        if not items:
            click.echo("数据库中没有分析结果")
            return
        click.echo(f"数据库中有 {len(items)} 条分析结果")
        policies_with_analysis = []
        actual_source = source or (items[0][1].source_name if hasattr(items[0][1], "source_name") else "国务院")
        for analysis, policy in items:
            try:
                ad = _json.loads(analysis.raw_response) if analysis.raw_response else {}
            except Exception:
                ad = {"趋势评分": analysis.trend_score or 0, "一句话总结": analysis.summary or ""}
            policies_with_analysis.append({"policy": policy, "analysis": ad})
        report_text = generate_daily_report(policies_with_analysis, source_name=actual_source)
        rp = save_report(report_text, mode=mode, source_name=actual_source)
        click.echo(f"报告已保存: {rp}")
    finally:
        db.close()


@cli.command(name="verify-notification")
@click.option(
    "--report",
    "report_path",
    required=True,
    type=click.Path(exists=False, dir_okay=False, path_type=Path),
    help="待验收的 UTF-8 Markdown 报告路径",
)
@click.option(
    "--channel",
    "channels",
    multiple=True,
    type=click.Choice(["email", "wechat"]),
    help="验收渠道，可重复指定；默认同时检查 email 和 wechat",
)
@click.option("--send", is_flag=True, help="真实发送；省略时仅预演，不创建台账、不访问网络")
@click.option("--task-id", default=None, help="发送任务的稳定 ID；用于验证重复发送会被跳过")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 输出机器可读结果")
def verify_notification(report_path, channels, send, task_id, as_json):
    """验收指定报告的邮件/企业微信通知链路。"""
    from uuid import uuid4

    from app.notifiers.service import NotificationError, NotificationService

    selected_channels = list(channels) or ["email", "wechat"]
    stable_task_id = task_id or f"notification_verify:{uuid4()}"
    metadata = {
        "task_id": stable_task_id,
        "task_type": "notification_verify",
        "trigger_type": "manual",
        "title": f"政策通知验收 - {report_path.stem}",
    }

    try:
        preview = NotificationService.preview(
            report_path=report_path,
            channels=selected_channels,
            metadata=metadata,
        )
    except NotificationError as exc:
        if as_json:
            click.echo(_json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        else:
            click.echo(f"验收失败: {exc}", err=True)
        raise click.exceptions.Exit(2)

    unconfigured = [
        channel
        for channel, detail in preview["channels"].items()
        if not detail["configured"]
    ]
    result = {
        "status": "failed" if unconfigured else "ok",
        "mode": "send" if send else "dry-run",
        "report_path": preview["report_path"],
        "chars": preview["chars"],
        "task_id": stable_task_id,
        "channels": preview["channels"],
    }

    if unconfigured:
        result["error"] = f"以下渠道未配置: {', '.join(unconfigured)}"
    elif send:
        result["results"] = NotificationService.notify_channels(
            channels=selected_channels,
            content=Path(report_path).read_text(encoding="utf-8"),
            metadata=metadata,
        )
        failed_channels = [
            channel
            for channel, item in result["results"].items()
            if item.get("status") not in {"success", "skipped"}
        ]
        if failed_channels:
            result["status"] = "failed"
            result["error"] = f"以下渠道发送失败: {', '.join(failed_channels)}"

    if as_json:
        click.echo(_json.dumps(result, ensure_ascii=False, indent=2))
    else:
        click.echo(f"验收模式: {result['mode']}")
        click.echo(f"报告: {result['report_path']}")
        click.echo(f"字符数: {result['chars']}")
        click.echo(f"任务 ID: {result['task_id']}")
        for channel, detail in result["channels"].items():
            configured = "已配置" if detail["configured"] else "未配置"
            click.echo(
                f"{channel}: {configured}, 预计 {detail['estimated_chunks']} 个分片 "
                f"(上限 {detail['max_chars']} 字符/片)"
            )
        if send and "results" in result:
            for channel, item in result["results"].items():
                click.echo(f"{channel} 发送结果: {item.get('status')}")
        if result.get("error"):
            click.echo(f"结果: {result['error']}", err=True)

    if result["status"] == "failed":
        raise click.exceptions.Exit(1)


@cli.command(name="source-health")
@click.option("--probe", is_flag=True, help="先执行一次真实采集探针，再读取健康结果")
@click.option("--strict", is_flag=True, help="存在陈旧、降级或持续失败来源时返回非零退出码")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 输出机器可读结果")
def source_health(probe, strict, as_json):
    """查看数据源健康与政策数据新鲜度。"""
    from app.services.source_health import source_health_service

    init_db()
    if probe:
        try:
            run_all_scrapers(suppress_health_alerts=True)
        except Exception as exc:
            logger.exception("数据源巡检探针执行失败")
            result = {
                "status": "probe_failed",
                "probe": True,
                "error": str(exc),
            }
            if as_json:
                click.echo(_json.dumps(result, ensure_ascii=False, indent=2))
            else:
                click.echo(f"巡检探针执行失败: {exc}", err=True)
            raise click.exceptions.Exit(2)

    try:
        summary = source_health_service.summary()
    except Exception as exc:
        logger.exception("读取数据源健康摘要失败")
        result = {
            "status": "query_failed",
            "probe": probe,
            "error": str(exc),
        }
        if as_json:
            click.echo(_json.dumps(result, ensure_ascii=False, indent=2))
        else:
            click.echo(f"读取数据源健康摘要失败: {exc}", err=True)
        raise click.exceptions.Exit(2)

    failing = summary.get("failing_sources") or []
    degraded = summary.get("degraded_sources") or []
    stale = summary.get("stale_sources") or []
    unknown = summary.get("unknown_freshness_sources") or []
    unhealthy = bool(failing or degraded or stale)
    result = {
        "status": "degraded" if unhealthy else "ok",
        "probe": probe,
        "strict": strict,
        "summary": summary,
    }

    if as_json:
        click.echo(_json.dumps(result, ensure_ascii=False, indent=2))
    else:
        click.echo(f"数据源总数: {summary.get('total', 0)}")
        click.echo(
            "状态: "
            f"正常 {summary.get('ok', 0)}，无更新 {summary.get('empty', 0)}，"
            f"降级 {summary.get('degraded', 0)}，失败 {summary.get('failed', 0)}"
        )
        click.echo(
            "新鲜度: "
            f"新鲜 {summary.get('fresh', 0)}，陈旧 {summary.get('stale', 0)}，"
            f"未知 {summary.get('unknown_freshness', 0)}"
        )
        for item in summary.get("sources") or []:
            age = item.get("stale_hours")
            age_text = "—" if age is None else f"{float(age):.1f} 小时"
            click.echo(
                f"- {item.get('source_name')}: {item.get('status')}/"
                f"{item.get('freshness_status')}，最近入库 "
                f"{item.get('last_new_policy_at') or '—'}，距今 {age_text}"
            )
        if stale:
            click.echo(f"陈旧来源: {'、'.join(stale)}", err=True)
        if degraded:
            click.echo(f"降级来源: {'、'.join(degraded)}", err=True)
        if failing:
            click.echo(f"持续失败来源: {'、'.join(failing)}", err=True)
        if unknown:
            click.echo(f"新鲜度未知来源: {'、'.join(unknown)}")

    if strict and unhealthy:
        raise click.exceptions.Exit(1)


@cli.command(name="source-diagnostics")
@click.option("--probe", is_flag=True, help="联网执行真实探测并写入审计记录")
@click.option("--strict", is_flag=True, help="任一启用来源非 ok 时返回退出码 1")
@click.option("--json", "as_json", is_flag=True, help="以 JSON 输出机器可读结果")
def source_diagnostics(probe, strict, as_json):
    """检查来源的 WAF、TLS 与 DOM/JSON 政策列表结构稳定性。"""
    from app.services.source_diagnostic import source_diagnostic_service

    try:
        init_db()
        summary = (
            source_diagnostic_service.probe_all(suppress_alert=True)
            if probe
            else source_diagnostic_service.summary()
        )
    except Exception as exc:
        logger.exception("来源稳定性诊断执行失败")
        error_result = {
            "status": "probe_failed" if probe else "query_failed",
            "probe": probe,
            "strict": strict,
            "error": str(exc),
        }
        if as_json:
            click.echo(_json.dumps(error_result, ensure_ascii=False, indent=2, default=str))
        else:
            click.echo(f"来源稳定性诊断失败: {exc}", err=True)
        raise click.exceptions.Exit(2)

    unhealthy = summary.get("unhealthy") or 0
    storage_errors = summary.get("storage_errors") or 0
    if storage_errors:
        overall_status = "storage_failed"
    elif unhealthy:
        overall_status = "degraded"
    else:
        overall_status = "ok"
    result = {
        "status": overall_status,
        "probe": probe,
        "strict": strict,
        "summary": summary,
    }

    if as_json:
        click.echo(_json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        click.echo(f"启用来源总数: {summary.get('total', 0)}")
        click.echo(
            "状态: "
            f"正常 {summary.get('ok', 0)}，WAF {summary.get('waf_challenge', 0)}，"
            f"TLS {summary.get('tls_issues', 0)}，DOM {summary.get('dom_issues', 0)}，"
            f"Schema {summary.get('schema_issues', 0)}，"
            f"传输错误 {summary.get('transport_errors', 0)}，"
            f"未配置 {summary.get('unconfigured', 0)}，未检查 {summary.get('not_checked', 0)}"
        )
        for item in summary.get("sources") or []:
            tls_text = "—"
            if item.get("tls_days_remaining") is not None:
                tls_text = f"{item['tls_days_remaining']} 天"
            checked_text = item.get("checked_at") or "从未检查"
            click.echo(
                f"- {item.get('source_name')}: {item.get('status')}，"
                f"TLS 剩余 {tls_text}，检查于 {checked_text}"
            )
            if item.get("error"):
                click.echo(f"  详情: {item['error']}")
            if item.get("persistence_error"):
                click.echo(f"  落库失败: {item['persistence_error']}", err=True)
        if not probe:
            click.echo("提示: 当前展示数据库中的最新记录；使用 --probe 才会联网检查。")

    if storage_errors:
        raise click.exceptions.Exit(2)
    if strict and unhealthy:
        raise click.exceptions.Exit(1)


@cli.command(name="notification-status")
@click.option("--task-id", required=True, help="发送时使用的稳定任务 ID")
@click.option(
    "--wait",
    "wait_seconds",
    type=click.FloatRange(min=0, max=300),
    default=0,
    show_default=True,
    help="等待状态离开 pending 的秒数，0 表示立即查询",
)
@click.option("--json", "as_json", is_flag=True, help="以 JSON 输出机器可读结果")
def notification_status(task_id, wait_seconds, as_json):
    """查询通知各渠道的发送状态。"""
    import time

    from app.notifiers.service import NotificationError, NotificationService

    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            result = NotificationService.get_status(task_id)
        except NotificationError as exc:
            result = {
                "task_id": task_id,
                "status": "query_failed",
                "record_count": 0,
                "records": [],
                "error": str(exc),
            }
            if as_json:
                click.echo(_json.dumps(result, ensure_ascii=False, indent=2))
            else:
                click.echo(f"查询失败: {exc}", err=True)
            raise click.exceptions.Exit(2)

        if result["status"] not in {"pending", "stale"} or time.monotonic() >= deadline:
            break
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    result["timed_out"] = bool(wait_seconds and result["status"] == "pending")
    if as_json:
        click.echo(_json.dumps(result, ensure_ascii=False, indent=2))
    else:
        click.echo(f"任务 ID: {result['task_id']}")
        click.echo(f"总体状态: {result['status']}")
        click.echo(f"通知记录数: {result['record_count']}")
        for item in result["records"]:
            click.echo(
                f"{item['channel']}: {item['status']}, "
                f"{item['sent_chunks']}/{item['chunks']} 个分片, "
                f"重试 {item['retry_count']}/{item['max_retries']}"
            )
            if item["error_message"]:
                click.echo(f"  错误: {item['error_message']}")
        if result["timed_out"]:
            click.echo("等待超时，通知仍处于发送中", err=True)

    if result["status"] in {"failed", "query_failed", "stale"} or result["timed_out"]:
        raise click.exceptions.Exit(1)


@cli.command()
@click.argument("url")
def analyze(url):
    import trafilatura
    click.echo(f"正在提取: {url}")
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        click.echo("无法访问")
        return
    content = trafilatura.extract(downloaded, include_links=False, include_images=False)
    if not content:
        click.echo("无法提取正文")
        return
    click.echo(f"正文: {len(content)} 字符")
    result = analyze_policy(content, url)
    if result:
        for k, v in result.items():
            click.echo(f"  {k}: {v}")


@cli.command()
def sources():
    from app.config import load_sources_config
    config = load_sources_config()
    click.echo(f"{'来源名称':<20} {'级别':<8} {'状态':<8} {'网站'}")
    click.echo("-" * 70)
    for s in config.get("sources", []):
        status = "[启用]" if s.get("enabled") else "[禁用]"
        click.echo(f"{s['name']:<20} {s['level']:<8} {status:<8} {s['website']}")


# ========== 核心逻辑 ==========

def _run(report_mode: str, days: int, force: bool, source_name: str = None):
    """按来源分别运行"""
    init_db()
    sources = get_enabled_sources()
    if source_name:
        sources = [s for s in sources if s.get("name") == source_name]
        if not sources:
            click.echo(f"未找到来源: {source_name}")
            click.echo("可用来源: 国务院, 湖北省人民政府, 十堰市人民政府")
            return

    for src in sources:
        name = src.get("name", "未知")
        click.echo(f"\n{'='*50}")
        click.echo(f"开始分析: {name}")
        click.echo(f"{'='*50}")

        raw_items = _try_fetch(src, days)

        if not raw_items:
            click.echo(f"  {name}: 未获取到政策，跳过")
            continue

        click.echo(f"  {name}: 获取到 {len(raw_items)} 条政策")
        _save_and_analyze(raw_items, name, True, force, report_mode, days)


def _try_fetch(source_config: dict, days: int) -> list:
    """尝试获取某个来源的政策数据"""
    scraper_type = source_config.get("scraper_type", "")

    # 在线抓取
    try:
        scraper = create_scraper(source_config)
        scraper.days_back = days
        items = scraper.fetch()
        if items:
            return items
    except Exception as e:
        logger.warning("在线抓取失败 %s: %s", source_config.get("name"), e)

    # 本地回退（人社部跳过，无专用本地文件）
    name = source_config.get("name", "")
    scraper_type = source_config.get("scraper_type", "")
    if "人社部" in name or "人力资源和社会保障部" in name or scraper_type == "mohrss":
        logger.info("人社部无专用本地文件，跳过本地回退")
    else:
        click.echo(f"  在线抓取失败，尝试本地文件回退...")
        paths = _collect_local_paths()
        if paths:
            if "湖北" in name:
                paths = [p for p in paths if "hubei" in p.name.lower() or "湖北" in p.name.lower()]
            elif "十堰" in name:
                paths = [p for p in paths if "shiyan" in p.name.lower() or "十堰" in p.name.lower()]
            items = _import_local_files(paths, source_name=name, days=days)
            if items:
                return items

    # 尝试直接下载 JSON（仅国务院）
    if scraper_type == "gov_cn":
        try:
            import requests
            url = "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json"
            click.echo(f"  尝试直接下载 JSON...")
            resp = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            from app.scrapers.gov_scraper import GovCnScraper
            scraper = GovCnScraper(source_config)
            scraper.days_back = days
            items = scraper._parse_json_data(data)
            if items:
                click.echo(f"  直接下载 JSON 成功: {len(data)} 条, 筛选后 {len(items)} 条")
                return items
        except Exception as e:
            logger.debug("直接下载 JSON 失败: %s", e)

    return []


def _save_and_analyze(raw_items, source, do_analyze, force, report_mode="daily", days_back=None):
    init_db()
    db = SessionLocal()
    try:
        new_policies = _dedupe_and_save_policies(db, raw_items)
        click.echo(f"  导入: 候选 {len(raw_items)} 条, 新增 {len(new_policies)} 条")

        if do_analyze:
            all_urls = [item.url for item in raw_items]
            
            # 计算日期截止线
            cutoff = datetime.now() - timedelta(days=days_back) if days_back else None
            logger.info("日期过滤: days_back=%s, cutoff=%s", days_back, cutoff)
            
            if force:
                # force=True 时，获取该来源所有政策并按日期过滤
                source_policies = db.query(Policy).filter(
                    Policy.source_name == source,
                    ~Policy.title.like("%解读%"),
                    ~Policy.url.like("%/zcjd/%"),
                    ~Policy.url.like("%/jiedu/%")
                ).all()
                logger.info("force模式: 该来源共 %s 条政策", len(source_policies))
                
                # 应用日期过滤
                if cutoff:
                    need_analysis = [p for p in source_policies if p.publish_time and p.publish_time >= cutoff]
                    logger.info("日期过滤后: %s 条政策 (>= %s)", len(need_analysis), cutoff)
                    # 记录被过滤掉的政策
                    filtered = [p for p in source_policies if not (p.publish_time and p.publish_time >= cutoff)]
                    for p in filtered:
                        logger.debug("  过滤掉: %s (发布时间: %s)", p.title[:30], p.publish_time)
                else:
                    need_analysis = source_policies
                
                for p in need_analysis:
                    db.query(Analysis).filter(Analysis.policy_id == p.id).delete()
                    p.content = None
                db.commit()
            else:
                # 正常模式：只分析新获取的政策
                need_analysis = list(new_policies)
                existing = (
                    db.query(Policy)
                    .filter(Policy.url.in_(all_urls))
                    .filter(~Policy.id.in_(db.query(Analysis.policy_id)))
                    .all()
                )
                for p in existing:
                    if p not in need_analysis:
                        need_analysis.append(p)
                # 应用日期过滤
                if cutoff:
                    before_count = len(need_analysis)
                    need_analysis = [p for p in need_analysis if p.publish_time and p.publish_time >= cutoff]
                    logger.info("正常模式日期过滤: %s -> %s 条", before_count, len(need_analysis))

            click.echo(f"  待分析: {len(need_analysis)} 条")
            if need_analysis:
                analyzed = _fill_content_and_analyze(db, need_analysis)
                click.echo(f"  AI 分析成功: {len(analyzed)} 条")
                if analyzed:
                    report_text = generate_daily_report(analyzed, source_name=source)
                    rp = save_report(report_text, mode=report_mode, source_name=source)
                    click.echo(f"  报告已保存: {rp}")
                else:
                    click.echo("  AI分析均失败，尝试从数据库重新生成报告")
                    _regenerate_report_from_db(db, [p.url for p in need_analysis], report_mode, source)
            else:
                click.echo("  所有政策已有分析结果")
                _regenerate_report_from_db(db, all_urls, report_mode, source)
        db.commit()
    except Exception as e:
        logger.error("执行失败: %s", e)
        db.rollback()
        raise click.ClickException(str(e))
    finally:
        db.close()
def _parse_file(fp, source, items, seen, days=None):
    try:
        raw = fp.read_bytes()
        text = raw.decode("utf-8", errors="replace")
    except Exception as e:
        click.echo(f"读取失败 {fp.name}: {e}")
        return
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(text, "html.parser")
    all_a = soup.find_all("a", href=True)
    click.echo(f"  {fp.name}: {len(raw)} 字节, <a> {len(all_a)} 个")
    if days:
        click.echo(f"  日期筛选: 最近 {days} 天")
    matched = 0
    for a in all_a:
        href = (a.get("href") or "").strip()
        title = a.get_text(" ", strip=True)
        if "/zhengce/" not in href and "/zfwj/" not in href and "/xxgk/" not in href:
            continue
        if not title or len(title) < 5:
            continue
        if not href.startswith("http"):
            if href.startswith("./"):
                href = f"https://www.hubei.gov.cn/zfwj/{href.lstrip('./')}"
            elif href.startswith("/"):
                href = f"https://www.gov.cn{href}"
            else:
                href = f"https://www.gov.cn/{href}"
        if href in seen:
            continue
        seen.add(href)
        from app.scrapers.base import ScrapedItem
        items.append(ScrapedItem(title=title, url=href, source_name=source, government_level="国家", department=source))
        matched += 1
    click.echo(f"    -> 匹配: {matched} 条")


def _parse_json_file(fp, source, items, seen, days=None):
    try:
        data = _json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:
        click.echo(f"  JSON 解析失败 {fp.name}: {e}")
        return
    if not isinstance(data, list):
        click.echo(f"  JSON 格式错误: {fp.name}")
        return
    from app.scrapers.base import ScrapedItem
    cutoff = datetime.now() - timedelta(days=days) if days else None
    if days:
        click.echo(f"  日期筛选: 最近 {days} 天")
    matched = 0
    total = 0
    for entry in data:
        url = (entry.get("URL") or "").strip()
        title = (entry.get("TITLE") or "").strip()
        pub_str = (entry.get("DOCRELPUBTIME") or "").strip()
        total += 1
        if not url or not title:
            continue
        if cutoff and pub_str:
            try:
                pub_date = datetime.strptime(pub_str[:10], "%Y-%m-%d")
                if pub_date < cutoff:
                    continue
            except Exception:
                continue
        if not url.startswith("http"):
            url = "https://www.gov.cn" + url if url.startswith("/") else "https://www.gov.cn/" + url
        if url in seen:
            continue
        seen.add(url)
        items.append(ScrapedItem(title=title, url=url, source_name=source, government_level="国家", department=source))
        matched += 1
    click.echo(f"  {fp.name}: JSON 共 {total} 条, 筛选后匹配 {matched} 条")


def _regenerate_report_from_db(db, urls, report_mode="daily", source_name=None):
    items = (
        db.query(Analysis, Policy)
        .join(Policy, Analysis.policy_id == Policy.id)
        .filter(Policy.url.in_(urls))
        .order_by(Analysis.trend_score.desc())
        .all()
    )
    if not items:
        return
    if not source_name and items:
        source_name = items[0][1].source_name if hasattr(items[0][1], "source_name") else "国务院"
    policies_with_analysis = []
    for analysis, policy in items:
        try:
            ad = _json.loads(analysis.raw_response) if analysis.raw_response else {}
        except Exception:
            ad = {"趋势评分": analysis.trend_score or 0, "一句话总结": analysis.summary or ""}
        policies_with_analysis.append({"policy": policy, "analysis": ad})
    report_text = generate_daily_report(policies_with_analysis, source_name=source_name)
    rp = save_report(report_text, mode=report_mode, source_name=source_name)
    click.echo(f"报告已保存: {rp}")
