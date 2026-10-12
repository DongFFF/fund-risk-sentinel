"""六维评测模块——课题表2「风险识别能力评测指标体系」的代码实现。

======================  ============================  ==============================
维度                    观测指标                      本实现取值方式
======================  ============================  ==============================
准确性                  风险提示与人工结论一致率       (采纳 + 0.5×部分采纳) / 已复核线索数
召回性                  真实风险线索覆盖率             采纳数 / (采纳数 + 人工补充的漏报数)
时延                    业务节点前提示生成时间         结论 duration_ms 的均值 / P95
一致性                  相似材料判别稳定率             重复回放的线索集合 Jaccard 相似度
可解释性                提示与依据、证据对应完整率     同时具备依据与证据定位的线索占比
审核采纳度              提示被采纳或参考比例           (采纳 + 部分采纳) / 全部线索数
======================  ============================  ==============================

**为什么必须多维度**：只看识别率会诱导"宁滥勿缺"——把线索堆到几十条，
识别率上去了，审核人被噪声淹没，实际不可用。加入可解释性与采纳度后，
"给得多但没人看"的方案会被自动惩罚。

**当前数据状态说明**：课题尚处早期，没有标准化标注数据，因此本模块的
"人工结论"来自审核人在界面上的逐条复核（``human_verdict``）。随着复核量积累，
评测结果会从"样本不足"逐步变为可用结论——这正是课题"以人工复核结论为基准，
持续校正风险标签、知识库和阈值"的落地方式。
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from ..core.enums import HumanVerdict, MetricDimension, RiskLevel
from ..core.models import (
    EvaluationReport,
    MetricResult,
    ReviewConclusion,
    RiskFinding,
    utc_now,
)

logger = logging.getLogger(__name__)

#: 各维度的目标阈值（"目标取向"的可判定化）
DEFAULT_THRESHOLDS: dict[MetricDimension, float] = {
    MetricDimension.ACCURACY: 0.80,
    MetricDimension.RECALL: 0.70,
    MetricDimension.LATENCY: 60_000.0,        # 单位 ms，均值上限 60 秒
    MetricDimension.CONSISTENCY: 0.85,
    MetricDimension.EXPLAINABILITY: 0.90,
    MetricDimension.ADOPTION: 0.60,
}

#: 各维度的目标取向描述（与课题表2一致）
TARGET_NOTES: dict[MetricDimension, str] = {
    MetricDimension.ACCURACY: "持续提升",
    MetricDimension.RECALL: "持续提升",
    MetricDimension.LATENCY: "满足业务时效",
    MetricDimension.CONSISTENCY: "保持稳定",
    MetricDimension.EXPLAINABILITY: "完整可追溯",
    MetricDimension.ADOPTION: "稳步提高",
}

#: 样本量下限：低于此值时指标仅供参考
_MIN_SAMPLE = 5


@dataclass
class EvaluationInput:
    """评测输入。"""

    findings: list[RiskFinding]
    conclusions: list[ReviewConclusion]
    task_ids: list[str]


class Evaluator:
    """六维评测器。"""

    def __init__(
        self,
        *,
        repository=None,
        thresholds: dict[MetricDimension, float] | None = None,
    ) -> None:
        self.repository = repository
        self.thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    # ============================================================ 取数

    def load_input(self, task_ids: Sequence[str] | None = None) -> EvaluationInput:
        """从存储层取评测素材。"""
        if self.repository is None:
            return EvaluationInput(findings=[], conclusions=[], task_ids=list(task_ids or []))

        ids = list(task_ids or [])
        if ids:
            findings: list[RiskFinding] = []
            conclusions: list[ReviewConclusion] = []
            for task_id in ids:
                findings.extend(self.repository.list_findings(task_id=task_id, limit=5000))
                conclusion = self.repository.latest_conclusion(task_id)
                if conclusion:
                    conclusions.append(conclusion)
            return EvaluationInput(findings=findings, conclusions=conclusions, task_ids=ids)

        findings = self.repository.list_findings(limit=100_000)
        tasks = self.repository.list_tasks(limit=100_000)
        conclusions = []
        for task in tasks:
            conclusion = self.repository.latest_conclusion(task["task_id"])
            if conclusion:
                conclusions.append(conclusion)
        return EvaluationInput(
            findings=findings,
            conclusions=conclusions,
            task_ids=[t["task_id"] for t in tasks],
        )

    # ============================================================ 主入口

    def evaluate(
        self,
        *,
        task_ids: Sequence[str] | None = None,
        findings: Iterable[RiskFinding] | None = None,
        conclusions: Iterable[ReviewConclusion] | None = None,
        replays: list[list[RiskFinding]] | None = None,
    ) -> EvaluationReport:
        """计算六维指标。``replays`` 提供重复回放结果时才能算出真实的一致性。"""
        if findings is None or conclusions is None:
            data = self.load_input(task_ids)
            findings = findings if findings is not None else data.findings
            conclusions = conclusions if conclusions is not None else data.conclusions
            resolved_task_ids = list(task_ids or data.task_ids)
        else:
            resolved_task_ids = list(task_ids or [])

        finding_list = list(findings)
        conclusion_list = list(conclusions)
        notes: list[str] = []

        metrics = [
            self._accuracy(finding_list, notes),
            self._recall(finding_list, notes),
            self._latency(conclusion_list, notes),
            self._consistency(replays, finding_list, notes),
            self._explainability(finding_list, notes),
            self._adoption(finding_list, notes),
        ]

        if not finding_list:
            notes.append(
                "当前没有任何风险线索记录。请先完成至少一次审查任务，再查看评测结果。"
            )
        elif len(finding_list) < _MIN_SAMPLE:
            notes.append(
                f"当前样本量偏小（{len(finding_list)} 条线索），指标仅供趋势参考，"
                f"建议复核样本达到 {_MIN_SAMPLE} 条以上后再作判断。"
            )
        unreviewed = sum(1 for f in finding_list if f.human_verdict == HumanVerdict.PENDING)
        if unreviewed:
            notes.append(
                f"有 {unreviewed} 条线索尚未人工复核。准确性、召回性与采纳度"
                "依赖人工复核结论作为基准，未复核部分不计入分母。"
            )

        return EvaluationReport(
            task_ids=resolved_task_ids,
            metrics=metrics,
            notes=notes,
        )

    # ============================================================ 各维度

    def _accuracy(self, findings: list[RiskFinding], notes: list[str]) -> MetricResult:
        reviewed = [f for f in findings if f.human_verdict != HumanVerdict.PENDING
                    and f.human_verdict != HumanVerdict.MISSED]
        if not reviewed:
            return MetricResult(
                dimension=MetricDimension.ACCURACY,
                value=0.0,
                unit="ratio",
                target=TARGET_NOTES[MetricDimension.ACCURACY],
                sample_size=0,
                passed=None,
                detail="尚无可用于比对的已复核线索",
            )
        accepted = sum(1 for f in reviewed if f.human_verdict == HumanVerdict.ACCEPTED)
        partial = sum(1 for f in reviewed if f.human_verdict == HumanVerdict.PARTIAL)
        rejected = sum(1 for f in reviewed if f.human_verdict == HumanVerdict.REJECTED)
        value = (accepted + 0.5 * partial) / len(reviewed)
        return MetricResult(
            dimension=MetricDimension.ACCURACY,
            value=round(value, 4),
            unit="ratio",
            target=TARGET_NOTES[MetricDimension.ACCURACY],
            sample_size=len(reviewed),
            passed=value >= self.thresholds[MetricDimension.ACCURACY],
            detail=(
                f"已复核 {len(reviewed)} 条：采纳 {accepted}、部分采纳 {partial}、否决 {rejected}"
                f"（部分采纳按 0.5 计权）"
            ),
        )

    def _recall(self, findings: list[RiskFinding], notes: list[str]) -> MetricResult:
        accepted = sum(1 for f in findings if f.human_verdict == HumanVerdict.ACCEPTED)
        missed = sum(1 for f in findings if f.human_verdict == HumanVerdict.MISSED)
        denominator = accepted + missed
        if denominator == 0:
            return MetricResult(
                dimension=MetricDimension.RECALL,
                value=0.0,
                unit="ratio",
                target=TARGET_NOTES[MetricDimension.RECALL],
                sample_size=0,
                passed=None,
                detail=(
                    "尚无'模型漏报'记录。请在人工复核时把系统未发现、但人工发现的线索"
                    "标记为「模型漏报」，召回性才可计算。"
                ),
            )
        value = accepted / denominator
        return MetricResult(
            dimension=MetricDimension.RECALL,
            value=round(value, 4),
            unit="ratio",
            target=TARGET_NOTES[MetricDimension.RECALL],
            sample_size=denominator,
            passed=value >= self.thresholds[MetricDimension.RECALL],
            detail=f"模型采纳 {accepted} 条，人工补充漏报 {missed} 条",
        )

    def _latency(self, conclusions: list[ReviewConclusion], notes: list[str]) -> MetricResult:
        durations = [c.duration_ms for c in conclusions if c.duration_ms > 0]
        if not durations:
            return MetricResult(
                dimension=MetricDimension.LATENCY,
                value=0.0,
                unit="ms",
                target=TARGET_NOTES[MetricDimension.LATENCY],
                sample_size=0,
                passed=None,
                detail="暂无审查耗时记录",
            )
        mean = statistics.fmean(durations)
        p95 = _percentile(durations, 0.95)
        return MetricResult(
            dimension=MetricDimension.LATENCY,
            value=round(mean, 1),
            unit="ms",
            target=TARGET_NOTES[MetricDimension.LATENCY],
            sample_size=len(durations),
            passed=mean <= self.thresholds[MetricDimension.LATENCY],
            detail=(
                f"均值 {mean / 1000:.1f} 秒，P95 {p95 / 1000:.1f} 秒，"
                f"最快 {min(durations)} ms，最慢 {max(durations)} ms"
            ),
        )

    def _consistency(
        self,
        replays: list[list[RiskFinding]] | None,
        findings: list[RiskFinding],
        notes: list[str],
    ) -> MetricResult:
        if replays and len(replays) >= 2:
            value, detail = self._jaccard_stability(replays)
            return MetricResult(
                dimension=MetricDimension.CONSISTENCY,
                value=round(value, 4),
                unit="ratio",
                target=TARGET_NOTES[MetricDimension.CONSISTENCY],
                sample_size=len(replays),
                passed=value >= self.thresholds[MetricDimension.CONSISTENCY],
                detail=detail,
            )

        # 没有回放数据时，退而求其次：检查同批线索内部是否存在互相矛盾的等级标注
        if findings:
            titles: dict[str, set[str]] = {}
            for finding in findings:
                key = _normalize(finding.title)
                titles.setdefault(key, set()).add(finding.level.value)
            conflicting = sum(1 for levels in titles.values() if len(levels) > 1)
            if titles:
                stability = 1 - conflicting / len(titles)
                notes.append(
                    "一致性维度尚未进行重复回放测试，当前值由'同名线索等级标注是否自相矛盾'"
                    "近似估计，仅供趋势参考。建议用同一批材料连续审查两次以获得准确值。"
                )
                return MetricResult(
                    dimension=MetricDimension.CONSISTENCY,
                    value=round(stability, 4),
                    unit="ratio",
                    target=TARGET_NOTES[MetricDimension.CONSISTENCY],
                    sample_size=len(titles),
                    passed=None,
                    detail=f"同名线索 {len(titles)} 组，其中 {conflicting} 组出现等级不一致（近似估计）",
                )

        return MetricResult(
            dimension=MetricDimension.CONSISTENCY,
            value=0.0,
            unit="ratio",
            target=TARGET_NOTES[MetricDimension.CONSISTENCY],
            sample_size=0,
            passed=None,
            detail="暂无数据。一致性需对同一批材料重复审查后比对结果。",
        )

    @staticmethod
    def _jaccard_stability(replays: list[list[RiskFinding]]) -> tuple[float, str]:
        """两两比较回放结果中线索集合的 Jaccard 相似度，取均值。"""
        sets = [{_normalize(f.title) for f in run} for run in replays]
        scores: list[float] = []
        for i in range(len(sets)):
            for j in range(i + 1, len(sets)):
                a, b = sets[i], sets[j]
                if not a and not b:
                    scores.append(1.0)
                    continue
                union = a | b
                scores.append(len(a & b) / len(union) if union else 1.0)
        value = statistics.fmean(scores) if scores else 0.0
        return value, (
            f"共 {len(replays)} 次回放，两两比较 {len(scores)} 组，"
            f"线索集合平均 Jaccard 相似度 {value:.2%}"
        )

    def _explainability(self, findings: list[RiskFinding], notes: list[str]) -> MetricResult:
        if not findings:
            return MetricResult(
                dimension=MetricDimension.EXPLAINABILITY,
                value=0.0,
                unit="ratio",
                target=TARGET_NOTES[MetricDimension.EXPLAINABILITY],
                sample_size=0,
                passed=None,
                detail="暂无线索记录",
            )
        with_basis = [f for f in findings if f.matched_clauses or f.rule_hits]
        with_evidence = [f for f in findings if f.evidences]
        with_rationale = [f for f in findings if f.rationale.strip()]
        complete = [
            f
            for f in findings
            if (f.matched_clauses or f.rule_hits) and f.evidences and f.rationale.strip()
        ]
        value = len(complete) / len(findings)
        missing_basis = len(findings) - len(with_basis)
        missing_evidence = len(findings) - len(with_evidence)
        if missing_basis:
            notes.append(
                f"有 {missing_basis} 条线索未附制度依据（属经验性提示），"
                "建议补充相关制度条款以提升可解释性。"
            )
        if missing_evidence:
            notes.append(f"有 {missing_evidence} 条线索未定位到具体材料位置。")
        return MetricResult(
            dimension=MetricDimension.EXPLAINABILITY,
            value=round(value, 4),
            unit="ratio",
            target=TARGET_NOTES[MetricDimension.EXPLAINABILITY],
            sample_size=len(findings),
            passed=value >= self.thresholds[MetricDimension.EXPLAINABILITY],
            detail=(
                f"依据完整 {len(with_basis)}/{len(findings)}，"
                f"证据定位 {len(with_evidence)}/{len(findings)}，"
                f"推理说明 {len(with_rationale)}/{len(findings)}，"
                f"三者齐备 {len(complete)}/{len(findings)}"
            ),
        )

    def _adoption(self, findings: list[RiskFinding], notes: list[str]) -> MetricResult:
        if not findings:
            return MetricResult(
                dimension=MetricDimension.ADOPTION,
                value=0.0,
                unit="ratio",
                target=TARGET_NOTES[MetricDimension.ADOPTION],
                sample_size=0,
                passed=None,
                detail="暂无线索记录",
            )
        accepted = sum(1 for f in findings if f.human_verdict == HumanVerdict.ACCEPTED)
        partial = sum(1 for f in findings if f.human_verdict == HumanVerdict.PARTIAL)
        value = (accepted + partial) / len(findings)
        reviewed = len(findings) - sum(
            1 for f in findings if f.human_verdict in (HumanVerdict.PENDING, HumanVerdict.MISSED)
        )
        if reviewed == 0:
            return MetricResult(
                dimension=MetricDimension.ADOPTION,
                value=0.0,
                unit="ratio",
                target=TARGET_NOTES[MetricDimension.ADOPTION],
                sample_size=len(findings),
                passed=None,
                detail="线索尚未复核，无法计算采纳度",
            )
        return MetricResult(
            dimension=MetricDimension.ADOPTION,
            value=round(value, 4),
            unit="ratio",
            target=TARGET_NOTES[MetricDimension.ADOPTION],
            sample_size=len(findings),
            passed=value >= self.thresholds[MetricDimension.ADOPTION],
            detail=(
                f"全部 {len(findings)} 条线索中，被采纳 {accepted} 条、"
                f"部分采纳 {partial} 条（分母含未复核线索，反映实际使用率）"
            ),
        )


# ================================================================ 工具

def _normalize(text: str) -> str:
    import re

    return re.sub(r"[\s，。、；：？！（）()【】\[\]「」『』]+", "", (text or "").lower())


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[index]


def format_report(report: EvaluationReport) -> str:
    """把评测报告渲染成 Markdown。"""
    lines = ["# 风险识别能力评测报告", ""]
    lines.append(f"- 生成时间：{report.generated_at:%Y-%m-%d %H:%M:%S}")
    lines.append(f"- 覆盖任务：{len(report.task_ids)} 个")
    lines.append("")
    lines.append("| 评价维度 | 指标值 | 目标取向 | 样本量 | 是否达标 | 说明 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for metric in report.metrics:
        from ..core.enums import METRIC_DIMENSION_LABELS

        label = METRIC_DIMENSION_LABELS.get(metric.dimension, metric.dimension.value)
        if metric.unit == "ratio":
            value_text = f"{metric.value:.2%}"
        elif metric.unit == "ms":
            value_text = f"{metric.value / 1000:.1f} 秒"
        else:
            value_text = f"{metric.value:g}"
        passed_text = "—" if metric.passed is None else ("达标" if metric.passed else "未达标")
        lines.append(
            f"| {label} | {value_text} | {metric.target} | {metric.sample_size} "
            f"| {passed_text} | {metric.detail} |"
        )
    lines.append("")
    if report.notes:
        lines.append("## 说明与数据状态")
        lines.append("")
        for note in report.notes:
            lines.append(f"- {note}")
        lines.append("")
    lines.append("## 指标口径")
    lines.append("")
    lines.append("- **准确性** =（采纳数 + 0.5 × 部分采纳数）/ 已复核线索数")
    lines.append("- **召回性** = 采纳数 /（采纳数 + 人工标记的漏报数）")
    lines.append("- **时延** = 单次审查从材料解析到结论生成的总耗时")
    lines.append("- **一致性** = 同一批材料重复审查所得线索集合的 Jaccard 相似度")
    lines.append("- **可解释性** = 同时具备制度依据、材料定位与推理说明的线索占比")
    lines.append("- **审核采纳度** =（采纳数 + 部分采纳数）/ 全部线索数")
    lines.append("")
    lines.append(
        "> 基准说明：以上指标以人工复核结论为基准。在标注数据尚未标准化的阶段，"
        "评测结果随复核量积累而逐步可信；模型失效、规则冲突与人工否决等负面样本"
        "应同步记录并分析成因。"
    )
    return "\n".join(lines)


__all__ = [
    "Evaluator",
    "EvaluationInput",
    "DEFAULT_THRESHOLDS",
    "TARGET_NOTES",
    "format_report",
]
