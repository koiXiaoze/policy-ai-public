"""
数据库管理模块
使用 SQLAlchemy 管理数据库连接和会话。
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings
from app.logger import logger


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类。"""

    pass


class DatabaseMigrationError(RuntimeError):
    """数据库迁移前置条件或版本状态异常。"""


def _ensure_sqlite_parent_dir(database_url: str) -> None:
    """为文件型 SQLite 创建父目录。

    默认数据库位于被 .gitignore 排除的 data/ 目录。全新克隆不存在该目录，
    必须在 Engine 建立连接前创建，否则 python main.py init 会直接失败。
    使用 SQLAlchemy 的 URL 解析器处理相对路径、Windows 盘符和内存数据库。
    """

    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return
    Path(url.database).parent.mkdir(parents=True, exist_ok=True)

_ensure_sqlite_parent_dir(settings.database_url)

# 创建数据库引擎
engine = create_engine(
    settings.database_url,
    echo=False,
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
)

# 会话工厂
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

#: 历史库后续新增的列；新库由初始迁移直接创建完整表。
AGENT_TASK_ADDED_COLUMNS = {
    "channel": "VARCHAR(20)",
    "conversation_id": "VARCHAR(200)",
    "user_id": "VARCHAR(100)",
}

#: source_health 表后续新增的列；仅用于接管无 Alembic 版本的历史库。
SOURCE_HEALTH_ADDED_COLUMNS = {
    "error_type": "VARCHAR(40)",
}


def _alembic_config(database_url: str | None = None) -> Config:
    """构造 Alembic 配置，路径相对本项目而不是当前工作目录。"""

    project_root = Path(__file__).resolve().parent.parent
    config = Config(str(project_root / "alembic.ini"))
    # init_db 是应用运行路径，不是独立 CLI；不得让 Alembic 重配日志。
    config.attributes["configure_logging"] = False
    if database_url:
        # 避免 URL 中的 % 被 ConfigParser 当成插值语法。
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _run_alembic(
    action: str,
    *,
    database_url: str | None = None,
    connection: Connection | None = None,
    revision: str = "head",
) -> None:
    """执行 upgrade 或 stamp；调用方传入连接时复用同一事务/内存库。"""

    config = _alembic_config(database_url)
    if connection is not None:
        config.attributes["connection"] = connection
    if action == "upgrade":
        command.upgrade(config, revision)
    elif action == "stamp":
        command.stamp(config, revision)
    else:
        raise ValueError(f"不支持的 Alembic 操作: {action}")


def _has_business_tables(connection: Connection) -> bool:
    """判断是否已存在任一本项目的业务表。

    不能只检查 ``policy``：部分初始化的旧库可能恰好缺这张表，
    此时必须进入严格结构校验，而不是把它误判为空库后尝试重建。
    """

    import app.models  # noqa: F401  注册全部模型

    expected_tables = set(Base.metadata.tables)
    existing_tables = set(inspect(connection).get_table_names())
    return bool(expected_tables & existing_tables)


def _has_migration_version(connection: Connection) -> bool:
    """判断是否由 Alembic 接管过。"""

    return "alembic_version" in inspect(connection).get_table_names()


def _validate_migration_version(connection: Connection) -> None:
    """校验已存在的版本表确实且只记录了一个版本。"""

    try:
        revisions = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalars().all()
    except Exception as exc:
        raise DatabaseMigrationError(
            "alembic_version 表存在但无法读取 version_num，请先人工检查数据库"
        ) from exc

    if len(revisions) != 1 or not revisions[0]:
        raise DatabaseMigrationError(
            "alembic_version 表必须恰好包含一个非空版本号，"
            f"当前记录数为 {len(revisions)}"
        )


def _validate_schema(connection: Connection) -> None:
    """校验当前库至少包含 ORM 声明的全部业务表和列。

    历史 SQLite 库可能存在 NOT NULL/默认值等约束差异，本阶段不做破坏性
    重建；但缺表或缺列会让运行时查询直接失败，因此必须在 stamp 前阻断。
    """

    import app.models  # noqa: F401  注册全部模型

    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    missing_tables: list[str] = []
    missing_columns: list[str] = []

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            missing_tables.append(table.name)
            continue
        existing_columns = {
            column["name"] for column in inspector.get_columns(table.name)
        }
        for column in table.columns:
            if column.name not in existing_columns:
                missing_columns.append(f"{table.name}.{column.name}")

    if missing_tables or missing_columns:
        details: list[str] = []
        if missing_tables:
            details.append(f"缺表: {', '.join(missing_tables)}")
        if missing_columns:
            details.append(f"缺列: {', '.join(missing_columns)}")
        raise DatabaseMigrationError(
            "数据库结构不完整，已拒绝标记 Alembic 版本；"
            + "；".join(details)
        )


