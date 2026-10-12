"""审查主流程：材料上传 → 结构化抽取 → 规则校验 → 制度语义匹配与风险推理 → 解释与证据定位。

.. code-block:: python

    from fund_risk_sentinel.review import ReviewPipeline, PipelineOptions
    from fund_risk_sentinel.core.enums import EventType

    pipeline = ReviewPipeline.from_config()
    conclusion = pipeline.run_paths(
        ["报销单.docx", "发票.pdf"],
        title="2026年3月差旅报销",
        options=PipelineOptions(event_type=EventType.REIMBURSEMENT),
    )
    print(conclusion.summary)
    for finding in conclusion.findings:
        print(finding.level.value, finding.title, finding.suggestion)
"""

from .pipeline import STAGE_TITLES, PipelineOptions, ReviewPipeline
from .reasoner import STAGE_NAME as REASON_STAGE_NAME
from .reasoner import STAGE_TITLE as REASON_STAGE_TITLE
from .reasoner import ReasonOutcome, RiskReasoner

__all__ = [
    "ReviewPipeline",
    "PipelineOptions",
    "STAGE_TITLES",
    "RiskReasoner",
    "ReasonOutcome",
    "REASON_STAGE_NAME",
    "REASON_STAGE_TITLE",
]
