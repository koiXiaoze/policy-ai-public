#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""审计并安全修复政策库中的乱码和不可见字符。

设计原则：
1. 默认只读；只有显式 ``--apply`` 才写入。
2. 仅自动修复可逆的 Latin-1/CP1252 -> UTF-8 乱码，以及零宽/方向控制字符。
3. U+FFFD、``锟斤拷``、清洗后为空只报告，禁止猜测。
4. SQLite 写入前自动创建并校验备份；PostgreSQL 必须由外部备份确认。
5. 所有字段条件更新，在一个事务中完成，失败整体回滚；写入后自动复审计。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

try:
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.engine import make_url
except ImportError:  # pragma: no cover
    create_engine = inspect = text = make_url = None  # type: ignore[assignment]


DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "policies.db"
INVISIBLE_CHARS = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
REPLACEMENT_CHAR = "\ufffd"
LOSSY_MARKERS = (REPLACEMENT_CHAR, "锟斤拷")
CJK_CHAR = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

POLICY_TEXT_FIELDS = ("title", "department", "content", "summary", "keywords")
ANALYSIS_TEXT_FIELDS = (
    "summary", "plain_explanation", "national_direction", "risk_analysis", "raw_response",
)
ANALYSIS_JSON_FIELDS = ("industry_impact", "career_opportunity", "personal_advice")
TABLE_FIELDS = {
    "policy": POLICY_TEXT_FIELDS,
    "analysis": ANALYSIS_TEXT_FIELDS + ANALYSIS_JSON_FIELDS,
}
JSON_FIELDS = {"policy": set(), "analysis": set(ANALYSIS_JSON_FIELDS)}


class CleanupError(RuntimeError):
    """乱码处理流程的可预期失败。"""


class BackupError(CleanupError):
    """备份创建或完整性校验失败。"""


class ConcurrentModificationError(CleanupError):
    """字段在审计后被其他进程修改，拒绝覆盖。"""


@dataclass(frozen=True)
class FieldIssue:
    """一个具体字段值中的异常；JSON 字段按字符串叶子分别报告。"""

    table: str
    record_id: int
    policy_id: int
    field_name: str
    path: str
    original_value: str
    cleaned_value: str
    source_name: str | None = None
    url: str | None = None
    has_invisible: bool = False
    has_replacement_char: bool = False
    has_lossy_marker: bool = False
    has_mojibake: bool = False
    manually_review_only: bool = False
    review_reason: str | None = None
    original_sha256: str = ""
    json_path: tuple[str | int, ...] = field(default_factory=tuple, repr=False)
    original_field_value: str | None = field(default=None, repr=False)
    cleaned_field_value: str | None = field(default=None, repr=False)

    @property
    def original_title(self) -> str:
        return self.original_value

    @property
    def cleaned_title(self) -> str:
        return self.cleaned_value

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "record_id": self.record_id,
            "policy_id": self.policy_id,
            "field": self.field_name,
            "path": self.path,
            "source_name": self.source_name,
            "url": self.url,
            "original_value": self.original_value,
            "cleaned_value": self.cleaned_value,
            "original_sha256": self.original_sha256,
            "has_invisible": self.has_invisible,
            "has_replacement_char": self.has_replacement_char,
            "has_lossy_marker": self.has_lossy_marker,
            "has_mojibake": self.has_mojibake,
            "manually_review_only": self.manually_review_only,
            "review_reason": self.review_reason,
        }


TitleIssue = FieldIssue


@dataclass
class CleanupReport:
    """一次审计/修复的完整结果。"""

    database: str
    dialect: str
    apply: bool
    issues_before: list[FieldIssue]
    issues_after: list[FieldIssue]
    backup: dict[str, Any] | None = None
    updated_fields: int = 0
    unmatched_fields: int = 0
    verification_ok: bool = True

    def payload(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "dialect": self.dialect,
            "apply": self.apply,
            "summary_before": summarize_issues(self.issues_before),
            "summary_after": summarize_issues(self.issues_after),
            "backup": self.backup,
            "repair": {
                "updated_fields": self.updated_fields,
                "unmatched_fields": self.unmatched_fields,
                "verification_ok": self.verification_ok,
            },
            "issues_before": [issue.to_public_dict() for issue in self.issues_before],
            "issues_after": [issue.to_public_dict() for issue in self.issues_after],
        }

