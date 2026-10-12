"""领域数据模型——「事项—规则—证据—风险—处置」五元关联框架的代码落地。

设计要点
--------
1. **线索而非结论**：``RiskFinding`` 表达的是"可核验的风险线索"，字段设计上强制携带
   命中依据（``matched_clauses``）与关联材料（``evidences``），从数据结构层面杜绝
   "黑箱提示"。``needs_human_review`` 默认 ``True``，合规与否的判断权始终留在人这一侧。
2. **可追溯**：每个对象都带 ``*_id`` 与来源字段；``ReviewConclusion.pipeline_trace``
   记录每个阶段的耗时、所用 provider/model 与 token 消耗，构成完整审计链路。
3. **可序列化**：全部模型均可 ``model_dump(mode="json")`` 直接落 SQLite 或返回给界面。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .enums import (
    AdaptationPath,
    Capability,
    DispositionType,
    DocumentFormat,
    DocumentRole,
    EventType,
    EvidenceStatus,
    HumanVerdict,
    MetricDimension,
    ReviewStatus,
    RiskCategory,
    RiskLevel,
    RISK_LEVEL_WEIGHT,
    RuleDimension,
    StageStatus,
)


# ================================================================ 工具

def new_id(prefix: str) -> str:
    """生成带前缀的短 ID，便于在日志与界面中肉眼区分对象类型。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utc_now() -> datetime:
    """带时区的当前时间（统一存 UTC，界面按本地时区展示）。"""
    return datetime.now(timezone.utc)


class Base(BaseModel):
    """项目内所有模型的公共基类。"""

    model_config = ConfigDict(
        populate_by_name=True,
        validate_assignment=True,
        extra="allow",           # 允许携带扩展字段，避免上游多给字段就报错
        str_strip_whitespace=True,
    )


# ================================================================ 制度条款

class ClauseRef(Base):
    """制度条款引用——风险线索的"命中依据"最小单元。"""

    doc_title: str = Field(description="制度文件名称，如《国家自然科学基金资助项目资金管理办法》")
    doc_no: str = Field(default="", description="文号，如 财教〔2021〕177 号")
    article: str = Field(default="", description="条款号，如 第十二条 / 3.2")
    text: str = Field(default="", description="条款原文摘录")
    url: str = Field(default="", description="原文链接（可选）")
    effective_date: str = Field(default="", description="施行日期 YYYY-MM-DD")
    clause_id: str = Field(default="", description="知识库内的稳定标识，用于回查")

    def display(self) -> str:
        """人类可读的单行引用。"""
        parts = [self.doc_title]
        if self.doc_no:
            parts.append(f"（{self.doc_no}）")
        if self.article:
            parts.append(self.article)
        return "".join(parts)


# ================================================================ 材料

class Material(Base):
    """上传的一份业务材料（或制度文本）。"""

    material_id: str = Field(default_factory=lambda: new_id("mat"))
    filename: str = Field(description="原始文件名")
    path: str = Field(default="", description="本机绝对路径（不随材料外发）")
    fmt: DocumentFormat = Field(default=DocumentFormat.UNKNOWN, description="格式")
    role: DocumentRole = Field(default=DocumentRole.OTHER, description="在证据链中的角色")
    size_bytes: int = Field(default=0, ge=0)
    sha256: str = Field(default="", description="内容指纹，用于去重与完整性校验")
    uploaded_at: datetime = Field(default_factory=utc_now)
    page_count: int = Field(default=0, ge=0)
    text: str = Field(default="", description="提取出的纯文本正文")
    tables: list[list[list[str]]] = Field(
        default_factory=list,
        description="提取出的表格：tables[表序号][行][列]",
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="解析器补充的元信息")
    redaction_applied: bool = Field(default=False, description="是否已执行脱敏")
    redaction_log: list[str] = Field(default_factory=list, description="脱敏动作记录")
    source: str = Field(default="local_upload", description="来源：local_upload / api / folder_scan / manual")
    retention_until: str = Field(default="", description="保存期限 YYYY-MM-DD")

    @field_validator("text")
    @classmethod
    def _limit_text(cls, v: str) -> str:
        # 单份材料正文上限 200 万字符，防止异常文件拖垮内存
        return v[:2_000_000]

    @property
    def char_count(self) -> int:
        return len(self.text)

    def preview(self, limit: int = 400) -> str:
        text = self.text.strip().replace("\n", " ")
        return text[:limit] + ("…" if len(text) > limit else "")


