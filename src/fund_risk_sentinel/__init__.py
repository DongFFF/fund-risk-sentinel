"""fund-risk-sentinel —— 高校科研经费风险智能识别系统。

按课题提出的三类核心能力组织代码：

.. code-block:: text

    ingest/  →  extract/  →  rules/  →  review/  →  explain/
    材料解析     结构化抽取    规则+知识    语义推理      解释与定位
                              ↑
                         llm/ 模型统一接入（可换任何厂商）
                              ↓
                    storage/ 持久化与审计    evaluate/ 六维评测
                              ↓
                        ui/ PySide6 桌面界面

最简用法：

.. code-block:: python

    from fund_risk_sentinel import ReviewPipeline, PipelineOptions, EventType

    pipeline = ReviewPipeline.from_config()
    conclusion = pipeline.run_paths(["报销单.docx", "发票.pdf"],
                                    options=PipelineOptions(event_type=EventType.REIMBURSEMENT))
    print(conclusion.summary)
"""

from __future__ import annotations

__version__ = "0.1.0"
__app_name__ = "高校科研经费风险智能识别系统"

from .core.enums import (
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
    RuleDimension,
    StageStatus,
)
from .core.models import (
    ClauseRef,
    Disposition,
    EvaluationReport,
    Evidence,
    ExtractedField,
    ExtractedFields,
    Material,
    MetricResult,
    ReviewConclusion,
    ReviewTask,
    RiskFinding,
    Rule,
    RuleHit,
)
from .explain import Reporter, render_markdown, render_text
from .evaluate import Evaluator, format_report
from .extract import Extractor
from .ingest import IngestPipeline, IngestResult
from .llm import LLMClient, get_client
from .review import PipelineOptions, ReviewPipeline

__all__ = [
    "__version__",
    "__app_name__",
    # 核心枚举与模型
    "EventType",
    "RiskCategory",
    "RiskLevel",
    "DispositionType",
    "EvidenceStatus",
    "RuleDimension",
    "Capability",
    "AdaptationPath",
    "DocumentFormat",
    "DocumentRole",
    "ReviewStatus",
    "HumanVerdict",
    "MetricDimension",
    "StageStatus",
    "Material",
    "ExtractedField",
    "ExtractedFields",
    "Rule",
    "RuleHit",
    "Evidence",
    "RiskFinding",
    "Disposition",
    "ClauseRef",
    "ReviewTask",
    "ReviewConclusion",
    "MetricResult",
    "EvaluationReport",
    # 主要组件
    "IngestPipeline",
    "IngestResult",
    "Extractor",
    "Reporter",
    "ReviewPipeline",
    "PipelineOptions",
    "Evaluator",
    "LLMClient",
    "get_client",
    "render_markdown",
    "render_text",
    "format_report",
]