def _contains_lossy_marker(value: str) -> bool:
    return any(marker in value for marker in LOSSY_MARKERS)


def _is_reasonable_chinese(value: str) -> bool:
    """要求修复结果确有汉字，避免把普通西文误判为乱码。"""
    if not value or REPLACEMENT_CHAR in value or "锟斤拷" in value:
        return False
    visible = [char for char in value if not char.isspace()]
    if not visible or any(ord(char) < 32 and char not in "\t\r\n" for char in visible):
        return False
    han_count = sum(1 for char in value if CJK_CHAR.fullmatch(char))
    return han_count >= 1 and han_count / len(visible) >= 0.10


def _decode_mojibake_once(value: str) -> str | None:
    """严格尝试一次 Latin-1/CP1252 -> UTF-8 可逆解码。"""
    for encoding in ("latin1", "cp1252"):
        try:
            raw = value.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            continue
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if decoded.encode("utf-8") != raw:
            continue
        if decoded != value and _is_reasonable_chinese(decoded):
            return decoded
    return None


def repair_mojibake(value: str, *, max_layers: int = 3) -> tuple[str, bool]:
    """修复一到多层严格可逆乱码，返回 (结果, 是否发生修复)。"""
    current = value
    changed = False
    for _ in range(max_layers):
        decoded = _decode_mojibake_once(current)
        if decoded is None:
            break
        current = decoded
        changed = True
    return current, changed


def clean_text(value: str, *, trim: bool = False) -> tuple[str, dict[str, Any]]:
    """清洗一个文本值并返回诊断标记。人工复核场景不返回可写修复值。"""
    original = value
    normalized = INVISIBLE_CHARS.sub("", original)
    repaired, has_mojibake = repair_mojibake(normalized)
    if trim:
        repaired = repaired.strip()

    has_invisible = normalized != original
    has_replacement = REPLACEMENT_CHAR in original
    has_lossy_marker = _contains_lossy_marker(original)
    reason = None
    if has_replacement:
        reason = "包含 U+FFFD，原文信息已丢失"
    elif has_lossy_marker:
        reason = "包含锟斤拷等有损乱码标记"
    elif original and not repaired.strip():
        reason = "清洗后为空，禁止自动清空"
    manually_review = reason is not None
    if manually_review:
        repaired = original
    return repaired, {
        "has_invisible": has_invisible,
        "has_replacement_char": has_replacement,
        "has_lossy_marker": has_lossy_marker,
        "has_mojibake": has_mojibake,
        "manually_review_only": manually_review,
        "review_reason": reason,
        "changed": (not manually_review) and repaired != original,
    }


def _safe_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _json_path_text(path: tuple[str | int, ...]) -> str:
    parts: list[str] = ["$"]
    for item in path:
        if isinstance(item, int):
            parts.append(f"[{item}]")
        else:
            parts.append(f"[{json.dumps(item, ensure_ascii=False)}]")
    return "".join(parts)


