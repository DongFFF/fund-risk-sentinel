"""端到端：离线跑通完整审查流程，验证治理边界与降级行为。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fund_risk_sentinel.core.enums import EventType, ReviewStatus, StageStatus
from fund_risk_sentinel.explain import render_markdown, render_text
from fund_risk_sentinel.review.pipeline import PipelineOptions, ReviewPipeline

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture()
def conclusion(pipeline: ReviewPipeline, sample_paths: list[Path], default_options: PipelineOptions):
    return pipeline.run_paths(
        [str(p) for p in sample_paths], title="端到端测试", options=default_options
    )


class TestPipelineRun:
    def test_produces_conclusion(self, conclusion) -> None:
        assert conclusion is not None
        assert conclusion.task_id

    def test_records_all_six_stages(self, conclusion) -> None:
        stages = [record.stage for record in conclusion.pipeline_trace]
        for expected in ("assemble", "extract", "rule_check", "retrieve", "reason", "explain"):
            assert expected in stages, f"缺少阶段 {expected}"

    def test_finds_risks_on_sample(self, conclusion) -> None:
        assert conclusion.findings, "样例材料含明显疑点，应至少产生一条线索"

    def test_every_finding_needs_human_review(self, conclusion) -> None:
        """治理边界：不得替人下结论。"""
        assert all(f.needs_human_review for f in conclusion.findings)

    def test_every_finding_has_evidence(self, conclusion) -> None:
        """可解释性：每条线索都要能定位到材料。"""
        missing = [f.title for f in conclusion.findings if not f.evidences]
        assert not missing, f"以下线索缺少证据定位：{missing}"

    def test_findings_have_rationale(self, conclusion) -> None:
        assert all(f.rationale.strip() for f in conclusion.findings)

    def test_status_needs_human_when_findings(self, conclusion) -> None:
        if conclusion.findings:
            assert conclusion.status is ReviewStatus.NEEDS_HUMAN

    def test_statistics_match_findings(self, conclusion) -> None:
        assert conclusion.statistics["high"] == sum(
            1 for f in conclusion.findings if f.level.value == "high"
        )

    def test_summary_mentions_correct_count(self, conclusion) -> None:
        """回归：摘要条数不得与实际线索数矛盾。"""
        if conclusion.findings:
            assert str(len(conclusion.findings)) in conclusion.summary

    def test_disclaimer_present(self, conclusion) -> None:
        assert "不构成" in conclusion.disclaimer or "线索" in conclusion.disclaimer

    def test_usage_recorded(self, conclusion) -> None:
        assert conclusion.provider

    def test_duration_positive(self, conclusion) -> None:
        assert conclusion.duration_ms >= 0


class TestDegradation:
    def test_rule_only_mode_still_works(
        self, pipeline: ReviewPipeline, sample_paths: list[Path]
    ) -> None:
        """关掉大模型，规则路径必须独立可用。"""
        options = PipelineOptions(
            event_type=EventType.REIMBURSEMENT, enable_llm=False, provider="mock"
        )
        conclusion = pipeline.run_paths([str(p) for p in sample_paths], options=options)
        assert conclusion.findings
        assert all(f.needs_human_review for f in conclusion.findings)

    def test_extract_stage_marked_skipped_without_llm(
        self, pipeline: ReviewPipeline, sample_paths: list[Path]
    ) -> None:
        options = PipelineOptions(
            event_type=EventType.REIMBURSEMENT, enable_llm=False, provider="mock"
        )
        conclusion = pipeline.run_paths([str(p) for p in sample_paths], options=options)
        record = next(r for r in conclusion.pipeline_trace if r.stage == "extract")
        assert record.status in (StageStatus.OK, StageStatus.SKIPPED, StageStatus.DEGRADED)


class TestRunMaterials:
    def test_run_materials_accepts_pipeline_output(
        self, pipeline: ReviewPipeline, sample_paths: list[Path], default_options: PipelineOptions
    ) -> None:
        result = pipeline.ingest.load_paths([str(p) for p in sample_paths])
        conclusion = pipeline.run_materials(
            result.materials, title="从材料直接审查", options=default_options
        )
        assert conclusion.findings


class TestRender:
    def test_markdown_has_all_sections(self, conclusion) -> None:
        markdown = render_markdown(conclusion)
        for section in ("结论摘要", "风险线索明细", "处置建议", "结构化抽取结果", "声明"):
            assert section in markdown, f"Markdown 底稿缺少「{section}」小节"

    def test_markdown_lists_evidence_and_basis(self, conclusion) -> None:
        markdown = render_markdown(conclusion)
        assert "关联材料" in markdown
        assert "依据" in markdown

    def test_markdown_starts_with_heading(self, conclusion) -> None:
        assert render_markdown(conclusion).startswith("#")

    def test_text_rendering_non_empty(self, conclusion) -> None:
        assert render_text(conclusion).strip()

    def test_markdown_contains_disclaimer(self, conclusion) -> None:
        assert conclusion.disclaimer[:8] in render_markdown(conclusion)