# ================================================================ 结构化抽取结果

class ExtractedField(Base):
    """单个抽取字段及其置信度——支撑"字段级可核验"。"""

    name: str = Field(description="字段名，如 amount / budget_subject / supplier")
    value: str = Field(default="", description="规范化后的值")
    raw: str = Field(default="", description="材料中的原始表述")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_material_id: str = Field(default="", description="该字段来自哪份材料")
    locator: str = Field(default="", description="定位，如 p.3 / 表1第4行 / 单元格D7")


class ExtractedFields(Base):
    """能力① 的产出：材料理解与结构化抽取。

    字段集合对应经费材料的关键要素，同时记录附件齐全性与字段一致性判断。
    """

    fields: dict[str, ExtractedField] = Field(default_factory=dict)
    attachments_expected: list[str] = Field(default_factory=list, description="制度要求的附件清单")
    attachments_present: list[str] = Field(default_factory=list, description="实际提供的附件")
    consistency_issues: list[str] = Field(
        default_factory=list,
        description="字段间一致性疑点，如「合同金额与报销金额不一致」",
    )
    notes: list[str] = Field(default_factory=list)
    produced_by: str = Field(default="", description="产出方式：llm / rule / hybrid")
    provider: str = Field(default="")
    model: str = Field(default="")

    def get(self, name: str, default: str = "") -> str:
        f = self.fields.get(name)
        return f.value if f and f.value else default

    def confidence(self, name: str) -> float:
        f = self.fields.get(name)
        return f.confidence if f else 0.0

    @property
    def low_confidence_fields(self) -> list[str]:
        return [n for n, f in self.fields.items() if f.confidence < 0.6]

    @property
    def missing_attachments(self) -> list[str]:
        return [a for a in self.attachments_expected if a not in self.attachments_present]


# ================================================================ 规则

class Rule(Base):
    """一条可执行的风险规则。

    ``condition`` 为受限表达式（见 ``rules.engine`` 的安全求值器），可用变量：
    ``amount`` / ``budget_amount`` / ``budget_subject`` / ``purpose`` /
    ``event_type`` / ``days_after_event`` / ``contract_amount`` / ``has_contract`` /
    ``has_approval`` / ``approver_level`` / ``supplier`` / ``invoice_count`` /
    ``fields``（原始抽取字典，用 ``fields["x"]`` 取值）。
    """

    rule_id: str = Field(default_factory=lambda: new_id("rule"))
    name: str = Field(description="规则名称，如「单笔报销超预算科目余额」")
    dimension: RuleDimension = Field(description="规则维度")
    event_types: list[EventType] = Field(default_factory=list, description="适用事项；空表示全部")
    condition: str = Field(default="", description="触发条件表达式；空表示仅作知识提示不自动触发")
    requirement: str = Field(default="", description="制度要求的人话描述")
    basis: list[ClauseRef] = Field(default_factory=list, description="制度依据")
    risk_category: RiskCategory = Field(default=RiskCategory.DEVIATION)
    level_hint: RiskLevel = Field(default=RiskLevel.MEDIUM, description="命中后的初始风险等级")
    disposition_hint: DispositionType = Field(default=DispositionType.PENDING_REVIEW)
    suggestion: str = Field(default="", description="建议复核点")
    enabled: bool = Field(default=True)
    weight: float = Field(default=1.0, ge=0.0, description="命中计分权重")
    tags: list[str] = Field(default_factory=list)
    source: str = Field(default="builtin", description="builtin / institution（校内细则） / user")
    version: str = Field(default="1.0")


class RuleHit(Base):
    """规则命中记录。"""

    rule_id: str
    rule_name: str
    dimension: RuleDimension
    requirement: str = ""
    risk_category: RiskCategory
    level_hint: RiskLevel
    disposition_hint: DispositionType
    suggestion: str = ""
    matched_expression: str = Field(default="", description="命中的条件表达式原文")
    evaluated_values: dict[str, Any] = Field(default_factory=dict, description="参与求值的变量快照")
    basis: list[ClauseRef] = Field(default_factory=list)
    weight: float = 1.0


# ================================================================ 证据

