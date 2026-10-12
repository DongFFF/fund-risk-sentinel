"""能力③ 结果解释与证据定位。

.. code-block:: python

    from fund_risk_sentinel.explain import Reporter, render_markdown

    reporter = Reporter(client)
    outcome = reporter.explain(findings, materials=materials)
    print(outcome.summary)
    print(outcome.uncertainties)

    md = render_markdown(conclusion)
"""

from .reporter import (
    STAGE_NAME,
    STAGE_TITLE,
    ExplanationOutcome,
    Reporter,
    render_markdown,
    render_text,
)

__all__ = [
    "Reporter",
    "ExplanationOutcome",
    "render_markdown",
    "render_text",
    "STAGE_NAME",
    "STAGE_TITLE",
]
