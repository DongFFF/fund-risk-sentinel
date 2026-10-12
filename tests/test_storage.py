"""存储层：SQLite 读写、审计哈希链、人工复核回写。"""

from __future__ import annotations

from pathlib import Path

from fund_risk_sentinel.core.enums import (
    EventType,
    HumanVerdict,
    ReviewStatus,
    RiskCategory,
    RiskLevel,
)
from fund_risk_sentinel.core.models import (
    Evidence,
    ExtractedFields,
    Material,
    ReviewConclusion,
    ReviewTask,
    RiskFinding,
)
from fund_risk_sentinel.storage.audit import AuditLogger, mask_secret


class TestDatabase:
    def test_tables_created(self, storage) -> None:
        repository, _audit = storage
        counts = repository.db.table_counts()
        for table in ("tasks", "materials", "conclusions", "findings", "audit_log"):
            assert table in counts

    def test_wal_mode_enabled(self, storage) -> None:
        repository, _audit = storage
        row = repository.db.query_one("PRAGMA journal_mode")
        assert str(row["journal_mode"]).lower() == "wal"


class TestRepository:
    def _task(self) -> ReviewTask:
        material = Material(filename="报销单.txt", text="金额：9560", role="application")
        return ReviewTask(title="测试任务", event_type=EventType.REIMBURSEMENT, materials=[material])

    def test_save_and_get_task(self, storage) -> None:
        repository, _audit = storage
        task = self._task()
        repository.save_task(task)
        loaded = repository.get_task(task.task_id)
        assert loaded is not None
        assert loaded.title == "测试任务"
        # 材料单独存表，需显式落库
        repository.save_materials(task.task_id, task.materials)
        assert len(repository.get_materials(task.task_id)) == 1

    def test_list_tasks_returns_saved(self, storage) -> None:
        repository, _audit = storage
        task = self._task()
        repository.save_task(task)
        tasks = repository.list_tasks()
        assert any(t["task_id"] == task.task_id for t in tasks)

    def test_find_material_by_hash(self, storage) -> None:
        repository, _audit = storage
        material = Material(filename="a.txt", text="内容", sha256="deadbeef")
        task = ReviewTask(title="t", materials=[material])
        repository.save_task(task)
        repository.save_materials(task.task_id, task.materials)
        found = repository.find_material_by_hash("deadbeef")
        assert found is not None
        assert found["filename"] == "a.txt"

    def test_save_conclusion_splits_findings(self, storage) -> None:
        repository, _audit = storage
        task = self._task()
        repository.save_task(task)

        conclusion = ReviewConclusion(
            task_id=task.task_id,
            event_type=EventType.REIMBURSEMENT,
            extracted=ExtractedFields(),
            status=ReviewStatus.NEEDS_HUMAN,
        )
        conclusion.add_finding(
            RiskFinding(
                title="支出超出预算科目余额",
                category=RiskCategory.DEVIATION,
                level=RiskLevel.HIGH,
                rationale="报销金额高于科目余额",
                evidences=[Evidence(material_name="报销单.txt", locator="p.1")],
            )
        )
        conclusion.recount()
        repository.save_conclusion(conclusion)

        loaded = repository.latest_conclusion(task.task_id)
        assert loaded is not None
        assert len(loaded.findings) == 1
        assert loaded.findings[0].title == "支出超出预算科目余额"

        findings = repository.list_findings(task_id=task.task_id)
        assert len(findings) == 1

    def test_update_finding_verdict(self, storage) -> None:
        repository, _audit = storage
        task = self._task()
        repository.save_task(task)
        conclusion = ReviewConclusion(task_id=task.task_id)
        finding = RiskFinding(
            title="线索", category=RiskCategory.MISSING, level=RiskLevel.MEDIUM, rationale="r"
        )
        conclusion.add_finding(finding)
        conclusion.recount()
        repository.save_conclusion(conclusion)

        repository.update_finding_verdict(finding.finding_id, HumanVerdict.ACCEPTED, note="确认")
        reloaded = repository.get_finding(finding.finding_id)
        assert reloaded is not None
        assert reloaded.human_verdict is HumanVerdict.ACCEPTED

    def test_dashboard_stats_shape(self, storage) -> None:
        repository, _audit = storage
        stats = repository.dashboard_stats()
        assert "counts" in stats
        assert "db_size_bytes" in stats


class TestAuditLogger:
    def test_chain_valid_after_appends(self, storage) -> None:
        _repository, audit = storage
        audit.log_task_created("task-1", "测试", 0)
        audit.log_ingest("task-1", summary="3 份材料", materials=[], warnings=[])
        ok, message = audit.verify_chain()
        assert ok, message

    def test_chain_detects_tampering(self, storage) -> None:
        _repository, audit = storage
        audit.log_task_created("task-1", "测试", 0)
        audit.log_task_created("task-2", "测试2", 0)

        # 直接改库里的明细，模拟篡改
        with audit.db.transaction() as conn:
            conn.execute("UPDATE audit_log SET detail_json = ? WHERE id = 1", ('{"tampered": 1}',))

        ok, message = audit.verify_chain()
        assert ok is False
        assert "不符" in message or "修改" in message or "篡改" in message

    def test_recent_returns_newest_first(self, storage) -> None:
        _repository, audit = storage
        audit.log_task_created("task-1", "A", 0)
        audit.log_task_created("task-2", "B", 0)
        records = audit.recent(limit=10)
        assert records
        assert records[0]["action"].startswith("task")

    def test_secrets_are_sanitized(self, storage) -> None:
        _repository, audit = storage
        audit.log_config_change(changes={"api_key": "sk-abcdef1234567890", "model": "x"})
        records = audit.recent(limit=1)
        detail = str(records[0].get("detail_json", ""))
        assert "sk-abcdef1234567890" not in detail

    def test_mask_secret_keeps_edges(self) -> None:
        masked = mask_secret("sk-1234567890abcdef")
        assert masked.startswith("sk-1")
        assert masked.endswith("cdef")
        assert "234567890ab" not in masked

    def test_text_log_written(self, storage, tmp_config) -> None:
        _repository, audit = storage
        audit.log_task_created("task-1", "测试", 0)
        log_dir = Path(tmp_config.settings.resolved_log_dir())
        files = list(log_dir.glob("audit-*.log"))
        assert files, "审计应同时写文本日志，便于人工翻阅"


class TestOpenStorage:
    def test_returns_repository_and_audit(self, storage) -> None:
        repository, audit = storage
        assert repository is not None
        assert isinstance(audit, AuditLogger)