def _walk_json_strings(value: Any, path: tuple[str | int, ...] = ()) -> Iterable[tuple[tuple[str | int, ...], str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_json_strings(item, path + (str(key),))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_json_strings(item, path + (index,))


def _set_json_path(root: Any, path: tuple[str | int, ...], new_value: str) -> Any:
    if not path:
        return new_value
    target = root
    for item in path[:-1]:
        target = target[item]
    target[path[-1]] = new_value
    return root


def _make_issue(
    *, table: str, record_id: int, policy_id: int, field_name: str, path: str,
    original: str, cleaned: str, flags: Mapping[str, Any],
    source_name: str | None, url: str | None,
    json_path: tuple[str | int, ...] = (), original_field_value: str | None = None,
) -> FieldIssue:
    return FieldIssue(
        table=table,
        record_id=int(record_id),
        policy_id=int(policy_id),
        field_name=field_name,
        path=path,
        original_value=original,
        cleaned_value=cleaned,
        source_name=source_name,
        url=url,
        has_invisible=bool(flags["has_invisible"]),
        has_replacement_char=bool(flags["has_replacement_char"]),
        has_lossy_marker=bool(flags["has_lossy_marker"]),
        has_mojibake=bool(flags["has_mojibake"]),
        manually_review_only=bool(flags["manually_review_only"]),
        review_reason=flags.get("review_reason"),
        original_sha256=_safe_sha256(original),
        json_path=json_path,
        original_field_value=original_field_value,
    )


def _scan_row(
    *, table: str, record_id: int, policy_id: int, row: Mapping[str, Any],
    source_name: str | None, url: str | None,
) -> list[FieldIssue]:
    issues: list[FieldIssue] = []
    for field_name in TABLE_FIELDS[table]:
        value = row.get(field_name)
        if value is None:
            continue
        if field_name in JSON_FIELDS[table]:
            raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            try:
                parsed = json.loads(raw) if isinstance(value, str) else value
            except (json.JSONDecodeError, TypeError) as exc:
                issues.append(_make_issue(
                    table=table, record_id=record_id, policy_id=policy_id,
                    field_name=field_name, path="$", original=raw, cleaned=raw,
                    flags={
                        "has_invisible": False,
                        "has_replacement_char": REPLACEMENT_CHAR in raw,
                        "has_lossy_marker": _contains_lossy_marker(raw),
                        "has_mojibake": False,
                        "manually_review_only": True,
                        "review_reason": f"JSON 无法解析: {exc}",
                    },
                    source_name=source_name, url=url, original_field_value=raw,
                ))
                continue
            for json_path, original in _walk_json_strings(parsed):
                cleaned, flags = clean_text(original)
                if not flags["changed"] and not flags["manually_review_only"]:
                    continue
                issues.append(_make_issue(
                    table=table, record_id=record_id, policy_id=policy_id,
                    field_name=field_name, path=_json_path_text(json_path),
                    original=original, cleaned=cleaned, flags=flags,
                    source_name=source_name, url=url, json_path=json_path,
                    original_field_value=raw,
                ))
            continue
        original = str(value)
        cleaned, flags = clean_text(
            original, trim=field_name in {"title", "department", "summary", "keywords"}
        )
        if not flags["changed"] and not flags["manually_review_only"]:
            continue
        issues.append(_make_issue(
            table=table, record_id=record_id, policy_id=policy_id,
            field_name=field_name, path="$", original=original, cleaned=cleaned,
            flags=flags, source_name=source_name, url=url,
            original_field_value=original,
        ))
    return issues

def _resolve_sqlite_path(target: Any) -> Path | None:
    if isinstance(target, Path):
        return target.expanduser().resolve()
    if not isinstance(target, str):
        return None
    if target.startswith("sqlite:///") or target.startswith("sqlite://"):
        if make_url is None:
            raise CleanupError("解析 SQLite URL 需要 SQLAlchemy")
        database = make_url(target).database
        if not database:
            raise ValueError("SQLite URL 缺少数据库路径")
        return Path(database).expanduser().resolve()
    if "://" not in target:
        return Path(target).expanduser().resolve()
    return None


def _display_database(target: Any) -> tuple[str, str]:
    sqlite_path = _resolve_sqlite_path(target)
    if sqlite_path is not None:
        return str(sqlite_path), "sqlite"
    if make_url is None:
        return str(target), "unknown"
    try:
        url = make_url(str(target))
        return url.render_as_string(hide_password=True), url.get_backend_name()
    except Exception:
        return "[unparseable-database-url]", "unknown"


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    """以只读模式打开 SQLite，确保预览阶段不可能修改正式库。"""
    resolved = db_path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"数据库不存在: {resolved}")
    uri_path = quote(resolved.as_posix(), safe="/:")
    return sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)


def _sqlite_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _sqlite_ensure_policy_table(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='policy'"
    ).fetchone()
    if not row:
        raise ValueError("数据库中不存在 policy 表，已拒绝处理")


