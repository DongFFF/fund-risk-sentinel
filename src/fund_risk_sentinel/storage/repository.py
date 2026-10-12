"""数据访问层：领域对象 ↔ SQLite 的映射。

上层（界面 / 命令行）只与 ``Repository`` 打交道，不直接写 SQL。这样后续要换
PostgreSQL 或加同步机制，只改这一层。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from ..core.enums import (
    DispositionType,
    DocumentFormat,
    DocumentRole,
    EventType,
    HumanVerdict,
    ReviewStatus,
    RiskCategory,
    RiskLevel,
    StageStatus,
)
from ..core.exceptions import StorageError
from ..core.models import (
    ClauseRef,
    Disposition,
    EvaluationReport,
    Evidence,
    ExtractedField,
    ExtractedFields,
    Material,
    MetricResult,
    PipelineStageRecord,
    ReviewConclusion,
    ReviewTask,
    RiskFinding,
    TokenUsage,
    utc_now,
)
from .db import Database

logger = logging.getLogger(__name__)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


class Repository:
    """审查任务、材料、结论、线索、处置与评测的持久化。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ============================================================ 任务

    def save_task(self, task: ReviewTask) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO tasks (task_id, title, event_type, status, created_at, started_at,
                                   finished_at, reviewer, project_name, project_code, notes,
                                   material_count, finding_count, high_count, overall_level)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET
                    title=excluded.title, event_type=excluded.event_type, status=excluded.status,
                    started_at=excluded.started_at, finished_at=excluded.finished_at,
                    reviewer=excluded.reviewer, project_name=excluded.project_name,
                    project_code=excluded.project_code, notes=excluded.notes,
                    material_count=excluded.material_count, finding_count=excluded.finding_count,
                    high_count=excluded.high_count, overall_level=excluded.overall_level
                """,
                (
                    task.task_id,
                    task.title,
                    task.event_type.value,
                    task.status.value,
                    task.created_at.isoformat(),
                    task.started_at.isoformat() if task.started_at else None,
                    task.finished_at.isoformat() if task.finished_at else None,
                    task.reviewer,
                    task.project_name,
                    task.project_code,
                    task.notes,
                    task.material_count,
                    len(task.conclusion.findings) if task.conclusion else 0,
                    task.conclusion.high_risk_count if task.conclusion else 0,
                    task.conclusion.overall_level.value if task.conclusion else RiskLevel.UNKNOWN.value,
                ),
            )

    def get_task(self, task_id: str) -> ReviewTask | None:
        row = self.db.query_one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        if row is None:
            return None
        task = ReviewTask(
            task_id=row["task_id"],
            title=row["title"],
            event_type=EventType.from_value(row["event_type"], EventType.OTHER),
            status=ReviewStatus.from_value(row["status"], ReviewStatus.PENDING),
            created_at=_parse_dt(row["created_at"]) or utc_now(),
            started_at=_parse_dt(row["started_at"]),
            finished_at=_parse_dt(row["finished_at"]),
            reviewer=row["reviewer"],
            project_name=row["project_name"],
            project_code=row["project_code"],
            notes=row["notes"],
        )
        task.materials = self.get_materials(task_id)
        task.conclusion = self.latest_conclusion(task_id)
        return task

    def list_tasks(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
        keyword: str = "",
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tasks"
        params: list[Any] = []
        conditions: list[str] = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if keyword:
            conditions.append("(title LIKE ? OR project_name LIKE ? OR reviewer LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like])
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self.db.query(sql, tuple(params))
        return [
            {
                "task_id": r["task_id"],
                "title": r["title"],
                "event_type": r["event_type"],
                "status": r["status"],
                "created_at": r["created_at"],
                "finished_at": r["finished_at"],
                "reviewer": r["reviewer"],
                "project_name": r["project_name"],
                "material_count": r["material_count"],
                "finding_count": r["finding_count"],
                "high_count": r["high_count"],
                "overall_level": r["overall_level"],
            }
            for r in rows
        ]

    def delete_task(self, task_id: str) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
            return cursor.rowcount > 0

    # ============================================================ 材料

    def save_material(self, task_id: str, material: Material) -> None:
        self.save_materials(task_id, [material])

    def save_materials(self, task_id: str, materials: Iterable[Material]) -> None:
        payloads = [
            (
                m.material_id,
                task_id,
                m.filename,
                m.path,
                m.fmt.value,
                m.role.value,
                m.size_bytes,
                m.sha256,
                m.page_count,
                m.char_count,
                m.text,
                _dumps(m.tables),
                _dumps(m.metadata),
                1 if m.redaction_applied else 0,
                _dumps(m.redaction_log),
                m.source,
                m.uploaded_at.isoformat(),
                m.retention_until,
            )
            for m in materials
        ]
        if not payloads:
            return
        with self.db.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO materials (material_id, task_id, filename, path, fmt, role,
                    size_bytes, sha256, page_count, char_count, text, tables_json,
                    metadata_json, redaction_applied, redaction_log_json, source,
                    uploaded_at, retention_until)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(material_id) DO UPDATE SET
                    role=excluded.role, text=excluded.text, tables_json=excluded.tables_json,
                    metadata_json=excluded.metadata_json,
                    redaction_applied=excluded.redaction_applied,
                    redaction_log_json=excluded.redaction_log_json
                """,
                payloads,
            )

    def get_materials(self, task_id: str) -> list[Material]:
        rows = self.db.query(
            "SELECT * FROM materials WHERE task_id = ? ORDER BY rowid", (task_id,)
        )
        materials: list[Material] = []
        for row in rows:
            try:
                materials.append(
                    Material(
                        material_id=row["material_id"],
                        filename=row["filename"],
                        path=row["path"],
                        fmt=DocumentFormat.from_value(row["fmt"], DocumentFormat.UNKNOWN),
                        role=DocumentRole.from_value(row["role"], DocumentRole.OTHER),
                        size_bytes=row["size_bytes"],
                        sha256=row["sha256"],
                        page_count=row["page_count"],
                        text=row["text"],
                        tables=_loads(row["tables_json"], []),
                        metadata=_loads(row["metadata_json"], {}),
                        redaction_applied=bool(row["redaction_applied"]),
                        redaction_log=_loads(row["redaction_log_json"], []),
                        source=row["source"],
                        uploaded_at=_parse_dt(row["uploaded_at"]) or utc_now(),
                        retention_until=row["retention_until"],
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 单条损坏不应导致整任务不可读
                logger.warning("材料 %s 反序列化失败：%s", row["material_id"], exc)
        return materials

    def find_material_by_hash(self, sha256: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT material_id, task_id, filename FROM materials WHERE sha256 = ? LIMIT 1",
            (sha256,),
        )
        return dict(row) if row else None

    # ============================================================ 结论

    def save_conclusion(self, conclusion: ReviewConclusion) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO conclusions (conclusion_id, task_id, status, event_type, summary,
                    overall_level, statistics_json, extracted_json, trace_json, usage_json,
                    generated_at, duration_ms, provider, model, adaptation_paths_json,
                    degraded_stages_json, disclaimer, payload_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(conclusion_id) DO UPDATE SET
                    status=excluded.status, summary=excluded.summary,
                    overall_level=excluded.overall_level,
                    statistics_json=excluded.statistics_json,
                    extracted_json=excluded.extracted_json,
                    trace_json=excluded.trace_json, usage_json=excluded.usage_json,
                    duration_ms=excluded.duration_ms, provider=excluded.provider,
                    model=excluded.model,
                    adaptation_paths_json=excluded.adaptation_paths_json,
                    degraded_stages_json=excluded.degraded_stages_json,
                    disclaimer=excluded.disclaimer, payload_json=excluded.payload_json
                """,
                (
                    conclusion.conclusion_id,
                    conclusion.task_id,
                    conclusion.status.value,
                    conclusion.event_type.value,
                    conclusion.summary,
                    conclusion.overall_level.value,
                    _dumps(conclusion.statistics),
                    _dumps(conclusion.extracted.model_dump(mode="json")) if conclusion.extracted else None,
                    _dumps([s.model_dump(mode="json") for s in conclusion.pipeline_trace]),
                    _dumps(conclusion.usage.model_dump(mode="json")),
                    conclusion.generated_at.isoformat(),
                    conclusion.duration_ms,
                    conclusion.provider,
                    conclusion.model,
                    _dumps([p.value for p in conclusion.adaptation_paths]),
                    _dumps(conclusion.degraded_stages),
                    conclusion.disclaimer,
                    _dumps(conclusion.model_dump(mode="json")),
                ),
            )
            # 线索随结论一起落库：先清旧的，避免重复审查后残留
            conn.execute("DELETE FROM findings WHERE conclusion_id = ?", (conclusion.conclusion_id,))
            for finding in conclusion.findings:
                conn.execute(_FINDING_INSERT, _finding_row(finding, conclusion))

            if conclusion.task_id:
                conn.execute(
                    """
                    UPDATE tasks SET finding_count = ?, high_count = ?, overall_level = ?,
                                     status = ?, finished_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        len(conclusion.findings),
                        conclusion.high_risk_count,
                        conclusion.overall_level.value,
                        ReviewStatus.NEEDS_HUMAN.value
                        if conclusion.findings
                        else ReviewStatus.COMPLETED.value,
                        conclusion.generated_at.isoformat(),
                        conclusion.task_id,
                    ),
                )

    def get_conclusion(self, conclusion_id: str) -> ReviewConclusion | None:
        row = self.db.query_one(
            "SELECT * FROM conclusions WHERE conclusion_id = ?", (conclusion_id,)
        )
        if row is None:
            return None
        payload = _loads(row["payload_json"], None)
        if payload:
            try:
                conclusion = ReviewConclusion.model_validate(payload)
                conclusion.findings = self.list_findings(conclusion_id=conclusion_id)
                return conclusion
            except Exception as exc:  # noqa: BLE001 - 结构升级后旧数据可能不兼容
                logger.warning("结论 %s 反序列化失败，改用分列字段重建：%s", conclusion_id, exc)
        return self._rebuild_conclusion(row)

    def latest_conclusion(self, task_id: str) -> ReviewConclusion | None:
        row = self.db.query_one(
            "SELECT conclusion_id FROM conclusions WHERE task_id = ? ORDER BY generated_at DESC LIMIT 1",
            (task_id,),
        )
        return self.get_conclusion(row["conclusion_id"]) if row else None

    def _rebuild_conclusion(self, row: Any) -> ReviewConclusion:
        extracted_payload = _loads(row["extracted_json"], None)
        conclusion = ReviewConclusion(
            conclusion_id=row["conclusion_id"],
            task_id=row["task_id"],
            status=ReviewStatus.from_value(row["status"], ReviewStatus.PENDING),
            event_type=EventType.from_value(row["event_type"], EventType.OTHER),
            summary=row["summary"],
            overall_level=RiskLevel.from_value(row["overall_level"], RiskLevel.UNKNOWN),
            statistics=_loads(row["statistics_json"], {}),
            pipeline_trace=[
                PipelineStageRecord.model_validate(item)
                for item in _loads(row["trace_json"], [])
            ],
            usage=TokenUsage.model_validate(_loads(row["usage_json"], {})),
            generated_at=_parse_dt(row["generated_at"]) or utc_now(),
            duration_ms=row["duration_ms"],
            provider=row["provider"],
            model=row["model"],
            degraded_stages=_loads(row["degraded_stages_json"], []),
            disclaimer=row["disclaimer"],
        )
        if extracted_payload:
            conclusion.extracted = ExtractedFields.model_validate(extracted_payload)
        conclusion.findings = self.list_findings(conclusion_id=row["conclusion_id"])
        return conclusion

    # ============================================================ 线索

    def list_findings(
        self,
        *,
        conclusion_id: str = "",
        task_id: str = "",
        level: str = "",
        verdict: str = "",
        limit: int = 500,
    ) -> list[RiskFinding]:
        sql = "SELECT * FROM findings"
        conditions: list[str] = []
        params: list[Any] = []
        if conclusion_id:
            conditions.append("conclusion_id = ?")
            params.append(conclusion_id)
        if task_id:
            conditions.append("task_id = ?")
            params.append(task_id)
        if level:
            conditions.append("level = ?")
            params.append(level)
        if verdict:
            conditions.append("human_verdict = ?")
            params.append(verdict)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY CASE level WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END DESC, confidence DESC LIMIT ?"
        params.append(limit)
        return [_row_to_finding(r) for r in self.db.query(sql, tuple(params))]

    def get_finding(self, finding_id: str) -> RiskFinding | None:
        row = self.db.query_one("SELECT * FROM findings WHERE finding_id = ?", (finding_id,))
        return _row_to_finding(row) if row else None

    def update_finding_verdict(
        self,
        finding_id: str,
        verdict: HumanVerdict | str,
        *,
        note: str = "",
        reviewed_by: str = "",
    ) -> bool:
        """记录人工复核结论——这是六维评测的基准标签来源。"""
        value = verdict.value if isinstance(verdict, HumanVerdict) else HumanVerdict.from_value(
            str(verdict), HumanVerdict.PENDING
        ).value
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE findings SET human_verdict = ?, human_note = ?, reviewed_by = ?,
                                    reviewed_at = ?
                WHERE finding_id = ?
                """,
                (value, note, reviewed_by, utc_now().isoformat(), finding_id),
            )
            return cursor.rowcount > 0

    # ============================================================ 处置

    def save_disposition(self, disposition: Disposition) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO dispositions (disposition_id, finding_id, type, note, actor, role,
                    decided_at, is_exception, exception_reason)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(disposition_id) DO UPDATE SET
                    type=excluded.type, note=excluded.note, actor=excluded.actor,
                    role=excluded.role, decided_at=excluded.decided_at,
                    is_exception=excluded.is_exception, exception_reason=excluded.exception_reason
                """,
                (
                    disposition.disposition_id,
                    disposition.finding_id,
                    disposition.type.value,
                    disposition.note,
                    disposition.actor,
                    disposition.role,
                    disposition.decided_at.isoformat() if disposition.decided_at else None,
                    1 if disposition.is_exception else 0,
                    disposition.exception_reason,
                ),
            )

    def list_dispositions(self, finding_id: str) -> list[Disposition]:
        rows = self.db.query(
            "SELECT * FROM dispositions WHERE finding_id = ? ORDER BY rowid", (finding_id,)
        )
        return [
            Disposition(
                disposition_id=r["disposition_id"],
                finding_id=r["finding_id"],
                type=DispositionType.from_value(r["type"], DispositionType.PENDING_REVIEW),
                note=r["note"],
                actor=r["actor"],
                role=r["role"],
                decided_at=_parse_dt(r["decided_at"]),
                is_exception=bool(r["is_exception"]),
                exception_reason=r["exception_reason"],
            )
            for r in rows
        ]

    # ============================================================ 评测

    def save_evaluation(self, report: EvaluationReport) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO evaluations (report_id, generated_at, task_ids_json, metrics_json, notes_json)
                VALUES (?,?,?,?,?)
                ON CONFLICT(report_id) DO UPDATE SET
                    metrics_json=excluded.metrics_json, notes_json=excluded.notes_json
                """,
                (
                    report.report_id,
                    report.generated_at.isoformat(),
                    _dumps(report.task_ids),
                    _dumps([m.model_dump(mode="json") for m in report.metrics]),
                    _dumps(report.notes),
                ),
            )

    def latest_evaluation(self) -> EvaluationReport | None:
        row = self.db.query_one("SELECT * FROM evaluations ORDER BY generated_at DESC LIMIT 1")
        if row is None:
            return None
        return EvaluationReport(
            report_id=row["report_id"],
            generated_at=_parse_dt(row["generated_at"]) or utc_now(),
            task_ids=_loads(row["task_ids_json"], []),
            metrics=[MetricResult.model_validate(m) for m in _loads(row["metrics_json"], [])],
            notes=_loads(row["notes_json"], []),
        )

    # ============================================================ 概览

    def dashboard_stats(self) -> dict[str, Any]:
        """首页概览数字。"""
        counts = self.db.table_counts()
        level_rows = self.db.query(
            "SELECT level, COUNT(*) AS n FROM findings GROUP BY level"
        )
        verdict_rows = self.db.query(
            "SELECT human_verdict, COUNT(*) AS n FROM findings GROUP BY human_verdict"
        )
        recent = self.list_tasks(limit=5)
        return {
            "counts": counts,
            "findings_by_level": {r["level"]: r["n"] for r in level_rows},
            "findings_by_verdict": {r["human_verdict"]: r["n"] for r in verdict_rows},
            "recent_tasks": recent,
            "db_size_bytes": self.db.size_bytes(),
        }


# ================================================================ 行映射

_FINDING_INSERT = """
INSERT INTO findings (finding_id, conclusion_id, task_id, title, category, level, event_type,
    description, rationale, matched_clauses_json, evidences_json, counter_evidence_json,
    dispositions_json, suggestion, confidence, produced_by, provider, model,
    needs_human_review, human_verdict, human_note, reviewed_by, reviewed_at, created_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def _finding_row(finding: RiskFinding, conclusion: ReviewConclusion) -> tuple[Any, ...]:
    return (
        finding.finding_id,
        conclusion.conclusion_id,
        conclusion.task_id,
        finding.title,
        finding.category.value,
        finding.level.value,
        finding.event_type.value,
        finding.description,
        finding.rationale,
        _dumps([c.model_dump(mode="json") for c in finding.matched_clauses]),
        _dumps([e.model_dump(mode="json") for e in finding.evidences]),
        _dumps(finding.counter_evidence),
        _dumps([d.value for d in finding.suggested_dispositions]),
        finding.suggestion,
        finding.confidence,
        finding.produced_by,
        finding.provider,
        finding.model,
        1 if finding.needs_human_review else 0,
        finding.human_verdict.value,
        finding.human_note,
        finding.reviewed_by,
        finding.reviewed_at.isoformat() if finding.reviewed_at else None,
        finding.created_at.isoformat(),
    )


def _row_to_finding(row: Any) -> RiskFinding:
    return RiskFinding(
        finding_id=row["finding_id"],
        title=row["title"],
        category=RiskCategory.from_value(row["category"], RiskCategory.DEVIATION),
        level=RiskLevel.from_value(row["level"], RiskLevel.UNKNOWN),
        event_type=EventType.from_value(row["event_type"], EventType.OTHER),
        description=row["description"],
        rationale=row["rationale"],
        matched_clauses=[ClauseRef.model_validate(c) for c in _loads(row["matched_clauses_json"], [])],
        evidences=[Evidence.model_validate(e) for e in _loads(row["evidences_json"], [])],
        counter_evidence=_loads(row["counter_evidence_json"], []),
        suggested_dispositions=[
            d
            for d in (
                DispositionType.from_value(x)
                for x in _loads(row["dispositions_json"], [])
            )
            if d
        ],
        suggestion=row["suggestion"],
        confidence=row["confidence"],
        produced_by=row["produced_by"],
        provider=row["provider"],
        model=row["model"],
        needs_human_review=bool(row["needs_human_review"]),
        human_verdict=HumanVerdict.from_value(row["human_verdict"], HumanVerdict.PENDING),
        human_note=row["human_note"],
        reviewed_by=row["reviewed_by"],
        reviewed_at=_parse_dt(row["reviewed_at"]),
        created_at=_parse_dt(row["created_at"]) or utc_now(),
    )


__all__ = ["Repository"]
