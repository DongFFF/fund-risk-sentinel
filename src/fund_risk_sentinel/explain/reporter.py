"""能力③ 实现：结果解释与证据定位。

这一阶段回答审核人最关心的三个问题：

1. **这次审查发现了什么？**（summary）
2. **每条线索凭什么成立、我该去哪里核对？**（traceability）
3. **哪些地方系统其实没把握？**（uncertainties）

刻意保留一个"确定性来源"标记（``basis_quality``）：有明确制度条款支撑的线索
和仅凭经验判断的线索，对审核人的意义完全不同，不能让它们看起来一样。

另外提供 ``render_markdown`` / ``render_text``，把结论渲染成可直接归档或
粘进审批意见的格式——这是"可追溯"从数据落到人手上的最后一公里。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field as dc_field
from typing import Any, Iterable

from ..config.settings import ReviewSettings
from ..core.enums import (
    DISPOSITION_TYPE_LABELS,
    EVENT_TYPE_LABELS,
    RISK_CATEGORY_LABELS,
    RISK_LEVEL_LABELS,
    Capability,
    DispositionType,
    EventType,
    RiskLevel,
    StageStatus,
)
from ..core.exceptions import LLMError
from ..core.models import (
    Material,
    PipelineStageRecord,
    ReviewConclusion,
    RiskFinding,
    utc_now,
)
from ..llm.registry import LLMClient
from ..prompts.explanation import EXPLANATION_SYSTEM, build_explanation_user

logger = logging.getLogger(__name__)

STAGE_NAME = "explain"
STAGE_TITLE = "结果解释与证据定位"


@dataclass
class ExplanationOutcome:
    """解释阶段的产出。"""

    summary: str = ""
    uncertainties: list[str] = dc_field(default_factory=list)
    recommended_actions: list[str] = dc_field(default_factory=list)
    explanations: dict[str, str] = dc_field(default_factory=dict)
    traceability: dict[str, str] = dc_field(default_factory=dict)
    basis_quality: dict[str, str] = dc_field(default_factory=dict)
    processing_levels: dict[str, list[str]] = dc_field(default_factory=dict)
    notes: list[str] = dc_field(default_factory=list)
    stage: PipelineStageRecord = dc_field(
        default_factory=lambda: PipelineStageRecord(stage=STAGE_NAME)
    )
    warnings: list[str] = dc_field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return self.stage.status == StageStatus.DEGRADED


class Reporter:
    """结果解释生成器。"""

    def __init__(
        self,
        client: LLMClient | None = None,
        settings: ReviewSettings | None = None,
        *,
        enable_llm: bool | None = None,
    ) -> None:
        self.client = client
        self.settings = settings or ReviewSettings()
        self.enable_llm = self.settings.enable_llm_reasoning if enable_llm is None else enable_llm

    def explain(
        self,
        findings: list[RiskFinding],
        *,
        materials: list[Material] | None = None,
        provider: str | None = None,
        degraded_notes: str = "",
        event_type: EventType = EventType.OTHER,
        draft_summary: str = "",
    ) -> ExplanationOutcome:
        stage = PipelineStageRecord(stage=STAGE_NAME, title=STAGE_TITLE, started_at=utc_now())
        warnings: list[str] = []
        material_list = materials or []

        # 无线索时不做无谓的模型调用：确定性摘要更省、更稳
        if not findings:
            stage.status = StageStatus.SKIPPED
            stage.message = "未发现线索，直接生成确定性摘要"
            outcome = _deterministic_outcome(findings, event_type, material_list)
            outcome.stage = stage
            return outcome

        if not (self.enable_llm and self.client is not None):
            stage.status = StageStatus.SKIPPED
            stage.message = "未启用大模型解释，使用确定性摘要"
            outcome = _deterministic_outcome(findings, event_type, material_list)
            outcome.stage = stage
            return outcome

        try:
            user_prompt = build_explanation_user(
                _format_findings(findings),
                material_manifest=_format_manifest(material_list),
                statistics=_format_statistics(findings),
                degraded_note=degraded_notes,
            )
            response = self.client.chat_json(
                EXPLANATION_SYSTEM,
                user_prompt,
                provider=provider,
                temperature=0.1,
                metadata={"task": "explain", "stage": STAGE_NAME},
            )
            payload = response.parsed if isinstance(response.parsed, dict) else {}
            outcome = _parse_explanation(payload, findings, event_type, material_list)
            if not outcome.summary:
                outcome.summary = draft_summary or _deterministic_outcome(
                    findings, event_type, material_list
                ).summary
            stage.status = StageStatus.DEGRADED if self.client.last_degraded else StageStatus.OK
            if self.client.last_degraded:
                stage.message = "主模型不可用，已回退到备用模型"
            stage.duration_ms = response.latency_ms
            stage.prompt_tokens = response.prompt_tokens
            stage.completion_tokens = response.completion_tokens
            stage.provider = response.provider
            stage.model = response.model
            warnings.extend(response.warnings)
        except LLMError as exc:
            if not self.settings.degrade_on_llm_failure:
                raise
            stage.status = StageStatus.DEGRADED
            stage.message = f"模型解释不可用，已降级为确定性摘要：{exc}"
            warnings.append(
                "结果解释已降级为确定性摘要（未使用模型润色），线索本身不受影响。"
            )
            logger.warning("解释阶段降级：%s", exc)
            outcome = _deterministic_outcome(findings, event_type, material_list)
            outcome.stage = stage
            outcome.warnings = warnings
            return outcome

        outcome.stage = stage
        outcome.warnings = warnings
        return outcome


# ================================================================ 确定性兜底

def _deterministic_outcome(
    findings: list[RiskFinding],
    event_type: EventType,
    materials: list[Material],
) -> ExplanationOutcome:
    """不依赖模型生成摘要与建议——保证"模型挂了也有可交付的解释"。"""
    label = EVENT_TYPE_LABELS.get(event_type, event_type.value)
    by_level: dict[RiskLevel, list[RiskFinding]] = {}
    by_category: dict[str, int] = {}
    for finding in findings:
        by_level.setdefault(finding.level, []).append(finding)
        key = RISK_CATEGORY_LABELS.get(finding.category, finding.category.value)
        by_category[key] = by_category.get(key, 0) + 1

    if not findings:
        summary = (
            f"本次{label}审查覆盖 {len(materials)} 份材料，"
            "未发现需人工介入的风险线索。仍建议按常规流程复核后办理。"
        )
        return ExplanationOutcome(
            summary=summary,
            recommended_actions=["按正常流程办理，并保留本次审查记录以备追溯。"],
        )

    level_parts = [
        f"{RISK_LEVEL_LABELS.get(level, level.value)} {len(items)} 条"
        for level, items in sorted(by_level.items(), key=lambda kv: -_level_weight(kv[0]))
    ]
    category_parts = [f"{k} {v} 条" for k, v in by_category.items()]
    summary = (
        f"本次{label}审查覆盖 {len(materials)} 份材料，"
        f"共形成 {len(findings)} 条待核验线索（{'，'.join(level_parts)}）。"
        f"线索类型分布：{'，'.join(category_parts)}。"
        "每条线索均附命中依据与关联材料位置，请复核后决定处置方式。"
    )

    uncertainties = [
        f"{finding.title}：{item}"
        for finding in findings
        for item in finding.counter_evidence
    ][:10]

    actions: list[str] = []
    high = by_level.get(RiskLevel.HIGH, [])
    if high:
        actions.append(
            "以下线索涉及明确的材料证据，建议在支出办理前处理："
            + "、".join(f.title for f in high[:5])
        )
    medium = by_level.get(RiskLevel.MEDIUM, [])
    if medium:
        actions.append(
            "以下线索需补充材料或人工确认后再办理："
            + "、".join(f.title for f in medium[:5])
        )
    unknown = by_level.get(RiskLevel.UNKNOWN, [])
    if unknown:
        actions.append(
            "以下线索因证据不足暂无法定级，建议优先补齐材料后重新审查："
            + "、".join(f.title for f in unknown[:5])
        )
    if not actions:
        actions.append("按正常流程办理，并保留本次审查记录以备追溯。")

    return ExplanationOutcome(
        summary=summary,
        uncertainties=uncertainties,
        recommended_actions=actions,
        explanations={f.title: f.description or f.rationale for f in findings},
        traceability={f.title: _traceability(f) for f in findings},
        basis_quality={f.title: _basis_quality(f) for f in findings},
        processing_levels={
            "before_processing": [f.title for f in high],
            "during_processing": [f.title for f in medium],
            "for_record": [f.title for f in by_level.get(RiskLevel.LOW, [])],
        },
    )


def _parse_explanation(
    payload: dict[str, Any],
    findings: list[RiskFinding],
    event_type: EventType,
    materials: list[Material],
) -> ExplanationOutcome:
    explanations: dict[str, str] = {}
    traceability: dict[str, str] = {}
    basis_quality: dict[str, str] = {}
    for item in payload.get("finding_explanations") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        if item.get("plain_explanation"):
            explanations[title] = str(item["plain_explanation"]).strip()
        if item.get("traceability"):
            traceability[title] = str(item["traceability"]).strip()
        if item.get("basis_quality"):
            basis_quality[title] = str(item["basis_quality"]).strip()

    # 模型可能改写了标题，导致映射不上——用兜底值补齐
    for finding in findings:
        explanations.setdefault(finding.title, finding.description or finding.rationale)
        traceability.setdefault(finding.title, _traceability(finding))
        basis_quality.setdefault(finding.title, _basis_quality(finding))

    levels = payload.get("processing_levels") or {}
    processing_levels = {
        key: [str(x) for x in (levels.get(key) or [])]
        for key in ("before_processing", "during_processing", "for_record")
    }

    fallback = _deterministic_outcome(findings, event_type, materials)

    # 一致性护栏：模型可能把线索条数说错（上下文被截断、或它自行"归纳"了）。
    # 摘要条数与实际线索数不一致时，宁可退回确定性摘要——结论自相矛盾比不润色更糟。
    summary = str(payload.get("summary") or "").strip()
    stated_total = _extract_stated_total(summary)
    if summary and stated_total is not None and stated_total != len(findings):
        logger.warning(
            "模型摘要称 %d 条线索，实际 %d 条，已改用确定性摘要", stated_total, len(findings)
        )
        summary = ""

    return ExplanationOutcome(
        summary=summary or fallback.summary,
        uncertainties=[str(x) for x in (payload.get("uncertainties") or [])] or fallback.uncertainties,
        recommended_actions=[
            str(x) for x in (payload.get("recommended_actions") or [])
        ] or fallback.recommended_actions,
        explanations=explanations,
        traceability=traceability,
        basis_quality=basis_quality,
        processing_levels=processing_levels,
        notes=[str(x) for x in (payload.get("notes") or [])],
    )


#: 匹配摘要里的线索总数声明，如"共形成 12 条""共 12 条线索"
_STATED_TOTAL_RE = re.compile(r"共(?:形成|发现|识别出?|计)?\s*(\d+)\s*条")


def _extract_stated_total(text: str) -> int | None:
    """从摘要中解析出模型自称的线索条数；解析不到返回 ``None``。"""
    if not text:
        return None
    match = _STATED_TOTAL_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):  # pragma: no cover - 正则已保证是数字
        return None


# ================================================================ 渲染

def render_markdown(conclusion: ReviewConclusion, *, title: str = "科研经费风险审查结论") -> str:
    """把结论渲染成 Markdown，可直接归档或作为审批意见附件。"""
    lines: list[str] = [f"# {title}", ""]
    lines.append(f"- 生成时间：{conclusion.generated_at:%Y-%m-%d %H:%M:%S}")
    lines.append(f"- 事项类型：{EVENT_TYPE_LABELS.get(conclusion.event_type, conclusion.event_type.value)}")
    lines.append(f"- 总体风险等级：{RISK_LEVEL_LABELS.get(conclusion.overall_level, conclusion.overall_level.value)}")
    lines.append(f"- 线索数量：{len(conclusion.findings)} 条")
    lines.append(f"- 使用模型：{conclusion.provider or '未使用模型'} / {conclusion.model or '-'}")
    lines.append(f"- 耗时：{conclusion.duration_ms} ms")
    if conclusion.degraded_stages:
        lines.append(f"- ⚠ 降级执行的阶段：{'、'.join(conclusion.degraded_stages)}")
    lines.append("")

    lines.append("## 一、结论摘要")
    lines.append("")
    lines.append(conclusion.summary or "（无摘要）")
    lines.append("")

    lines.append("## 二、风险线索明细")
    lines.append("")
    if not conclusion.findings:
        lines.append("未发现需人工介入的风险线索。")
        lines.append("")
    for index, finding in enumerate(conclusion.findings, start=1):
        level_label = RISK_LEVEL_LABELS.get(finding.level, finding.level.value)
        category_label = RISK_CATEGORY_LABELS.get(finding.category, finding.category.value)
        lines.append(f"### {index}. {finding.title}")
        lines.append("")
        lines.append(f"- **风险等级**：{level_label}")
        lines.append(f"- **风险类别**：{category_label}")
        lines.append(f"- **置信度**：{finding.confidence:.2f}")
        lines.append(f"- **产生方式**：{finding.produced_by}")
        lines.append("")
        lines.append(f"**疑点描述**：{finding.description or '（无）'}")
        lines.append("")
        if finding.rationale:
            lines.append(f"**推理与依据**：{finding.rationale}")
            lines.append("")
        if finding.matched_clauses:
            lines.append("**命中制度依据**：")
            lines.append("")
            for clause in finding.matched_clauses:
                text = clause.text.strip().replace("\n", " ")
                lines.append(f"- {clause.display()}" + (f"：{text[:200]}" if text else ""))
            lines.append("")
        if finding.evidences:
            lines.append("**关联材料位置**：")
            lines.append("")
            for evidence in finding.evidences:
                lines.append(
                    f"- {evidence.material_name}"
                    + (f"｜{evidence.locator}" if evidence.locator else "")
                    + (f"｜原文：{evidence.snippet[:120]}" if evidence.snippet else "")
                )
            lines.append("")
        if finding.counter_evidence:
            lines.append("**不确定点 / 反向证据**：")
            lines.append("")
            for item in finding.counter_evidence:
                lines.append(f"- {item}")
            lines.append("")
        if finding.suggested_dispositions:
            labels = [
                DISPOSITION_TYPE_LABELS.get(d, d.value) for d in finding.suggested_dispositions
            ]
            lines.append(f"**建议处置**：{'、'.join(labels)}")
            lines.append("")
        if finding.suggestion:
            lines.append(f"**建议复核点**：{finding.suggestion}")
            lines.append("")

    lines.append("## 三、处置建议")
    lines.append("")
    for action in conclusion.recommended_actions:
        lines.append(f"- {DISPOSITION_TYPE_LABELS.get(action, action.value)}")
    lines.append("")

    if conclusion.extracted is not None:
        lines.append("## 四、结构化抽取结果")
        lines.append("")
        if conclusion.extracted.fields:
            lines.append("| 字段 | 值 | 置信度 |")
            lines.append("| --- | --- | --- |")
            for name, item in conclusion.extracted.fields.items():
                lines.append(f"| {name} | {item.value} | {item.confidence:.2f} |")
        else:
            lines.append("（未抽取到字段）")
        lines.append("")
        if conclusion.extracted.missing_attachments:
            lines.append(
                "**缺失附件**：" + "、".join(conclusion.extracted.missing_attachments)
            )
            lines.append("")

    lines.append("## 五、执行轨迹")
    lines.append("")
    lines.append("| 阶段 | 状态 | 耗时(ms) | 模型 | 说明 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for stage in conclusion.pipeline_trace:
        lines.append(
            f"| {stage.title or stage.stage} | {stage.status.value} | {stage.duration_ms} "
            f"| {stage.model or '-'} | {stage.message or ''} |"
        )
    lines.append("")

    lines.append("## 六、声明")
    lines.append("")
    lines.append(conclusion.disclaimer)
    lines.append("")
    return "\n".join(lines)


def render_text(conclusion: ReviewConclusion) -> str:
    """纯文本渲染（用于终端输出与日志）。"""
    lines = [
        f"总体等级：{RISK_LEVEL_LABELS.get(conclusion.overall_level, conclusion.overall_level.value)}"
        f"　线索数：{len(conclusion.findings)}　耗时：{conclusion.duration_ms} ms",
        f"摘要：{conclusion.summary}",
        "",
    ]
    for index, finding in enumerate(conclusion.findings, start=1):
        lines.append(
            f"{index}. [{RISK_LEVEL_LABELS.get(finding.level, finding.level.value)}]"
            f"[{RISK_CATEGORY_LABELS.get(finding.category, finding.category.value)}] {finding.title}"
        )
        if finding.suggestion:
            lines.append(f"   建议：{finding.suggestion}")
    if conclusion.degraded_stages:
        lines.append("")
        lines.append(f"⚠ 降级阶段：{'、'.join(conclusion.degraded_stages)}")
    return "\n".join(lines)


# ================================================================ 辅助

def _traceability(finding: RiskFinding) -> str:
    parts: list[str] = []
    if finding.matched_clauses:
        parts.append("制度依据：" + "；".join(c.display() for c in finding.matched_clauses[:3]))
    if finding.evidences:
        parts.append(
            "材料位置："
            + "；".join(
                f"{e.material_name}{('｜' + e.locator) if e.locator else ''}"
                for e in finding.evidences[:3]
            )
        )
    return "　".join(parts) or "未定位到明确依据"


def _basis_quality(finding: RiskFinding) -> str:
    if finding.matched_clauses:
        return "explicit"
    if finding.rule_hits:
        return "explicit"
    return "experience"


def _format_findings(findings: Iterable[RiskFinding]) -> str:
    blocks: list[str] = []
    for index, finding in enumerate(findings, start=1):
        lines = [
            f"[线索 {index}] {finding.title}",
            f"  类别：{finding.category.value}　等级：{finding.level.value}　置信度：{finding.confidence:.2f}",
            f"  描述：{finding.description}",
            f"  推理：{finding.rationale[:400]}",
        ]
        if finding.matched_clauses:
            lines.append(
                "  依据：" + "；".join(c.display() for c in finding.matched_clauses[:3])
            )
        if finding.evidences:
            lines.append(
                "  材料：" + "；".join(
                    f"{e.material_name}{('｜' + e.locator) if e.locator else ''}"
                    for e in finding.evidences[:3]
                )
            )
        if finding.counter_evidence:
            lines.append("  不确定点：" + "；".join(finding.counter_evidence[:3]))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _format_manifest(materials: list[Material]) -> str:
    return "\n".join(
        f"{i}. {m.filename}（{m.role.value}，{m.char_count} 字符）"
        for i, m in enumerate(materials, start=1)
    )


def _format_statistics(findings: list[RiskFinding]) -> str:
    by_level: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for finding in findings:
        by_level[finding.level.value] = by_level.get(finding.level.value, 0) + 1
        by_category[finding.category.value] = by_category.get(finding.category.value, 0) + 1
    return (
        f"总数 {len(findings)}；"
        f"按等级 {'，'.join(f'{k}:{v}' for k, v in by_level.items())}；"
        f"按类别 {'，'.join(f'{k}:{v}' for k, v in by_category.items())}"
    )


def _level_weight(level: RiskLevel) -> int:
    from ..core.enums import RISK_LEVEL_WEIGHT

    return RISK_LEVEL_WEIGHT.get(level, 0)


__all__ = [
    "Reporter",
    "ExplanationOutcome",
    "render_markdown",
    "render_text",
    "STAGE_NAME",
    "STAGE_TITLE",
]
