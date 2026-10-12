"""六维评测模块：口径、阈值与结果结构。"""

from __future__ import annotations

from fund_risk_sentinel.core.enums import (
    HumanVerdict,
    MetricDimension,
    RiskCategory,
    RiskLevel,
)
from fund_risk_sentinel.core.models import (
    Evidence,
    MetricResult,
    ReviewConclusion,
    RiskFinding,
)
from fund_risk_sentinel.evaluate.metrics import (
    DEFAULT_THRESHOLDS,
    Evaluator,
    _percentile,
    format_report,
)


class TestThresholds:
    def test_all_six_dimensions_have_thresholds(self) -> None:
        for dim in MetricDimension:
            assert dim in DEFAULT_THRESHOLDS

    def test_explainability_threshold_is_strict(self) -> None:
        """可解释性门槛高，用于抑制"堆线索"的做法。"""
        assert DEFAULT_THRESHOLDS[MetricDimension.EXPLAINABILITY] >= 0.9


class TestPercentile:
    def test_ratio_is_a_fraction(self) -> None:
        # ratio 取值 0~1；最近秩法：index = round(ratio * (n-1))
        assert _percentile([1, 2, 3, 4], 0.5) == 3

    def test_p1_is_max(self) -> None:
        assert _percentile([5, 1, 9], 1.0) == 9

    def test_p0_is_min(self) -> None:
        assert _percentile([5, 1, 9], 0.0) == 1

    def test_empty_returns_zero(self) -> None:
        assert _percentile([], 0.95) == 0.0


def _finding(
    title: str,
    level: RiskLevel = RiskLevel.MEDIUM,
    verdict: HumanVerdict = HumanVerdict.PENDING,
    with_evidence: bool = True,
) -> RiskFinding:
    return RiskFinding(
        title=title,
        category=RiskCategory.MISSING,
        level=level,
        rationale="理由",
        confidence=0.7,
        evidences=[Evidence(material_name="a.txt", locator="p.1")] if with_evidence else [],
        human_verdict=verdict,
    )


class TestEvaluator:
    def test_evaluate_returns_six_metrics(self, storage) -> None:
        repository, _audit = storage
        evaluator = Evaluator(repository=repository)
        report = evaluator.evaluate()
        assert len(report.metrics) == 6
        assert {m.dimension for m in report.metrics} == set(MetricDimension)

    def test_report_has_notes(self, storage) -> None:
        repository, _audit = storage
        report = Evaluator(repository=repository).evaluate()
        assert report.notes

    def test_empty_sample_reports_insufficient(self, storage) -> None:
        repository, _audit = storage
        report = Evaluator(repository=repository).evaluate()
        # 无样本时应如实说明，而不是给个好看的分数
        assert any("线索" in note or "样本" in note for note in report.notes)

    def test_format_report_renders_table(self, storage) -> None:
        repository, _audit = storage
        report = Evaluator(repository=repository).evaluate()
        text = format_report(report)
        assert "准确性" in text or "accuracy" in text


class TestAccuracyFormula:
    def test_accuracy_counts_partial_as_half(self) -> None:
        """口径：准确性 =（采纳 + 0.5 × 部分采纳）/ 已复核。"""
        findings = [
            _finding("a", verdict=HumanVerdict.ACCEPTED),
            _finding("b", verdict=HumanVerdict.PARTIAL),
            _finding("c", verdict=HumanVerdict.REJECTED),
            _finding("d", verdict=HumanVerdict.ACCEPTED),
        ]
        evaluator = Evaluator(repository=None)
        metric = evaluator._accuracy(findings, [])  # noqa: SLF001
        assert isinstance(metric, MetricResult)
        # (2 + 0.5*1) / 4 = 0.625
        assert metric.value == 0.625

    def test_explainability_penalizes_missing_evidence(self) -> None:
        findings = [_finding(f"f{i}", with_evidence=(i % 2 == 0)) for i in range(4)]
        evaluator = Evaluator(repository=None)
        metric = evaluator._explainability(findings, [])  # noqa: SLF001
        # 只有一半带证据，得分应明显低于 1
        assert metric.value < 0.9
