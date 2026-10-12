"""能力② 实现：制度语义匹配与风险推理。

输入两侧信息，输出统一的 ``RiskFinding`` 列表：

- **规则侧**：``RuleEngine`` 的命中结果。确定性高、可解释性强，但只覆盖
  能被表达式描述的情形。
- **模型侧**：大模型基于制度依据与材料语义给出的线索。覆盖规则之外的情形
  （跨材料语义矛盾、异常组合、情境判断），但需要与规则结果去重、去冲突。

合并原则：同一疑点两路都发现 → 合并为一条（模型负责解释，规则提供确定性依据，
置信度上调）；只有规则发现 → 保留并标注来源为规则；只有模型发现 → 保留并标注
"该线索无规则表达式支撑，属语义判断"，让审核人知道这条线索的确定性来源不同。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field as dc_field
from typing import Any, Iterable

from ..config.settings import ReviewSettings
from ..core.enums import (
    EVENT_TYPE_LABELS,
    Capability,
    DispositionType,
    EventType,
    RiskCategory,
    RiskLevel,
    StageStatus,
)
from ..core.exceptions import LLMError
from ..core.models import (
    ClauseRef,
    Evidence,
    ExtractedFields,
    Material,
    PipelineStageRecord,
    RiskFinding,
    RuleHit,
    utc_now,
)
from ..llm.registry import LLMClient
from ..prompts.reasoning import REASONING_SYSTEM, build_reasoning_user

logger = logging.getLogger(__name__)

STAGE_NAME = "reason"
STAGE_TITLE = "制度语义匹配与风险推理"


@dataclass
class ReasonOutcome:
    findings: list[RiskFinding]
    summary: str = ""
    notes: list[str] = dc_field(default_factory=list)
    stage: PipelineStageRecord = dc_field(default_factory=lambda: PipelineStageRecord(stage=STAGE_NAME))
    warnings: list[str] = dc_field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return self.stage.status == StageStatus.DEGRADED


class RiskReasoner:
    """风险推理器：规则命中 + 模型语义 → 统一风险线索。"""

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

    # ============================================================ 主流程

    def reason(
        self,
        materials: list[Material],
        *,
        materials_text: str,
        extracted: ExtractedFields,
        rule_hits: list[RuleHit],
        clause_refs: list[ClauseRef] | None = None,
        event_type: EventType = EventType.OTHER,
        rule_block: str = "",
        clause_block: str = "",
        institution_context: str = "",
        provider: str | None = None,
    ) -> ReasonOutcome:
        stage = PipelineStageRecord(stage=STAGE_NAME, title=STAGE_TITLE, started_at=utc_now())
        warnings: list[str] = []

        # ---------------- 规则侧：先转成线索（永远执行）
        rule_findings = self._findings_from_rule_hits(rule_hits, materials, event_type)

        # ---------------- 模型侧
        llm_findings: list[RiskFinding] = []
        summary = ""
        notes: list[str] = []
        provider_name = ""
        model_name = ""

        if self.enable_llm and self.client is not None:
            try:
                user_prompt = build_reasoning_user(
                    materials_text,
                    event_type_label=EVENT_TYPE_LABELS.get(event_type, event_type.value),
                    extracted_summary=_format_extracted(extracted),
                    clause_block=clause_block or _format_clauses(clause_refs or []),
                    rule_hits_block=rule_block or _format_rule_hits(rule_hits),
                    institution_context=institution_context,
                )
                response = self.client.chat_json(
                    REASONING_SYSTEM,
                    user_prompt,
                    provider=provider,
                    metadata={"task": "reason", "stage": STAGE_NAME},
                )
                payload = response.parsed if isinstance(response.parsed, dict) else {}
                llm_findings = self._parse_findings(
                    payload.get("findings") or [],
                    materials=materials,
                    event_type=event_type,
                    provider=response.provider,
                    model=response.model,
                )
                summary = str(payload.get("summary") or "")
                notes = _as_str_list(payload.get("notes"))
                provider_name = response.provider
                model_name = response.model
                stage.status = StageStatus.DEGRADED if self.client.last_degraded else StageStatus.OK
                if self.client.last_degraded:
                    stage.message = "主模型不可用，已回退到备用模型"
                stage.duration_ms = response.latency_ms
                stage.prompt_tokens = response.prompt_tokens
                stage.completion_tokens = response.completion_tokens
                warnings.extend(response.warnings)
            except LLMError as exc:
                if not self.settings.degrade_on_llm_failure:
                    raise
                stage.status = StageStatus.DEGRADED
                stage.message = f"模型推理不可用，已降级为规则结果：{exc}"
                warnings.append(
                    "风险推理已降级为规则引擎结果：仅覆盖可被规则表达式描述的疑点，"
                    "跨材料语义矛盾与异常组合可能未被发现。结论中已标注该降级信息。"
                )
                logger.warning("推理阶段降级：%s", exc)
        else:
            stage.status = StageStatus.SKIPPED
            stage.message = "未启用大模型推理，仅使用规则引擎"

        # ---------------- 合并
        merged = _merge_findings(rule_findings, llm_findings)

        # 置信度阈值过滤
        threshold = self.settings.min_confidence_to_report
        kept: list[RiskFinding] = []
        for finding in merged:
            if finding.confidence < threshold and finding.rule_hits:
                # 规则命中的线索即便置信度低也保留（确定性来源），其余按阈值过滤
                kept.append(finding)
            elif finding.confidence < threshold:
                warnings.append(f"已过滤低置信度线索：{finding.title}（{finding.confidence:.2f}）")
            else:
                kept.append(finding)

        kept.sort(
            key=lambda f: (_level_weight(f.level), f.confidence, len(f.evidences)),
            reverse=True,
        )

        stage.provider = provider_name
        stage.model = model_name
        return ReasonOutcome(
            findings=kept,
            summary=summary,
            notes=notes,
            stage=stage,
            warnings=warnings,
        )

    # ============================================================ 规则 → 线索

    def _findings_from_rule_hits(
        self,
        hits: Iterable[RuleHit],
        materials: list[Material],
        event_type: EventType,
    ) -> list[RiskFinding]:
        findings: list[RiskFinding] = []
        for hit in hits:
            evidences = _locate_evidence(materials, hit)
            rationale_parts = [f"命中规则「{hit.rule_name}」（{hit.dimension.value} 维度）。"]
            if hit.matched_expression:
                rationale_parts.append(f"触发条件：{hit.matched_expression}")
            if hit.evaluated_values:
                snapshot = "、".join(f"{k}={v}" for k, v in hit.evaluated_values.items())
                rationale_parts.append(f"判定依据的取值：{snapshot}")
            if hit.requirement:
                rationale_parts.append(f"制度要求：{hit.requirement}")
            rationale_parts.append("该线索由结构化规则引擎判定，属确定性结果。")

            findings.append(
                RiskFinding(
                    title=hit.rule_name,
                    category=hit.risk_category,
                    level=hit.level_hint,
                    event_type=event_type,
                    description=hit.requirement or hit.rule_name,
                    rationale=" ".join(rationale_parts),
                    matched_clauses=list(hit.basis),
                    evidences=evidences,
                    counter_evidence=[],
                    suggested_dispositions=[hit.disposition_hint],
                    suggestion=hit.suggestion,
                    rule_hits=[hit],
                    confidence=0.8,
                    source_capability=Capability.RISK_REASONING,
                    produced_by="rule",
                )
            )
        return findings

    # ============================================================ 模型 → 线索

    def _parse_findings(
        self,
        raw_findings: Any,
        *,
        materials: list[Material],
        event_type: EventType,
        provider: str,
        model: str,
    ) -> list[RiskFinding]:
        if not isinstance(raw_findings, list):
            return []
        name_to_id = {m.filename: m.material_id for m in materials}
        findings: list[RiskFinding] = []
        for index, raw in enumerate(raw_findings):
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            if not title:
                continue
            category = RiskCategory.from_value(raw.get("category"), RiskCategory.DEVIATION)
            level = RiskLevel.from_value(raw.get("level"), RiskLevel.UNKNOWN)
            finding_event = EventType.from_value(raw.get("event_type"), event_type)

            clauses = _parse_clauses(raw.get("matched_clauses"))
            evidences = _parse_evidences(raw.get("evidence"), name_to_id)
            if not evidences and materials:
                evidences = [
                    Evidence(
                        material_id=m.material_id,
                        material_name=m.filename,
                        role=m.role,
                        locator="未给出具体位置",
                        snippet=m.preview(160),
                    )
                    for m in materials[:2]
                ]

            dispositions = [
                d
                for d in (
                    DispositionType.from_value(x)
                    for x in _as_str_list(raw.get("suggested_dispositions"))
                )
                if d
            ] or [DispositionType.PENDING_REVIEW]

            try:
                confidence = float(raw.get("confidence") or 0.6)
            except (TypeError, ValueError):
                confidence = 0.6

            findings.append(
                RiskFinding(
                    title=title,
                    category=category,
                    level=level,
                    event_type=finding_event,
                    description=str(raw.get("description") or "").strip(),
                    rationale=str(raw.get("rationale") or "").strip(),
                    matched_clauses=clauses,
                    evidences=evidences,
                    counter_evidence=_as_str_list(raw.get("counter_evidence")),
                    suggested_dispositions=dispositions,
                    suggestion=str(raw.get("suggestion") or "").strip(),
                    confidence=max(0.0, min(1.0, confidence)),
                    source_capability=Capability.RISK_REASONING,
                    produced_by="llm",
                    provider=provider,
                    model=model,
                )
            )
        return findings


# ================================================================ 合并逻辑

def _merge_findings(
    rule_findings: list[RiskFinding],
    llm_findings: list[RiskFinding],
) -> list[RiskFinding]:
    """合并两路线索：同义合并，规则线索补充确定性依据。"""
    merged: list[RiskFinding] = []
    used_rule: set[int] = set()

    for llm_finding in llm_findings:
        match_index = _match_rule_finding(llm_finding, rule_findings, used_rule)
        if match_index is None:
            merged.append(llm_finding)
            continue
        rule_finding = rule_findings[match_index]
        used_rule.add(match_index)
        merged.append(_combine(rule_finding, llm_finding))

    for index, rule_finding in enumerate(rule_findings):
        if index in used_rule:
            continue
        merged.append(rule_finding)

    # 去掉标题完全重复的（模型偶尔重复输出）
    unique: dict[str, RiskFinding] = {}
    for finding in merged:
        key = _normalize_title(finding.title)
        if key in unique:
            existing = unique[key]
            existing.counter_evidence = list(
                dict.fromkeys(existing.counter_evidence + finding.counter_evidence)
            )
            for evidence in finding.evidences:
                if all(e.material_id != evidence.material_id or e.locator != evidence.locator
                       for e in existing.evidences):
                    existing.evidences.append(evidence)
            continue
        unique[key] = finding
    return list(unique.values())


def _match_rule_finding(
    llm_finding: RiskFinding,
    rule_findings: list[RiskFinding],
    used: set[int],
) -> int | None:
    """判断模型线索与哪条规则线索是同一疑点。"""
    llm_title = _normalize_title(llm_finding.title)
    llm_terms = _key_terms(llm_finding.title + llm_finding.description)
    best_index: int | None = None
    best_score = 0.0

    for index, rule_finding in enumerate(rule_findings):
        if index in used:
            continue
        if rule_finding.category != llm_finding.category:
            continue
        rule_title = _normalize_title(rule_finding.title)
        if rule_title and (rule_title in llm_title or llm_title in rule_title):
            return index
        rule_terms = _key_terms(rule_finding.title + rule_finding.description)
        if not llm_terms or not rule_terms:
            continue
        overlap = len(llm_terms & rule_terms) / len(llm_terms | rule_terms)
        if overlap > best_score:
            best_score = overlap
            best_index = index
    return best_index if best_score >= 0.4 else None


def _combine(rule_finding: RiskFinding, llm_finding: RiskFinding) -> RiskFinding:
    """规则确定性 + 模型语义：合并为一条更强的线索。"""
    clauses = list(rule_finding.matched_clauses)
    existing_keys = {(c.doc_title, c.article, c.text[:40]) for c in clauses}
    for clause in llm_finding.matched_clauses:
        key = (clause.doc_title, clause.article, clause.text[:40])
        if key not in existing_keys:
            clauses.append(clause)
            existing_keys.add(key)

    evidences = list(rule_finding.evidences)
    seen = {(e.material_id, e.locator) for e in evidences}
    for evidence in llm_finding.evidences:
        if (evidence.material_id, evidence.locator) not in seen:
            evidences.append(evidence)
            seen.add((evidence.material_id, evidence.locator))

    dispositions = list(rule_finding.suggested_dispositions)
    for disposition in llm_finding.suggested_dispositions:
        if disposition not in dispositions:
            dispositions.append(disposition)

    rationale_parts = [
        f"【规则判定】{rule_finding.rationale}",
        f"【模型语义分析】{llm_finding.rationale}" if llm_finding.rationale else "",
    ]

    return RiskFinding(
        title=llm_finding.title or rule_finding.title,
        category=rule_finding.category,
        level=_max_level(rule_finding.level, llm_finding.level),
        event_type=llm_finding.event_type or rule_finding.event_type,
        description=llm_finding.description or rule_finding.description,
        rationale="\n".join(p for p in rationale_parts if p),
        matched_clauses=clauses,
        evidences=evidences,
        counter_evidence=list(llm_finding.counter_evidence),
        suggested_dispositions=dispositions,
        suggestion=llm_finding.suggestion or rule_finding.suggestion,
        rule_hits=list(rule_finding.rule_hits),
        # 两路都发现 → 置信度上调，但不超过 0.95（保留"需人工复核"的空间）
        confidence=min(0.95, max(rule_finding.confidence, llm_finding.confidence) + 0.1),
        source_capability=Capability.RISK_REASONING,
        produced_by="hybrid",
        provider=llm_finding.provider,
        model=llm_finding.model,
    )


# ================================================================ 格式化与解析

def _format_extracted(extracted: ExtractedFields) -> str:
    if not extracted.fields:
        return "（未抽取到结构化字段）"
    lines: list[str] = []
    for name, item in extracted.fields.items():
        lines.append(f"- {name}: {item.value}（置信度 {item.confidence:.2f}）")
    if extracted.consistency_issues:
        lines.append("一致性疑点：" + "；".join(extracted.consistency_issues))
    if extracted.missing_attachments:
        lines.append("缺失附件：" + "、".join(extracted.missing_attachments))
    return "\n".join(lines)


def _format_clauses(clauses: Iterable[ClauseRef]) -> str:
    lines: list[str] = []
    for index, clause in enumerate(clauses, start=1):
        lines.append(f"[制度依据 {index}] {clause.display()}\n{clause.text.strip()[:400]}")
    return "\n".join(lines)


def _format_rule_hits(hits: Iterable[RuleHit]) -> str:
    lines: list[str] = []
    for index, hit in enumerate(hits, start=1):
        snapshot = "、".join(f"{k}={v}" for k, v in hit.evaluated_values.items())
        lines.append(
            f"[规则 {index}] {hit.rule_name}（{hit.dimension.value}｜{hit.risk_category.value}"
            f"｜{hit.level_hint.value}）\n  要求：{hit.requirement}\n  取值：{snapshot}"
        )
    return "\n".join(lines) if lines else "（规则引擎未命中任何确定性项）"


def _parse_clauses(raw: Any) -> list[ClauseRef]:
    if not isinstance(raw, list):
        return []
    clauses: list[ClauseRef] = []
    for item in raw:
        if isinstance(item, str):
            clauses.append(ClauseRef(doc_title=item))
        elif isinstance(item, dict):
            try:
                clauses.append(ClauseRef.model_validate(item))
            except Exception:  # noqa: BLE001 - 模型偶尔给出不规整结构
                clauses.append(ClauseRef(doc_title=str(item.get("doc_title") or ""), text=str(item)))
    return clauses


def _parse_evidences(raw: Any, name_to_id: dict[str, str]) -> list[Evidence]:
    if not isinstance(raw, list):
        return []
    evidences: list[Evidence] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        material_name = str(item.get("material_name") or item.get("material") or "").strip()
        evidences.append(
            Evidence(
                material_id=name_to_id.get(material_name, ""),
                material_name=material_name,
                locator=str(item.get("locator") or "").strip(),
                snippet=str(item.get("snippet") or "").strip()[:500],
            )
        )
    return evidences


def _locate_evidence(materials: list[Material], hit: RuleHit) -> list[Evidence]:
    """为规则命中定位证据：优先用触发值在材料中反查原文位置。"""
    needles: list[str] = []
    for key, value in hit.evaluated_values.items():
        if isinstance(value, (int, float)) and value not in (0, 0.0):
            needles.append(f"{value:g}" if isinstance(value, float) else str(value))
        elif isinstance(value, str) and value and key in ("supplier", "budget_subject", "purpose"):
            needles.append(value)

    for needle in needles:
        for material in materials:
            position = material.text.find(needle)
            if position == -1:
                continue
            return [
                Evidence(
                    material_id=material.material_id,
                    material_name=material.filename,
                    role=material.role,
                    locator=f"字符位置 {position}-{position + len(needle)}",
                    snippet=_snippet(material.text, position, len(needle)),
                )
            ]

    # 反查不到具体位置时，至少给出材料清单，不虚构定位
    return [
        Evidence(
            material_id=m.material_id,
            material_name=m.filename,
            role=m.role,
            locator="未定位到具体位置（规则基于结构化字段判定）",
            snippet=m.preview(120),
        )
        for m in materials[:1]
    ]


def _snippet(text: str, position: int, length: int, window: int = 40) -> str:
    left = max(0, position - window)
    right = min(len(text), position + length + window)
    return text[left:right].replace("\n", " ").strip()


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def _level_weight(level: RiskLevel) -> int:
    from ..core.enums import RISK_LEVEL_WEIGHT

    return RISK_LEVEL_WEIGHT.get(level, 0)


def _max_level(a: RiskLevel, b: RiskLevel) -> RiskLevel:
    return a if _level_weight(a) >= _level_weight(b) else b


_PUNCT_CHARS = (
    " \t\r\n，。、；：？！（）()【】〔〕「」『』"
    "\u201c\u201d\u2018\u2019\"'…—－-·•"
)
_PUNCT = re.compile("[" + re.escape(_PUNCT_CHARS) + "]+")


def _normalize_title(title: str) -> str:
    return _PUNCT.sub("", title).lower()


def _key_terms(text: str) -> set[str]:
    """提取用于相似度比较的关键词：中文二元组 + 英文词。"""
    from ..rules.knowledge_base import tokenize

    tokens = {t for t in tokenize(text) if len(t) >= 2}
    stop = {"材料", "存在", "需要", "应当", "情况", "问题", "可能", "建议", "相关", "不一致"}
    return tokens - stop


__all__ = ["RiskReasoner", "ReasonOutcome", "STAGE_NAME", "STAGE_TITLE"]