class Evidence(Base):
    """关联材料片段——风险线索的"证据定位"。"""

    evidence_id: str = Field(default_factory=lambda: new_id("evd"))
    material_id: str = ""
    material_name: str = ""
    role: DocumentRole = DocumentRole.OTHER
    status: EvidenceStatus = EvidenceStatus.PRESENT
    locator: str = Field(default="", description="页码/表格/单元格等定位信息")
    snippet: str = Field(default="", description="原文摘录，供审核人快速核对")
    supports: bool = Field(default=True, description="该证据是支持还是反驳本线索")


# ================================================================ 风险线索

class RiskFinding(Base):
    """一条风险线索——系统的核心产出单元。

    刻意不命名为 "RiskConclusion"：模型输出的是待核验线索，不是合规结论。
    """

    finding_id: str = Field(default_factory=lambda: new_id("risk"))
    title: str = Field(description="线索标题，如「发票金额与报销单金额不一致」")
    category: RiskCategory = RiskCategory.DEVIATION
    level: RiskLevel = RiskLevel.UNKNOWN
    event_type: EventType = EventType.OTHER
    description: str = Field(default="", description="疑点的客观描述（不含定性结论）")
    rationale: str = Field(default="", description="能力③：为什么构成疑点——推理链条")
    matched_clauses: list[ClauseRef] = Field(default_factory=list, description="命中依据")
    evidences: list[Evidence] = Field(default_factory=list, description="关联材料")
    counter_evidence: list[str] = Field(default_factory=list, description="反向证据或不确定点（避免过度断言）")
    suggested_dispositions: list[DispositionType] = Field(default_factory=list)
    suggestion: str = Field(default="", description="建议复核点")
    rule_hits: list[RuleHit] = Field(default_factory=list, description="由哪些规则命中产生")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_capability: Capability = Capability.RISK_REASONING
    produced_by: str = Field(default="", description="rule / llm / hybrid")
    provider: str = ""
    model: str = ""
    needs_human_review: bool = Field(default=True, description="恒为 True 的设计意图：线索必须人工复核")
    human_verdict: HumanVerdict = HumanVerdict.PENDING
    human_note: str = ""
    reviewed_by: str = ""
    reviewed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def weight(self) -> int:
        return RISK_LEVEL_WEIGHT.get(self.level, 0)

    @property
    def is_actionable(self) -> bool:
        """是否达到需要人工介入的程度。"""
        return self.level in (RiskLevel.MEDIUM, RiskLevel.HIGH)


# ================================================================ 处置

class Disposition(Base):
    """处置动作记录——五元框架的最后一元，闭环的落点。"""

    disposition_id: str = Field(default_factory=lambda: new_id("dsp"))
    finding_id: str = ""
    type: DispositionType = DispositionType.PENDING_REVIEW
    note: str = ""
    actor: str = Field(default="", description="处置人（角色或账号）")
    role: str = Field(default="", description="经办人 / 审核人 / 项目负责人 / 二级单位 / 财务部门")
    decided_at: datetime | None = None
    is_exception: bool = Field(default=False, description="是否属于例外认定")
    exception_reason: str = Field(default="", description="例外说明——治理边界要求留痕")


# ================================================================ 流水线轨迹

class PipelineStageRecord(Base):
    """单个流水线阶段的执行记录，构成审计链路。"""

    stage: str = Field(description="阶段标识，如 ingest / extract / rule_check / reason / explain")
    title: str = Field(default="", description="阶段中文名")
    status: StageStatus = StageStatus.OK
    duration_ms: int = Field(default=0, ge=0)
    provider: str = ""
    model: str = ""
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    adaptation_path: AdaptationPath | None = None
    message: str = Field(default="", description="降级/失败原因")
    started_at: datetime = Field(default_factory=utc_now)


