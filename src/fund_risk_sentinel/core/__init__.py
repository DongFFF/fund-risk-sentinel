"""核心层：领域模型、枚举与异常。

这一层不依赖任何外部服务，是整个系统的语义基石——「事项—规则—证据—风险—处置」
五元关联框架在这里被固化为可校验、可序列化的数据结构。
"""

from . import enums, exceptions, models
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
    RuleDimension,
    StageStatus,
)
from .exceptions import (
    ConfigError,
    IngestError,
    LLMError,
    PipelineError,
    RuleError,
    SentinelError,
    StorageError,
)
from .models import (
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
    Rule,
    RuleHit,
    TokenUsage,
    new_id,
    utc_now,
)

__all__ = [
    "enums",
    "exceptions",
    "models",
    # enums
    "AdaptationPath",
    "Capability",
    "DispositionType",
    "DocumentFormat",
    "DocumentRole",
    "EventType",
    "EvidenceStatus",
    "HumanVerdict",
    "MetricDimension",
    "ReviewStatus",
    "RiskCategory",
    "RiskLevel",
    "RuleDimension",
    "StageStatus",
    # exceptions
    "SentinelError",
    "ConfigError",
    "IngestError",
    "LLMError",
    "PipelineError",
    "RuleError",
    "StorageError",
    # models
    "ClauseRef",
    "Disposition",
    "EvaluationReport",
    "Evidence",
    "ExtractedField",
    "ExtractedFields",
    "Material",
    "MetricResult",
    "PipelineStageRecord",
    "ReviewConclusion",
    "ReviewTask",
    "RiskFinding",
    "Rule",
    "RuleHit",
    "TokenUsage",
    "new_id",
    "utc_now",
]