def _sqlite_read_rows(db_path: Path) -> list[tuple[str, int, int, dict[str, Any], str | None, str | None]]:
    with _connect_readonly(db_path) as connection:
        _sqlite_ensure_policy_table(connection)
        policy_columns = _sqlite_columns(connection, "policy")
        analysis_exists = bool(connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis'"
        ).fetchone())
        analysis_columns = _sqlite_columns(connection, "analysis") if analysis_exists else set()
        policy_fields = [name for name in POLICY_TEXT_FIELDS if name in policy_columns]
        policy_select = ["id", *policy_fields]
        if "source_name" in policy_columns:
            policy_select.append("source_name")
        if "url" in policy_columns:
            policy_select.append("url")
        quoted = ", ".join(chr(34) + name + chr(34) for name in policy_select)
        policy_rows = connection.execute(f"SELECT {quoted} FROM policy ORDER BY id").fetchall()
        policy_map: dict[int, tuple[str | None, str | None]] = {}
        results: list[tuple[str, int, int, dict[str, Any], str | None, str | None]] = []
        for raw_row in policy_rows:
            values = dict(zip(policy_select, raw_row, strict=True))
            policy_id = int(values["id"])
            source_name = values.get("source_name")
            url = values.get("url")
            policy_map[policy_id] = (source_name, url)
            results.append(("policy", policy_id, policy_id, values, source_name, url))
        if analysis_exists:
            analysis_fields = [name for name in TABLE_FIELDS["analysis"] if name in analysis_columns]
            select_fields = ["id", "policy_id", *analysis_fields]
            quoted = ", ".join(chr(34) + name + chr(34) for name in select_fields)
            rows = connection.execute(f"SELECT {quoted} FROM analysis ORDER BY id").fetchall()
            for raw_row in rows:
                values = dict(zip(select_fields, raw_row, strict=True))
                policy_id = int(values["policy_id"])
                source_name, url = policy_map.get(policy_id, (None, None))
                results.append(("analysis", int(values["id"]), policy_id, values, source_name, url))
    return results


def _sqlalchemy_read_rows(database_url: str) -> list[tuple[str, int, int, dict[str, Any], str | None, str | None]]:
    if create_engine is None or inspect is None or text is None:
        raise CleanupError("PostgreSQL 审计需要 SQLAlchemy")
    engine = create_engine(database_url)
    results: list[tuple[str, int, int, dict[str, Any], str | None, str | None]] = []
    try:
        with engine.connect() as connection:
            table_names = set(inspect(connection).get_table_names())
            if "policy" not in table_names:
                raise ValueError("数据库中不存在 policy 表，已拒绝处理")
            column_map = {
                table: {column["name"] for column in inspect(connection).get_columns(table)}
                for table in ("policy", "analysis") if table in table_names
            }

            def select_table(table: str, columns: Sequence[str]) -> list[Mapping[str, Any]]:
                present = [name for name in columns if name in column_map[table]]
                select_parts = ['"id"']
                if table == "analysis":
                    select_parts.append('"policy_id"')
                for name in present:
                    if name in JSON_FIELDS[table]:
                        select_parts.append(f'CAST("{name}" AS TEXT) AS "{name}"')
                    else:
                        select_parts.append(f'"{name}"')
                if table == "policy" and "source_name" in column_map[table]:
                    select_parts.append('"source_name"')
                if table == "policy" and "url" in column_map[table]:
                    select_parts.append('"url"')
                statement = text(f'SELECT {", ".join(select_parts)} FROM "{table}" ORDER BY "id"')
                return list(connection.execute(statement).mappings())

            policy_map: dict[int, tuple[str | None, str | None]] = {}
            for row in select_table("policy", POLICY_TEXT_FIELDS):
                values = dict(row)
                policy_id = int(values["id"])
                source_name = values.get("source_name")
                url = values.get("url")
                policy_map[policy_id] = (source_name, url)
                results.append(("policy", policy_id, policy_id, values, source_name, url))
            if "analysis" in table_names:
                for row in select_table("analysis", TABLE_FIELDS["analysis"]):
                    values = dict(row)
                    policy_id = int(values["policy_id"])
                    source_name, url = policy_map.get(policy_id, (None, None))
                    results.append(("analysis", int(values["id"]), policy_id, values, source_name, url))
    finally:
        engine.dispose()
    return results


def find_issues(database: Path | str = DEFAULT_DB_PATH) -> list[FieldIssue]:
    """只读扫描 Policy 与 Analysis 的全部目标字段。"""
    sqlite_path = _resolve_sqlite_path(database)
    rows = _sqlite_read_rows(sqlite_path) if sqlite_path is not None else _sqlalchemy_read_rows(str(database))
    issues: list[FieldIssue] = []
    for table, record_id, policy_id, row, source_name, url in rows:
        issues.extend(_scan_row(
            table=table, record_id=record_id, policy_id=policy_id, row=row,
            source_name=source_name, url=url,
        ))
    return issues


def find_title_issues(db_path: Path | str) -> list[FieldIssue]:
    """兼容旧 API：只返回 policy.title 字段的问题。"""
    return [
        issue for issue in find_issues(db_path)
        if issue.table == "policy" and issue.field_name == "title"
    ]

@dataclass(frozen=True)
class _GroupKey:
    table: str
    record_id: int
    field_name: str


