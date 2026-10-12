"""存储层：SQLite 持久化 + 哈希链审计日志。

.. code-block:: python

    from fund_risk_sentinel.storage import open_storage

    repo, audit = open_storage()          # 按配置定位数据库
    repo.save_task(task)
    repo.save_conclusion(conclusion)
    audit.log_conclusion(conclusion.conclusion_id, task.task_id, ...)

    ok, message = audit.verify_chain()     # 校验审计链完整性
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .audit import AuditLogger, mask_secret
from .db import SCHEMA_VERSION, Database
from .repository import Repository

if TYPE_CHECKING:  # pragma: no cover
    from ..config.settings import ConfigManager


def open_storage(
    config: "ConfigManager | None" = None,
    *,
    db_path: Path | str | None = None,
) -> tuple[Repository, AuditLogger]:
    """按配置打开数据库与审计日志。"""
    if config is None:
        from ..config.settings import get_manager

        config = get_manager()

    path = Path(db_path) if db_path else config.settings.resolved_db_path()
    config.settings.ensure_dirs()
    db = Database(path)
    audit = AuditLogger(
        db,
        log_dir=config.settings.resolved_log_dir(),
        enabled=config.settings.security.audit_log_enabled,
    )
    return Repository(db), audit


__all__ = [
    "Database",
    "Repository",
    "AuditLogger",
    "SCHEMA_VERSION",
    "open_storage",
    "mask_secret",
]