def _ensure_legacy_agent_task_columns(connection: Connection) -> None:
    """只对旧库补齐历史上新增的 agent_task 列。

    新库由首个迁移直接创建完整表；这里仅服务已被应用运行过、
    但没有 alembic_version 的历史 SQLite/PostgreSQL 库。
    """

    if "agent_task" not in inspect(connection).get_table_names():
        return
    existing = {column["name"] for column in inspect(connection).get_columns("agent_task")}
    missing = {
        name: ddl
        for name, ddl in AGENT_TASK_ADDED_COLUMNS.items()
        if name not in existing
    }
    for name, ddl_type in missing.items():
        try:
            connection.execute(text(f"ALTER TABLE agent_task ADD COLUMN {name} {ddl_type}"))
            logger.info("agent_task 表已补列: %s %s", name, ddl_type)
        except Exception:
            logger.exception("agent_task 表补列失败: %s %s", name, ddl_type)
            raise


def _ensure_legacy_source_health_columns(connection: Connection) -> None:
    """为无 Alembic 版本的历史库补齐 source_health 新增列。"""

    if "source_health" not in inspect(connection).get_table_names():
        return
    existing = {
        column["name"] for column in inspect(connection).get_columns("source_health")
    }
    missing = {
        name: ddl
        for name, ddl in SOURCE_HEALTH_ADDED_COLUMNS.items()
        if name not in existing
    }
    for name, ddl_type in missing.items():
        try:
            connection.execute(
                text(f"ALTER TABLE source_health ADD COLUMN {name} {ddl_type}")
            )
            logger.info("source_health 表已补列: %s %s", name, ddl_type)
        except Exception:
            logger.exception("source_health 表补列失败: %s %s", name, ddl_type)
            raise


def _ensure_legacy_added_columns(connection: Connection) -> None:
    """统一补齐历史库已知新增列。"""

    _ensure_legacy_agent_task_columns(connection)
    _ensure_legacy_source_health_columns(connection)
    _ensure_legacy_source_diagnostic_table(connection)


def _ensure_legacy_source_diagnostic_table(connection: Connection) -> None:
    """为无 Alembic 版本的历史库补建诊断表。

    该表是新增的独立审计记录，不依赖既有业务表数据。只补建这一张
    明确已知的表，不调用 ``create_all()``，以免掩盖历史库中其他核心表
    缺失等必须人工处理的问题。
    """

    import app.models  # noqa: F401  注册全部模型

    inspector = inspect(connection)
    if "source_diagnostic" in inspector.get_table_names():
        return
    try:
        Base.metadata.tables["source_diagnostic"].create(connection)
        logger.info("历史数据库已补建 source_diagnostic 表")
    except Exception:
        logger.exception("历史数据库补建 source_diagnostic 表失败")
        raise


def upgrade_database(
    database_url: str | None = None,
    *,
    connection: Connection | None = None,
) -> None:
    """将数据库升级到最新版本。

    - 空库：执行初始迁移，创建完整结构；
    - 历史库且无版本表：先补历史列并校验完整结构，再 stamp head；
    - 已接管库：正常升级到 head，可重复执行。
    """

    import app.models  # noqa: F401  注册全部模型

    if connection is not None:
        _upgrade_with_connection(connection)
        return

    # 默认路径必须复用全局 Engine。临时 Engine 的 dispose() 会释放
    # shared-cache 内存 SQLite 的数据库，导致 SessionLocal 随后看不到表。
    if database_url is None:
        with engine.begin() as target_connection:
            _upgrade_with_connection(target_connection)
        return

    target_engine: Engine = create_engine(
        database_url,
        echo=False,
        connect_args={"check_same_thread": False} if "sqlite" in database_url else {},
    )
    try:
        with target_engine.begin() as target_connection:
            _upgrade_with_connection(target_connection)
    finally:
        target_engine.dispose()


def _upgrade_with_connection(connection: Connection) -> None:
    """在一个连接上完成历史库接管或正常迁移。"""

    if _has_migration_version(connection):
        _validate_migration_version(connection)
        _run_alembic("upgrade", connection=connection)
        _validate_schema(connection)
        return

    if _has_business_tables(connection):
        _ensure_legacy_added_columns(connection)
        _validate_schema(connection)
        # 补列与版本标记由外层事务一起提交；校验失败时绝不 stamp。
        _run_alembic("stamp", connection=connection, revision="head")
        logger.info("历史数据库已接管并标记为 Alembic head")
        return

    _run_alembic("upgrade", connection=connection)
    _validate_schema(connection)


def init_db() -> None:
    """初始化或升级数据库，应用启动和 CLI 均通过此入口。"""

    upgrade_database()


def _migrate_agent_task_columns() -> None:
    """兼容旧调用入口，仅补齐 agent_task 已知历史列。

    新的应用初始化统一使用 :func:`upgrade_database`；保留此函数是为了
    不破坏旧脚本和历史测试，不参与 Alembic 版本接管。
    """

    with engine.begin() as connection:
        _ensure_legacy_agent_task_columns(connection)


def get_db():
    """获取数据库会话（用于 FastAPI 依赖注入）。"""

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
