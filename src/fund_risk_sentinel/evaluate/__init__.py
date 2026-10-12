"""六维评测：准确性 / 召回性 / 时延 / 一致性 / 可解释性 / 审核采纳度。

.. code-block:: python

    from fund_risk_sentinel.evaluate import Evaluator, format_report
    from fund_risk_sentinel.storage import open_storage

    repo, _ = open_storage()
    evaluator = Evaluator(repository=repo)
    report = evaluator.evaluate()
    print(format_report(report))
"""

from .metrics import (
    DEFAULT_THRESHOLDS,
    TARGET_NOTES,
    EvaluationInput,
    Evaluator,
    format_report,
)

__all__ = [
    "Evaluator",
    "EvaluationInput",
    "DEFAULT_THRESHOLDS",
    "TARGET_NOTES",
    "format_report",
]