def _candidate_groups(issues: Iterable[FieldIssue]) -> dict[_GroupKey, list[FieldIssue]]:
    grouped: dict[_GroupKey, list[FieldIssue]] = {}
    for issue in issues:
        key = _GroupKey(issue.table, issue.record_id, issue.field_name)
        grouped.setdefault(key, []).append(issue)
    # JSON 字段中只要有一个叶子不可安全处理，整个字段转人工。
    return {
        key: group for key, group in grouped.items()
        if not any(issue.manually_review_only for issue in group)
    }


def _group_cleaned_value(group: list[FieldIssue]) -> str:
    first = group[0]
    if first.field_name not in JSON_FIELDS[first.table]:
        return first.cleaned_value
    raw = first.original_field_value
    if raw is None:
        raise CleanupError("JSON 字段缺少用于条件更新的原值")
    parsed = json.loads(raw)
    for issue in group:
        parsed = _set_json_path(parsed, issue.json_path, issue.cleaned_value)
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _validate_identifier(table: str, field_name: str) -> None:
    if table not in TABLE_FIELDS or field_name not in TABLE_FIELDS[table]:
        raise ValueError(f"拒绝更新未授权字段: {table}.{field_name}")


def _apply_sqlite(db_path: Path, issues: Iterable[FieldIssue], *, fail_on_unmatched: bool) -> tuple[int, int]:
    groups = _candidate_groups(issues)
    if not groups:
        return 0, 0
    updated = 0
    unmatched = 0
    connection = sqlite3.connect(db_path)
    try:
        _sqlite_ensure_policy_table(connection)
        connection.execute("BEGIN IMMEDIATE")
        for key, group in groups.items():
            _validate_identifier(key.table, key.field_name)
            original = group[0].original_field_value
            if original is None:
                raise CleanupError(f"{key.table}.{key.field_name} 缺少原值")
            new_value = _group_cleaned_value(group)
            cursor = connection.execute(
                f'UPDATE "{key.table}" SET "{key.field_name}" = ? '
                f'WHERE "id" = ? AND "{key.field_name}" = ?',
                (new_value, key.record_id, original),
            )
            if cursor.rowcount == 1:
                updated += 1
            else:
                unmatched += 1
        if fail_on_unmatched and unmatched:
            raise ConcurrentModificationError(
                f"{unmatched} 个字段在审计后被修改，已回滚全部更新"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return updated, unmatched


def _apply_sqlalchemy(
    database_url: str, issues: Iterable[FieldIssue], *, fail_on_unmatched: bool,
) -> tuple[int, int]:
    groups = _candidate_groups(issues)
    if not groups:
        return 0, 0
    if create_engine is None or text is None:
        raise CleanupError("PostgreSQL 修复需要 SQLAlchemy")
    engine = create_engine(database_url)
    updated = 0
    unmatched = 0
    try:
        with engine.begin() as connection:
            for key, group in groups.items():
                _validate_identifier(key.table, key.field_name)
                original = group[0].original_field_value
                if original is None:
                    raise CleanupError(f"{key.table}.{key.field_name} 缺少原值")
                new_value = _group_cleaned_value(group)
                set_expression = "CAST(:new_value AS JSON)" if key.field_name in JSON_FIELDS[key.table] else ":new_value"
                statement = text(
                    f'UPDATE "{key.table}" SET "{key.field_name}" = {set_expression} '
                    f'WHERE "id" = :record_id '
                    f'AND CAST("{key.field_name}" AS TEXT) = :original_value'
                )
                result = connection.execute(statement, {
                    "new_value": new_value,
                    "record_id": key.record_id,
                    "original_value": original,
                })
                if result.rowcount == 1:
                    updated += 1
                else:
                    unmatched += 1
            if fail_on_unmatched and unmatched:
                raise ConcurrentModificationError(
                    f"{unmatched} 个字段在审计后被修改，已回滚全部更新"
                )
    finally:
        engine.dispose()
    return updated, unmatched


def apply_field_repairs(
    database: Path | str, issues: Iterable[FieldIssue], *, fail_on_unmatched: bool = True,
) -> tuple[int, int]:
    """条件更新可安全修复字段，返回 (成功字段数, 未匹配字段数)。"""
    sqlite_path = _resolve_sqlite_path(database)
    if sqlite_path is not None:
        return _apply_sqlite(sqlite_path, issues, fail_on_unmatched=fail_on_unmatched)
    return _apply_sqlalchemy(str(database), issues, fail_on_unmatched=fail_on_unmatched)


def apply_title_repairs(db_path: Path | str, issues: Iterable[FieldIssue]) -> tuple[int, int]:
    """兼容旧 API：只修 policy.title；未匹配时保留其他成功项。"""
    title_issues = [
        issue for issue in issues
        if issue.table == "policy" and issue.field_name == "title"
    ]
    return apply_field_repairs(db_path, title_issues, fail_on_unmatched=False)

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_sqlite_backup(db_path: Path, *, backup_dir: Path | None = None) -> dict[str, Any]:
    """通过 SQLite Backup API 创建一致性备份并完成完整性校验。"""
    resolved = db_path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"数据库不存在: {resolved}")
    directory = (backup_dir or resolved.parent).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = directory / f"{resolved.name}.bak-{stamp}-before-garbled-cleanup"
    source = sqlite3.connect(f"file:{quote(resolved.as_posix(), safe='/:')}?mode=ro", uri=True)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
        destination.commit()
        integrity = destination.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise BackupError(f"备份完整性校验失败: {integrity!r}")
        source_count = source.execute("SELECT COUNT(*) FROM policy").fetchone()[0]
        backup_count = destination.execute("SELECT COUNT(*) FROM policy").fetchone()[0]
        if source_count != backup_count:
            raise BackupError(f"备份行数不一致: source={source_count}, backup={backup_count}")
    except Exception:
        destination.close()
        source.close()
        try:
            backup_path.unlink()
        except OSError:
            pass
        raise
    finally:
        try:
            destination.close()
        finally:
            source.close()
    digest = _sha256_file(backup_path)
    return {
        "kind": "sqlite_backup_api",
        "path": str(backup_path),
        "sha256": digest,
        "size_bytes": backup_path.stat().st_size,
        "integrity_check": "ok",
        "policy_row_count": backup_count,
        "verified": True,
    }


