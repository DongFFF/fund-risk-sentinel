"""审计日志——哈希链式防篡改记录。

课题第五章明确要求"完整记录数据来源、模型版本、输出结果与人工调整信息，
确保全过程可解释、可追溯、可问责"。本模块就是这条要求的落地：

- **哈希链**：每条日志的 ``entry_hash = sha256(prev_hash + 时间 + 动作 + 内容)``。
  任何人改动中间任意一条，``verify_chain()`` 都会定位到断链位置。
- **双写**：同时写 SQLite（可查询、可关联）与按日切分的文本日志（可人工翻阅、
  可随材料目录归档）。
- **敏感信息不入日志**：只记录 API Key 的掩码形式、材料指纹而非全文。

审计日志写入失败会抛 ``AuditError``——这类失败必须暴露，不能被静默吞掉，
否则"可追溯"就是一句空话。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.exceptions import AuditError
from .db import Database

logger = logging.getLogger(__name__)

#: 需要掩码的字段名（避免把 Key、口令写进日志）
_SENSITIVE_KEYS = ("api_key", "apikey", "token", "password", "secret", "authorization")


def mask_secret(value: str) -> str:
    """API Key 掩码：保留前 4 位与后 4 位，中间打码。"""
    if not value:
        return ""
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def _sanitize(detail: dict[str, Any]) -> dict[str, Any]:
    """递归清理敏感字段。"""
    out: dict[str, Any] = {}
    for key, value in detail.items():
        lowered = str(key).lower()
        if any(s in lowered for s in _SENSITIVE_KEYS):
            out[key] = mask_secret(str(value))
        elif isinstance(value, dict):
            out[key] = _sanitize(value)
        elif isinstance(value, (list, tuple)):
            out[key] = [
                _sanitize(v) if isinstance(v, dict) else v for v in value
            ]
        else:
            out[key] = value
    return out


class AuditLogger:
    """审计日志记录器。"""

    def __init__(
        self,
        db: Database,
        *,
        log_dir: Path | str | None = None,
        enabled: bool = True,
        actor: str = "system",
    ) -> None:
        self.db = db
        self.enabled = enabled
        self.actor = actor
        self.log_dir = Path(log_dir) if log_dir else None
        if self.log_dir and self.enabled:
            self.log_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------- 写入

    def log(
        self,
        action: str,
        *,
        target_type: str = "",
        target_id: str = "",
        detail: dict[str, Any] | None = None,
        level: str = "info",
        actor: str | None = None,
    ) -> int:
        """写入一条审计记录，返回记录 ID。"""
        if not self.enabled:
            return 0

        ts = datetime.now(timezone.utc).isoformat()
        safe_detail = _sanitize(detail or {})
        actor_name = actor or self.actor
        detail_json = json.dumps(safe_detail, ensure_ascii=False, default=str)

        try:
            prev_hash = self._last_hash()
            entry_hash = self._compute_hash(
                prev_hash, ts, level, action, actor_name, target_type, target_id, detail_json
            )
            with self.db.transaction() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO audit_log (ts, level, action, actor, target_type, target_id,
                                           detail_json, prev_hash, entry_hash)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (ts, level, action, actor_name, target_type, target_id, detail_json,
                     prev_hash, entry_hash),
                )
                record_id = cursor.lastrowid or 0
        except Exception as exc:  # noqa: BLE001 - 审计失败必须暴露
            raise AuditError(f"审计日志写入失败（action={action}）：{exc}") from exc

        self._write_text_log(ts, level, action, actor_name, target_type, target_id, safe_detail)
        return record_id

    def _last_hash(self) -> str:
        row = self.db.query_one("SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1")
        return str(row["entry_hash"]) if row else ""

    @staticmethod
    def _compute_hash(*parts: str) -> str:
        digest = hashlib.sha256()
        for part in parts:
            digest.update(str(part).encode("utf-8"))
            digest.update(b"\x1f")
        return digest.hexdigest()

    def _write_text_log(
        self,
        ts: str,
        level: str,
        action: str,
        actor: str,
        target_type: str,
        target_id: str,
        detail: dict[str, Any],
    ) -> None:
        if not self.log_dir:
            return
        try:
            path = self.log_dir / f"audit-{ts[:10]}.log"
            line = (
                f"{ts}\t{level.upper()}\t{action}\tactor={actor}"
                f"\ttarget={target_type}:{target_id}\t{json.dumps(detail, ensure_ascii=False)}"
            )
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:  # 文本日志失败不阻断主流程，但要在应用日志里留痕
            logger.warning("审计文本日志写入失败：%s", exc)

    # ---------------------------------------------------------- 语义化快捷方法

    def log_task_created(self, task_id: str, title: str, material_count: int) -> int:
        return self.log(
            "task.created",
            target_type="task",
            target_id=task_id,
            detail={"title": title, "material_count": material_count},
        )

    def log_ingest(
        self,
        task_id: str,
        *,
        summary: str,
        materials: list[dict[str, Any]],
        warnings: list[str] | None = None,
    ) -> int:
        """记录材料上传：只记指纹与元信息，不记正文。"""
        return self.log(
            "material.ingested",
            target_type="task",
            target_id=task_id,
            detail={
                "summary": summary,
                "materials": materials,
                "warnings": warnings or [],
            },
        )

    def log_provider_call(
        self,
        *,
        provider: str,
        model: str,
        task: str,
        latency_ms: int,
        prompt_tokens: int,
        completion_tokens: int,
        degraded: bool,
        ok: bool,
        error: str = "",
    ) -> int:
        return self.log(
            "llm.call",
            target_type="provider",
            target_id=provider,
            level="warning" if (degraded or not ok) else "info",
            detail={
                "model": model,
                "task": task,
                "latency_ms": latency_ms,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "degraded": degraded,
                "ok": ok,
                "error": error,
            },
        )

    def log_conclusion(
        self,
        conclusion_id: str,
        task_id: str,
        *,
        overall_level: str,
        finding_count: int,
        provider: str,
        model: str,
        duration_ms: int,
        degraded_stages: list[str] | None = None,
    ) -> int:
        return self.log(
            "review.completed",
            target_type="conclusion",
            target_id=conclusion_id,
            detail={
                "task_id": task_id,
                "overall_level": overall_level,
                "finding_count": finding_count,
                "provider": provider,
                "model": model,
                "duration_ms": duration_ms,
                "degraded_stages": degraded_stages or [],
            },
        )

    def log_verdict(
        self,
        finding_id: str,
        verdict: str,
        *,
        note: str = "",
        reviewer: str = "",
    ) -> int:
        """记录人工复核结论——模型输出与人工判断的差异是评测的核心素材。"""
        return self.log(
            "finding.verdict",
            target_type="finding",
            target_id=finding_id,
            actor=reviewer or self.actor,
            detail={"verdict": verdict, "note": note},
        )

    def log_disposition(
        self,
        finding_id: str,
        disposition_type: str,
        *,
        note: str = "",
        actor: str = "",
        is_exception: bool = False,
        exception_reason: str = "",
    ) -> int:
        return self.log(
            "finding.disposition",
            target_type="finding",
            target_id=finding_id,
            actor=actor or self.actor,
            level="warning" if is_exception else "info",
            detail={
                "type": disposition_type,
                "note": note,
                "is_exception": is_exception,
                "exception_reason": exception_reason,
            },
        )

    def log_config_change(self, *, changes: dict[str, Any], actor: str = "") -> int:
        return self.log(
            "config.changed",
            target_type="config",
            actor=actor or self.actor,
            level="warning",
            detail=changes,
        )

    def log_export(self, path: str, *, fmt: str, task_id: str = "") -> int:
        return self.log(
            "conclusion.exported",
            target_type="task",
            target_id=task_id,
            detail={"path": path, "format": fmt},
        )

    def log_error(self, where: str, error: str, detail: dict[str, Any] | None = None) -> int:
        return self.log(
            "error",
            target_type="module",
            target_id=where,
            level="error",
            detail={"error": error, **(detail or {})},
        )

    # ---------------------------------------------------------- 读取与校验

    def recent(self, limit: int = 200, *, level: str = "", action: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit_log"
        conditions: list[str] = []
        params: list[Any] = []
        if level:
            conditions.append("level = ?")
            params.append(level)
        if action:
            conditions.append("action LIKE ?")
            params.append(f"{action}%")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.db.query(sql, tuple(params))
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "level": r["level"],
                "action": r["action"],
                "actor": r["actor"],
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "detail": json.loads(r["detail_json"] or "{}"),
                "entry_hash": r["entry_hash"][:16],
            }
            for r in rows
        ]

    def verify_chain(self, limit: int = 100_000) -> tuple[bool, str]:
        """校验哈希链完整性。返回 ``(是否完整, 说明)``。"""
        rows = self.db.query(
            "SELECT id, ts, level, action, actor, target_type, target_id, detail_json, "
            "prev_hash, entry_hash FROM audit_log ORDER BY id ASC LIMIT ?",
            (limit,),
        )
        prev = ""
        for row in rows:
            expected = self._compute_hash(
                prev,
                row["ts"],
                row["level"],
                row["action"],
                row["actor"],
                row["target_type"],
                row["target_id"],
                row["detail_json"],
            )
            if row["prev_hash"] != prev:
                return False, f"第 {row['id']} 条记录的前序哈希不匹配，日志链在此断开"
            if row["entry_hash"] != expected:
                return False, f"第 {row['id']} 条记录内容与哈希不符，可能已被修改"
            prev = row["entry_hash"]
        return True, f"审计日志链完整（共 {len(rows)} 条记录）"


__all__ = ["AuditLogger", "mask_secret"]
