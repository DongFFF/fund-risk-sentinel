"""审查主流程编排——五阶段流水线。

::

    材料上传(ingest) ──► ① 结构化抽取(extract) ──► 规则校验(rules)
                                    │                    │
                                    └──────► ② 制度语义匹配与风险推理(review)
                                                     │
                                                     ▼
                                          ③ 结果解释与证据定位(explain)
                                                     │
                                                     ▼
                                            ReviewConclusion

设计要点：

- **任一阶段失败不中断整体**：默认降级并记录在 ``pipeline_trace`` 与
  ``degraded_stages``，结论里明确告诉使用者"这次哪些能力没跑到"。
- **可观测**：每个阶段的耗时、provider、model、token 都进 trace，
  既支撑六维评测中的"时延"，也支撑成本核算。
- **不越权**：结论恒为"待核验线索"，``disclaimer`` 强制随结论一起展示。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from ..config.settings import ConfigManager, get_manager
from ..core.enums import (
    EVENT_TYPE_LABELS,
    AdaptationPath,
    DocumentRole,
    EventType,
    ReviewStatus,
    RiskLevel,
    StageStatus,
)
from ..core.models import (
    ExtractedFields,
    Material,
    PipelineStageRecord,
    ReviewConclusion,
    ReviewTask,
    RiskFinding,
    Rule,
    new_id,
    utc_now,
)
from ..extract.extractor import Extractor
from ..explain.reporter import Reporter
from ..ingest.pipeline import IngestPipeline
from ..llm.registry import LLMClient, get_client
from ..rules.engine import RuleEngine, build_facts
from ..rules.knowledge_base import KnowledgeBase, load_knowledge_base
from ..rules.loader import load_all_rules
from .reasoner import RiskReasoner

logger = logging.getLogger(__name__)

StageCallback = Callable[[PipelineStageRecord], None]

#: 各阶段中文名，供界面进度展示
STAGE_TITLES = {
    "ingest": "材料上传与解析",
    "assemble": "上下文组装",
    "extract": "材料理解与结构化抽取",
    "rule_check": "结构化规则校验",
    "retrieve": "制度依据检索",
    "reason": "制度语义匹配与风险推理",
    "explain": "结果解释与证据定位",
}


@dataclass
class PipelineOptions:
    """单次审查的运行时选项。"""

    event_type: EventType = EventType.OTHER
    provider: str | None = None
    enable_rules: bool | None = None
    enable_rag: bool | None = None
    enable_llm: bool | None = None
    institution_context: str = ""
    on_stage: StageCallback | None = None


class ReviewPipeline:
    """审查流水线。"""

    def __init__(
        self,
        *,
        config: ConfigManager | None = None,
        client: LLMClient | None = None,
        rule_engine: RuleEngine | None = None,
        knowledge_base: KnowledgeBase | None = None,
    ) -> None:
        self.config = config or get_manager()
        self.client = client or get_client()
        self.settings = self.config.settings
        self.rule_engine = rule_engine if rule_engine is not None else RuleEngine()
        self.knowledge_base = knowledge_base if knowledge_base is not None else KnowledgeBase()
        self.ingest = IngestPipeline(
            security=self.settings.security,
            review=self.settings.review,
        )
        self.extractor = Extractor(self.client, self.settings.review)
        self.reasoner = RiskReasoner(self.client, self.settings.review)
        self.reporter = Reporter(self.client, self.settings.review)

    # ============================================================ 构造

    @classmethod
    def from_config(
        cls,
        config: ConfigManager | None = None,
        *,
        load_rules: bool = True,
        load_kb: bool = True,
    ) -> "ReviewPipeline":
        """按配置加载规则库与知识库，构造可直接使用的流水线。"""
        cfg = config or get_manager()
        engine = RuleEngine()
        if load_rules:
            try:
                rules = load_all_rules(cfg.settings.resolved_rules_dir())
                engine.add_rules(rules)
                logger.info("已加载 %d 条规则", len(rules))
            except Exception as exc:  # noqa: BLE001 - 规则加载失败不应阻止程序启动
                logger.warning("规则库加载失败：%s", exc)
        kb = KnowledgeBase()
        if load_kb:
            try:
                kb = load_knowledge_base(cfg.settings.resolved_knowledge_dir())
                logger.info("已加载知识库条款 %d 条", len(kb))
            except Exception as exc:  # noqa: BLE001
                logger.warning("知识库加载失败：%s", exc)
        return cls(config=cfg, client=get_client(), rule_engine=engine, knowledge_base=kb)

    # ============================================================ 便捷入口

    def run_paths(
        self,
        paths: Iterable[Path | str],
        *,
        title: str = "",
        options: PipelineOptions | None = None,
        reviewer: str = "",
    ) -> ReviewConclusion:
        """从文件路径直接跑完整个流程（含材料解析）。"""
        opts = options or PipelineOptions()
        started = time.perf_counter()
        trace: list[PipelineStageRecord] = []
        result = self.ingest.load_paths(paths)
        trace.append(_ingest_stage(result, started))
        _emit(opts, trace[-1])
        return self._run_core(
            materials=result.materials,
            title=title or "审查任务",
            options=opts,
            reviewer=reviewer,
            trace=trace,
            ingest_warnings=result.warnings,
            ingest_summary=result.summary(),
        )

    def run_materials(
        self,
        materials: list[Material],
        *,
        title: str = "",
        options: PipelineOptions | None = None,
        reviewer: str = "",
        ingest_summary: str = "",
        ingest_warnings: list[str] | None = None,
    ) -> ReviewConclusion:
        """对已解析好的材料集合执行审查。"""
        opts = options or PipelineOptions()
        return self._run_core(
            materials=materials,
            title=title or "审查任务",
            options=opts,
            reviewer=reviewer,
            trace=[],
            ingest_warnings=ingest_warnings or [],
            ingest_summary=ingest_summary,
        )

    def run_task(self, task: ReviewTask, options: PipelineOptions | None = None) -> ReviewConclusion:
        """对 ``ReviewTask`` 执行审查，并把结论写回任务对象。"""
        opts = options or PipelineOptions(event_type=task.event_type)
        conclusion = self.run_materials(
            task.materials,
            title=task.title,
            options=opts,
            reviewer=task.reviewer,
        )
        conclusion.task_id = task.task_id
        task.conclusion = conclusion
        task.status = ReviewStatus.NEEDS_HUMAN if conclusion.findings else ReviewStatus.COMPLETED
        task.finished_at = utc_now()
        return conclusion
    # ============================================================ 核心流程

    def _run_core(
        self,
        *,
        materials: list[Material],
        title: str,
        options: PipelineOptions,
        reviewer: str,
        trace: list[PipelineStageRecord],
        ingest_warnings: list[str],
        ingest_summary: str,
        task_id: str = "",
    ) -> ReviewConclusion:
        started = time.perf_counter()
        warnings: list[str] = list(ingest_warnings)
        # 每条结论都必须挂在一个任务号下——可追溯是这套框架的底线要求，
        # 即使调用方不落库（如命令行单次运行），也要留下可引用的任务标识。
        conclusion = ReviewConclusion(
            task_id=task_id or new_id("task"),
            event_type=options.event_type,
            status=ReviewStatus.RUNNING,
        )

        enable_rules = (
            self.settings.review.enable_rule_engine if options.enable_rules is None else options.enable_rules
        )
        enable_rag = self.settings.review.enable_rag if options.enable_rag is None else options.enable_rag
        enable_llm = (
            self.settings.review.enable_llm_reasoning if options.enable_llm is None else options.enable_llm
        )

        if not materials:
            conclusion.status = ReviewStatus.FAILED
            conclusion.summary = "未收到任何可解析的材料，无法开展审查。"
            conclusion.pipeline_trace = trace
            conclusion.duration_ms = int((time.perf_counter() - started) * 1000)
            return conclusion

        # ---------------- 阶段：上下文组装
        assemble_started = time.perf_counter()
        materials_text, assemble_warnings = self.ingest.assemble_context(materials)
        warnings.extend(assemble_warnings)
        trace.append(
            PipelineStageRecord(
                stage="assemble",
                title=STAGE_TITLES["assemble"],
                status=StageStatus.OK,
                duration_ms=int((time.perf_counter() - assemble_started) * 1000),
                message=f"共 {len(materials)} 份材料，{len(materials_text):,} 字符",
            )
        )
        _emit(options, trace[-1])

        # ---------------- 阶段 ①：结构化抽取
        extraction = self.extractor.extract(
            materials,
            materials_text=materials_text,
            event_type=options.event_type,
            rule_hints=self._rule_hints(options.event_type),
            provider=options.provider,
        )
        trace.append(extraction.stage)
        warnings.extend(extraction.warnings)
        conclusion.extracted = extraction.fields
        _emit(options, extraction.stage)

        # ---------------- 阶段：规则校验
        rule_hits: list = []
        if enable_rules and self.rule_engine.rules:
            rule_started = time.perf_counter()
            facts = build_facts(extraction.fields, materials, event_type=options.event_type)
            rule_hits = self.rule_engine.evaluate(facts, event_type=options.event_type)
            trace.append(
                PipelineStageRecord(
                    stage="rule_check",
                    title=STAGE_TITLES["rule_check"],
                    status=StageStatus.OK,
                    duration_ms=int((time.perf_counter() - rule_started) * 1000),
                    message=f"规则 {len(self.rule_engine.rules)} 条，命中 {len(rule_hits)} 条",
                )
            )
        else:
            trace.append(
                PipelineStageRecord(
                    stage="rule_check",
                    title=STAGE_TITLES["rule_check"],
                    status=StageStatus.SKIPPED,
                    message="未启用规则引擎或规则库为空",
                )
            )
        _emit(options, trace[-1])

        # ---------------- 阶段：制度依据检索
        clause_refs: list = []
        if enable_rag and len(self.knowledge_base) > 0:
            retrieve_started = time.perf_counter()
            clause_refs = self._retrieve_clauses(materials, extraction.fields)
            trace.append(
                PipelineStageRecord(
                    stage="retrieve",
                    title=STAGE_TITLES["retrieve"],
                    status=StageStatus.OK,
                    duration_ms=int((time.perf_counter() - retrieve_started) * 1000),
                    message=f"召回 {len(clause_refs)} 条制度依据（库内 {len(self.knowledge_base)} 条）",
                    adaptation_path=AdaptationPath.RAG,
                )
            )
        else:
            trace.append(
                PipelineStageRecord(
                    stage="retrieve",
                    title=STAGE_TITLES["retrieve"],
                    status=StageStatus.SKIPPED,
                    message="未启用知识检索或知识库为空",
                )
            )
        _emit(options, trace[-1])

        # ---------------- 阶段 ②：风险推理
        reasoning = self.reasoner.reason(
            materials,
            materials_text=materials_text,
            extracted=extraction.fields,
            rule_hits=rule_hits,
            clause_refs=clause_refs,
            event_type=options.event_type,
            clause_block=self.knowledge_base.format_for_prompt(clause_refs),
            institution_context=options.institution_context,
            provider=options.provider,
        )
        trace.append(reasoning.stage)
        warnings.extend(reasoning.warnings)
        conclusion.findings = reasoning.findings
        _emit(options, reasoning.stage)

        # ---------------- 阶段 ③：解释与证据定位
        explanation = self.reporter.explain(
            reasoning.findings,
            materials=materials,
            provider=options.provider,
            degraded_notes=_degraded_notes(trace),
            event_type=options.event_type,
            draft_summary=reasoning.summary,
        )
        trace.append(explanation.stage)
        warnings.extend(explanation.warnings)
        _emit(options, explanation.stage)

        # ---------------- 收口
        conclusion.summary = explanation.summary or _fallback_summary(reasoning.findings, options.event_type)
        conclusion.recount()
        conclusion.pipeline_trace = trace
        conclusion.duration_ms = int((time.perf_counter() - started) * 1000)
        conclusion.provider = _last_provider(trace)
        conclusion.model = _last_model(trace)
        conclusion.usage = self.client.usage
        conclusion.adaptation_paths = _adaptation_paths(trace, self.config, options)
        conclusion.degraded_stages = [s.stage for s in trace if s.status == StageStatus.DEGRADED]
        conclusion.status = (
            ReviewStatus.NEEDS_HUMAN
            if conclusion.findings or self.settings.review.require_human_review
            else ReviewStatus.COMPLETED
        )
        if warnings:
            conclusion.disclaimer = (
                conclusion.disclaimer
                + "\n\n本次审查的提示信息：\n- "
                + "\n- ".join(dict.fromkeys(warnings))
            )
        if explanation.uncertainties:
            conclusion.disclaimer = (
                conclusion.disclaimer
                + "\n\n未能确认的信息（建议优先补充材料）：\n- "
                + "\n- ".join(explanation.uncertainties)
            )
        logger.info(
            "审查完成：%s，线索 %d 条，耗时 %d ms，降级阶段 %s",
            title,
            len(conclusion.findings),
            conclusion.duration_ms,
            conclusion.degraded_stages or "无",
        )
        return conclusion

    # ============================================================ 内部

    def _rule_hints(self, event_type: EventType) -> list[str]:
        """把规则库里的"要求"描述抽成短提示，给模型对齐口径。"""
        hints: list[str] = []
        for rule in self.rule_engine.enabled_rules():
            if rule.event_types and event_type not in rule.event_types:
                continue
            if rule.requirement:
                hints.append(f"{rule.name}：{rule.requirement}")
            if len(hints) >= 20:
                break
        return hints

    def _retrieve_clauses(self, materials: list[Material], extracted: ExtractedFields):
        """按材料角色分别检索再合并，召回更稳。"""
        queries: list[str] = []
        if extracted.consistency_issues:
            queries.append(" ".join(extracted.consistency_issues))
        for field_name in ("budget_subject", "purpose", "event_type"):
            value = extracted.get(field_name)
            if value:
                queries.append(value)
        for material in materials:
            if material.role == DocumentRole.POLICY:
                continue
            queries.append(material.text[:1200])
        queries = [q for q in queries if q.strip()]
        if not queries:
            queries = ["科研经费 报销 审批 额度 用途"]
        return self.knowledge_base.search_multi(queries, top_k=self.settings.review.rag_top_k)


# ================================================================ 辅助

def _emit(options: PipelineOptions, stage: PipelineStageRecord) -> None:
    if options.on_stage:
        try:
            options.on_stage(stage)
        except Exception:  # noqa: BLE001 - 进度回调不应影响主流程
            logger.exception("进度回调执行失败")


def _ingest_stage(result, started: float) -> PipelineStageRecord:
    status = StageStatus.OK if result.materials else StageStatus.FAILED
    message = result.summary()
    if result.failed:
        message += "；失败：" + "、".join(f"{i.filename}({i.error})" for i in result.failed[:3])
    return PipelineStageRecord(
        stage="ingest",
        title=STAGE_TITLES["ingest"],
        status=status,
        duration_ms=int((time.perf_counter() - started) * 1000),
        message=message,
    )


def _last_provider(trace: list[PipelineStageRecord]) -> str:
    for stage in reversed(trace):
        if stage.provider:
            return stage.provider
    return ""


def _last_model(trace: list[PipelineStageRecord]) -> str:
    for stage in reversed(trace):
        if stage.model:
            return stage.model
    return ""


def _degraded_notes(trace: list[PipelineStageRecord]) -> str:
    degraded = [s for s in trace if s.status == StageStatus.DEGRADED]
    if not degraded:
        return ""
    lines = [f"- {s.title}：{s.message}" for s in degraded]
    return "本次审查存在降级执行的能力，请在解读结论时考虑其覆盖范围：\n" + "\n".join(lines)


def _adaptation_paths(
    trace: list[PipelineStageRecord],
    config: ConfigManager,
    options: PipelineOptions,
) -> list[AdaptationPath]:
    """标注本次审查实际使用了课题提出的哪几条适配路径。"""
    paths: list[AdaptationPath] = []
    if any(s.stage == "rule_check" and s.status == StageStatus.OK for s in trace):
        paths.append(AdaptationPath.RULE_KB)
    if any(s.adaptation_path == AdaptationPath.RAG for s in trace):
        paths.append(AdaptationPath.RAG)
    if any(s.stage in ("extract", "reason", "explain") and s.provider for s in trace):
        paths.append(AdaptationPath.PROMPT)
    try:
        active = config.provider(options.provider)
        if active.adaptation_path == AdaptationPath.PEFT:
            paths.append(AdaptationPath.PEFT)
    except Exception:  # noqa: BLE001 - provider 不存在时忽略
        pass
    unique: list[AdaptationPath] = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return unique


def _fallback_summary(findings: list[RiskFinding], event_type: EventType) -> str:
    label = EVENT_TYPE_LABELS.get(event_type, event_type.value)
    if not findings:
        return f"本次{label}审查未发现需人工介入的风险线索。"
    counts: dict[RiskLevel, int] = {}
    for finding in findings:
        counts[finding.level] = counts.get(finding.level, 0) + 1
    parts = [f"{level.value} {n} 条" for level, n in counts.items()]
    return (
        f"本次{label}审查共形成 {len(findings)} 条待核验线索（{'，'.join(parts)}）。"
        "线索均附命中依据与关联材料位置，请复核后决定处置方式。"
    )


__all__ = ["ReviewPipeline", "PipelineOptions", "STAGE_TITLES"]