def _prepare_backup(
    database: Path | str, *, backup_dir: Path | None,
    external_backup_confirmed: bool, backup_reference: str | None,
) -> dict[str, Any]:
    sqlite_path = _resolve_sqlite_path(database)
    if sqlite_path is not None:
        return create_sqlite_backup(sqlite_path, backup_dir=backup_dir)
    if not external_backup_confirmed or not backup_reference or not backup_reference.strip():
        raise BackupError(
            "PostgreSQL 无法由本脚本创建可信文件备份；--apply 前必须完成外部备份，"
            "并提供 --confirm-external-backup 与 --backup-reference"
        )
    return {
        "kind": "external_postgresql_backup",
        "reference": backup_reference.strip(),
        "verified": False,
        "verification": "not_verifiable_by_script",
        "note": "脚本只记录外部备份凭据，不声称已验证；请使用 pg_dump --format=custom 并做恢复演练。",
    }


def summarize_issues(issues: Iterable[FieldIssue]) -> dict[str, Any]:
    issue_list = list(issues)
    grouped: dict[_GroupKey, list[FieldIssue]] = {}
    by_field: dict[str, int] = {}
    for issue in issue_list:
        key = _GroupKey(issue.table, issue.record_id, issue.field_name)
        grouped.setdefault(key, []).append(issue)
        field_key = f"{issue.table}.{issue.field_name}"
        by_field[field_key] = by_field.get(field_key, 0) + 1
    manual_groups = {
        key for key, group in grouped.items() if any(item.manually_review_only for item in group)
    }
    repairable_groups = {
        key for key, group in grouped.items() if not any(item.manually_review_only for item in group)
    }
    unrecoverable = sum(
        1 for issue in issue_list if issue.has_replacement_char or issue.has_lossy_marker
    )
    return {
        "issue_count": len(issue_list),
        "affected_fields": len(grouped),
        "auto_repairable_fields": len(repairable_groups),
        "manual_review_fields": len(manual_groups),
        "manual_review_values": sum(len(grouped[key]) for key in manual_groups),
        "unrecoverable_values": unrecoverable,
        "by_field": dict(sorted(by_field.items())),
    }


