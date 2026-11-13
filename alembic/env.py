"""Alembic 运行环境。

支持两种调用方式：
1. 命令行 ``alembic upgrade head``：从 app.config 读取数据库 URL；
2. 应用内升级：通过 ``config.attributes["connection"]`` 注入现有连接，
   使 shared-cache 内存 SQLite 和测试事务能够看到同一份表结构。
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.database import Base
import app.models  # noqa: F401  确保所有模型已注册到 Base.metadata

config = context.config

# 应用内执行迁移时不得重配全局日志，否则会关闭应用已有 logger，
# 导致迁移之后的错误日志静默丢失。直接运行 alembic 命令时仍启用
# Alembic 日志，同时保留宿主进程中已经创建的 logger。
if (
    config.config_file_name is not None
    and config.attributes.get("configure_logging", True)
):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _get_url() -> str:
    """读取调用方显式传入的 URL，否则回退到应用配置。"""

    configured_url = config.get_main_option("sqlalchemy.url")
    if configured_url and configured_url != "driver://user:pass@localhost/dbname":
        return configured_url
    from app.config import settings

    return settings.database_url


def run_migrations_offline() -> None:
    """生成迁移 SQL，不建立数据库连接。"""

    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在现有连接或新建 Engine 上执行迁移。"""

    injected_connection = config.attributes.get("connection")
    if injected_connection is not None:
        context.configure(
            connection=injected_connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _get_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