class TokenUsage(Base):
    """整条流水线的 token 汇总，用于成本核算。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# ================================================================ 审查任务与结论

class ReviewConclusion(Base):
    """一次审查的最终产出。界面、导出、评测都以此为准。"""

    conclusion_id: str = Field(default_factory=lambda: new_id("ccl"))
    task_id: str = ""
    status: ReviewStatus = ReviewStatus.PENDING
    event_type: EventType = EventType.OTHER
    findings: list[RiskFinding] = Field(default_factory=list)
    summary: str = Field(default="", description="结论摘要（客观陈述，不下合规判断）")
    overall_level: RiskLevel = RiskLevel.UNKNOWN
    recommended_actions: list[DispositionType] = Field(default_factory=list)
    statistics: dict[str, int] = Field(default_factory=dict, description="各级别线索计数")
    extracted: ExtractedFields | None = None
    pipeline_trace: list[PipelineStageRecord] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    generated_at: datetime = Field(default_factory=utc_now)
    duration_ms: int = 0
    provider: str = ""
    model: str = ""
    adaptation_paths: list[AdaptationPath] = Field(default_factory=list)
    degraded_stages: list[str] = Field(default_factory=list, description="发生降级的阶段，需向用户明示")
    disclaimer: str = Field(
        default=(
            "本结论由人工智能模型生成，仅为可核验的风险线索，不构成合规与否的认定。"
            "所有线索须经具有相应权限的人员复核后，方可作为处置依据。"
        )
    )

    def add_finding(self, finding: RiskFinding) -> None:
        self.findings.append(finding)

    def recount(self) -> "ReviewConclusion":
        """重算统计与总体等级。总体等级取最高线索等级，但证据不足时保持"待核验"。"""
        counts: dict[str, int] = {lvl.value: 0 for lvl in RiskLevel}
        for f in self.findings:
            counts[f.level.value] = counts.get(f.level.value, 0) + 1
        self.statistics = counts
        levels = [f.level for f in self.findings if f.level != RiskLevel.UNKNOWN]
        self.overall_level = max(levels, key=lambda l: RISK_LEVEL_WEIGHT[l]) if levels else RiskLevel.UNKNOWN
        actions: list[DispositionType] = []
        for f in self.findings:
            for d in f.suggested_dispositions:
                if d not in actions:
                    actions.append(d)
        self.recommended_actions = actions
        return self

    @property
    def actionable_findings(self) -> list[RiskFinding]:
        return [f for f in self.findings if f.is_actionable]

    @property
    def high_risk_count(self) -> int:
        return sum(1 for f in self.findings if f.level == RiskLevel.HIGH)


class ReviewTask(Base):
    """一次审查任务——材料集合 + 事项类型 + 结论。"""

    task_id: str = Field(default_factory=lambda: new_id("task"))
    title: str = Field(default="未命名审查任务")
    event_type: EventType = EventType.OTHER
    materials: list[Material] = Field(default_factory=list)
    status: ReviewStatus = ReviewStatus.PENDING
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    conclusion: ReviewConclusion | None = None
    reviewer: str = Field(default="", description="发起人")
    project_name: str = ""
    project_code: str = ""
    notes: str = ""

    def add_material(self, material: Material) -> None:
        self.materials.append(material)

    @property
    def material_count(self) -> int:
        return len(self.materials)

    @property
    def total_bytes(self) -> int:
        return sum(m.size_bytes for m in self.materials)

    def material_by_role(self, role: DocumentRole) -> list[Material]:
        return [m for m in self.materials if m.role == role]


# ================================================================ 评测

class MetricResult(Base):
    """单个评测维度的结果。"""

    dimension: MetricDimension
    value: float = Field(description="指标值")
    unit: str = Field(default="", description="单位：ratio / ms / count")
    target: str = Field(default="", description="目标取向描述")
    sample_size: int = Field(default=0, ge=0)
    passed: bool | None = Field(default=None, description="是否达到目标；None 表示无阈值")
    detail: str = ""


class EvaluationReport(Base):
    """六维评测报告。"""

    report_id: str = Field(default_factory=lambda: new_id("eval"))
    generated_at: datetime = Field(default_factory=utc_now)
    task_ids: list[str] = Field(default_factory=list)
    metrics: list[MetricResult] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def metric(self, dimension: MetricDimension) -> MetricResult | None:
        for m in self.metrics:
            if m.dimension == dimension:
                return m
        return None


__all__ = [
    "new_id",
    "utc_now",
    "Base",
    "ClauseRef",
    "Material",
    "ExtractedField",
    "ExtractedFields",
    "Rule",
    "RuleHit",
    "Evidence",
    "RiskFinding",
    "Disposition",
    "PipelineStageRecord",
    "TokenUsage",
    "ReviewConclusion",
    "ReviewTask",
    "MetricResult",
    "EvaluationReport",
]