def run_cleanup(
    database: Path | str = DEFAULT_DB_PATH, *, apply: bool = False,
    backup_dir: Path | None = None, external_backup_confirmed: bool = False,
    backup_reference: str | None = None,
) -> CleanupReport:
    """执行“审计 -> 备份 -> 修复 -> 复审计”的完整流程。"""
    display, dialect = _display_database(database)
    before = find_issues(database)
    backup = None
    updated = 0
    unmatched = 0
    after = before
    if apply:
        backup = _prepare_backup(
            database, backup_dir=backup_dir,
            external_backup_confirmed=external_backup_confirmed,
            backup_reference=backup_reference,
        )
        updated, unmatched = apply_field_repairs(database, before, fail_on_unmatched=True)
        after = find_issues(database)
    remaining_repairable = summarize_issues(after)["auto_repairable_fields"] if apply else 0
    return CleanupReport(
        database=display, dialect=dialect, apply=apply,
        issues_before=before, issues_after=after, backup=backup,
        updated_fields=updated, unmatched_fields=unmatched,
        verification_ok=(not apply) or remaining_repairable == 0,
    )


def cleanup(
    db_path: Path | str, apply: bool = False, *, backup_dir: Path | None = None,
    external_backup_confirmed: bool = False, backup_reference: str | None = None,
) -> list[FieldIssue]:
    """兼容旧 API，返回修复前问题清单。"""
    return run_cleanup(
        db_path, apply=apply, backup_dir=backup_dir,
        external_backup_confirmed=external_backup_confirmed,
        backup_reference=backup_reference,
    ).issues_before

def _print_human(report: CleanupReport) -> None:
    print(f"数据库: {report.database}")
    before = summarize_issues(report.issues_before)
    print(f"发现异常值: {before['issue_count']} 个，涉及 {before['affected_fields']} 个字段")
    print(
        f"可自动修复字段: {before['auto_repairable_fields']}；"
        f"人工复核字段: {before['manual_review_fields']}；"
        f"不可恢复值: {before['unrecoverable_values']}"
    )
    for issue in report.issues_before:
        label = "需人工复核" if issue.manually_review_only else "可自动修复"
        print(
            f"  [{label}] {issue.table}.{issue.field_name}{issue.path} "
            f"ID={issue.record_id} 来源={issue.source_name or '-'}"
        )
        print(f"    原值: {issue.original_value!r}")
        if not issue.manually_review_only:
            print(f"    修复后: {issue.cleaned_value!r}")
        print(f"    URL: {issue.url or '-'}")
    if report.apply:
        print(f"备份: {report.backup}")
        print(f"已更新字段: {report.updated_fields}；未匹配: {report.unmatched_fields}")
        after = summarize_issues(report.issues_after)
        print(
            f"复审计：剩余异常值 {after['issue_count']}，"
            f"其中可自动修复字段 {after['auto_repairable_fields']}"
        )
    else:
        print("预览完成，未修改数据库。")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="审计并安全修复政策库乱码；默认 dry-run，不修改数据库。"
    )
    parser.add_argument(
        "--db", default=str(DEFAULT_DB_PATH),
        help="SQLite 路径或 SQLAlchemy 数据库 URL（默认: data/policies.db）",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="实际执行可逆修复；SQLite 自动备份，PostgreSQL 必须确认外部备份",
    )
    parser.add_argument(
        "--backup-dir", type=Path, default=None,
        help="SQLite 备份目录（默认与源库同目录）",
    )
    parser.add_argument(
        "--confirm-external-backup", action="store_true",
        help="确认 PostgreSQL 已在脚本外完成备份与恢复演练",
    )
    parser.add_argument(
        "--backup-reference", default=None,
        help="PostgreSQL 外部备份标识，例如 pg_dump 文件名或对象存储路径",
    )
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 报告")
    return parser


def _write_json(payload: Mapping[str, Any]) -> None:
    serialized = json.dumps(payload, ensure_ascii=True, indent=2)
    try:
        sys.stdout.buffer.write((serialized + "\n").encode("utf-8"))
        sys.stdout.flush()
    except AttributeError:
        print(serialized)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = run_cleanup(
            args.db, apply=args.apply, backup_dir=args.backup_dir,
            external_backup_confirmed=args.confirm_external_backup,
            backup_reference=args.backup_reference,
        )
    except (OSError, sqlite3.Error, ValueError, CleanupError) as exc:
        print(f"处理失败: {exc}", file=sys.stderr)
        return 1
    if args.json:
        _write_json(report.payload())
    else:
        _print_human(report)
    return 0 if report.verification_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
