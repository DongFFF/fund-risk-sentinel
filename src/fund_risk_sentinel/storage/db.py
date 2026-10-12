"""SQLite 存储底座。

选 SQLite 而不是服务端数据库，是为了让 exe 真正"零门槛"：单文件、免安装、
可随材料目录一起拷贝、断网可用。表结构按五元框架 + 流程轨迹设计，
所有复杂对象以 JSON 列存储，既保留完整信息，又不牺牲可查询性。

数据库文件默认位于用户数据目录（``%LOCALAPPDATA%\\FundRiskSentinel\\sentinel.db``），
不落在程序目录，避免 Program Files 只读问题。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..core.exceptions import StorageError

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- 审查任务
CREATE TABLE IF NOT EXISTS tasks (
    task_id         TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    event_type      TEXT NOT NULL DEFAULT 'other',
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    reviewer        TEXT NOT NULL DEFAULT '',
    project_name    TEXT NOT NULL DEFAULT '',
    project_code    TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    material_count  INTEGER NOT NULL DEFAULT 0,
    finding_count   INTEGER NOT NULL DEFAULT 0,
    high_count      INTEGER NOT NULL DEFAULT 0,
    overall_level   TEXT NOT NULL DEFAULT 'unknown'
);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

-- ---------------------------------------------------------------- 材料（证据）
CREATE TABLE IF NOT EXISTS materials (
    material_id       TEXT PRIMARY KEY,
    task_id           TEXT NOT NULL,
    filename          TEXT NOT NULL,
    path              TEXT NOT NULL DEFAULT '',
    fmt               TEXT NOT NULL DEFAULT 'unknown',
    role              TEXT NOT NULL DEFAULT 'other',
    size_bytes        INTEGER NOT NULL DEFAULT 0,
    sha256            TEXT NOT NULL DEFAULT '',
    page_count        INTEGER NOT NULL DEFAULT 0,
    char_count        INTEGER NOT NULL DEFAULT 0,
    text              TEXT NOT NULL DEFAULT '',
    tables_json       TEXT NOT NULL DEFAULT '[]',
    metadata_json     TEXT NOT NULL DEFAULT '{}',
    redaction_applied INTEGER NOT NULL DEFAULT 0,
    redaction_log_json TEXT NOT NULL DEFAULT '[]',
    source            TEXT NOT NULL DEFAULT 'local_upload',
    uploaded_at       TEXT NOT NULL,
    retention_until   TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_materials_task ON materials(task_id);
CREATE INDEX IF NOT EXISTS idx_materials_sha ON materials(sha256);

-- ---------------------------------------------------------------- 结论
CREATE TABLE IF NOT EXISTS conclusions (
    conclusion_id        TEXT PRIMARY KEY,
    task_id              TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'pending',
    event_type           TEXT NOT NULL DEFAULT 'other',
    summary              TEXT NOT NULL DEFAULT '',
    overall_level        TEXT NOT NULL DEFAULT 'unknown',
    statistics_json      TEXT NOT NULL DEFAULT '{}',
    extracted_json       TEXT,
    trace_json           TEXT NOT NULL DEFAULT '[]',
    usage_json           TEXT NOT NULL DEFAULT '{}',
    generated_at         TEXT NOT NULL,
    duration_ms          INTEGER NOT NULL DEFAULT 0,
    provider             TEXT NOT NULL DEFAULT '',
    model                TEXT NOT NULL DEFAULT '',
    adaptation_paths_json TEXT NOT NULL DEFAULT '[]',
    degraded_stages_json TEXT NOT NULL DEFAULT '[]',
    disclaimer           TEXT NOT NULL DEFAULT '',
    payload_json         TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_conclusions_task ON conclusions(task_id);
CREATE INDEX IF NOT EXISTS idx_conclusions_time ON conclusions(generated_at DESC);

-- ---------------------------------------------------------------- 风险线索
CREATE TABLE IF NOT EXISTS findings (
    finding_id        TEXT PRIMARY KEY,
    conclusion_id     TEXT NOT NULL,
    task_id           TEXT NOT NULL,
    title             TEXT NOT NULL,
    category          TEXT NOT NULL DEFAULT 'deviation',
    level             TEXT NOT NULL DEFAULT 'unknown',
    event_type        TEXT NOT NULL DEFAULT 'other',
    description       TEXT NOT NULL DEFAULT '',
    rationale         TEXT NOT NULL DEFAULT '',
    matched_clauses_json TEXT NOT NULL DEFAULT '[]',
    evidences_json    TEXT NOT NULL DEFAULT '[]',
    counter_evidence_json TEXT NOT NULL DEFAULT '[]',
    dispositions_json TEXT NOT NULL DEFAULT '[]',
    suggestion        TEXT NOT NULL DEFAULT '',
    confidence        REAL NOT NULL DEFAULT 0,
    produced_by       TEXT NOT NULL DEFAULT '',
    provider          TEXT NOT NULL DEFAULT '',
    model             TEXT NOT NULL DEFAULT '',
    needs_human_review INTEGER NOT NULL DEFAULT 1,
    human_verdict     TEXT NOT NULL DEFAULT 'pending',
    human_note        TEXT NOT NULL DEFAULT '',
    reviewed_by       TEXT NOT NULL DEFAULT '',
    reviewed_at       TEXT,
    created_at        TEXT NOT NULL,
    FOREIGN KEY (conclusion_id) REFERENCES conclusions(conclusion_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_findings_task ON findings(task_id);
CREATE INDEX IF NOT EXISTS idx_findings_verdict ON findings(human_verdict);
CREATE INDEX IF NOT EXISTS idx_findings_level ON findings(level);

-- ---------------------------------------------------------------- 处置
CREATE TABLE IF NOT EXISTS dispositions (
    disposition_id  TEXT PRIMARY KEY,
    finding_id      TEXT NOT NULL,
    type            TEXT NOT NULL DEFAULT 'pending_review',
    note            TEXT NOT NULL DEFAULT '',
    actor           TEXT NOT NULL DEFAULT '',
    role            TEXT NOT NULL DEFAULT '',
    decided_at      TEXT,
    is_exception    INTEGER NOT NULL DEFAULT 0,
    exception_reason TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (finding_id) REFERENCES findings(finding_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_dispositions_finding ON dispositions(finding_id);

-- ---------------------------------------------------------------- 审计日志
-- prev_hash / entry_hash 构成哈希链：任何一条日志被篡改，后续链路校验即失败，
-- 支撑课题要求的"全过程可解释、可追溯、可问责"。
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    level        TEXT NOT NULL DEFAULT 'info',
    action       TEXT NOT NULL,
    actor        TEXT NOT NULL DEFAULT 'system',
    target_type  TEXT NOT NULL DEFAULT '',
    target_id    TEXT NOT NULL DEFAULT '',
    detail_json  TEXT NOT NULL DEFAULT '{}',
    prev_hash    TEXT NOT NULL DEFAULT '',
    entry_hash   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);

-- ---------------------------------------------------------------- 评测报告
CREATE TABLE IF NOT EXISTS evaluations (
    report_id     TEXT PRIMARY KEY,
    generated_at  TEXT NOT NULL,
    task_ids_json TEXT NOT NULL DEFAULT '[]',
    metrics_json  TEXT NOT NULL DEFAULT '[]',
    notes_json    TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_eval_time ON evaluations(generated_at DESC);

-- ---------------------------------------------------------------- 元信息
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    """SQLite 连接管理（线程安全）。

    每个线程持有独立连接，避免 SQLite 的跨线程限制；WAL 模式让"界面读"与
    "后台审查写"可以并行，不会互相阻塞。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._lock = threading.Lock()
        self.init_schema()

    # ---------------------------------------------------------- 连接

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            try:
                conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
            except sqlite3.Error as exc:
                raise StorageError(f"无法打开数据库 {self.path}：{exc}") from exc
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """事务上下文：异常时回滚。"""
        conn = self.connect()
        with self._lock:
            try:
                conn.execute("BEGIN")
                yield conn
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    # ---------------------------------------------------------- 初始化

    def init_schema(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        except sqlite3.Error as exc:
            raise StorageError(f"数据库初始化失败：{exc}") from exc

    def schema_version(self) -> int:
        row = self.connect().execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row["value"]) if row else 0

    # ---------------------------------------------------------- 查询

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        try:
            return list(self.connect().execute(sql, params).fetchall())
        except sqlite3.Error as exc:
            raise StorageError(f"查询失败：{exc}\nSQL: {sql}") from exc

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        try:
            cursor = self.connect().execute(sql, params)
            return cursor.rowcount
        except sqlite3.Error as exc:
            raise StorageError(f"执行失败：{exc}\nSQL: {sql}") from exc

    def executemany(self, sql: str, seq: list[tuple[Any, ...]]) -> int:
        try:
            cursor = self.connect().executemany(sql, seq)
            return cursor.rowcount
        except sqlite3.Error as exc:
            raise StorageError(f"批量执行失败：{exc}\nSQL: {sql}") from exc

    # ---------------------------------------------------------- 维护

    def table_counts(self) -> dict[str, int]:
        tables = ("tasks", "materials", "conclusions", "findings", "dispositions", "audit_log", "evaluations")
        out: dict[str, int] = {}
        for table in tables:
            row = self.query_one(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608 - 表名来自白名单
            out[table] = int(row["n"]) if row else 0
        return out

    def vacuum(self) -> None:
        self.connect().execute("VACUUM")

    def size_bytes(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0


__all__ = ["Database", "SCHEMA_SQL", "SCHEMA_VERSION"]
