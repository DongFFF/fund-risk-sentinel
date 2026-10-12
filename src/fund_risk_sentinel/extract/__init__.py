"""能力① 材料理解与结构化抽取。

.. code-block:: python

    from fund_risk_sentinel.extract import Extractor

    extractor = Extractor(client=client)
    outcome = extractor.extract(materials, event_type=EventType.REIMBURSEMENT)
    print(outcome.fields.get("amount"), outcome.fields.missing_attachments)
    print(outcome.stage.status, outcome.stage.duration_ms)
"""

from .extractor import STAGE_NAME, STAGE_TITLE, ExtractionOutcome, Extractor

__all__ = ["Extractor", "ExtractionOutcome", "STAGE_NAME", "STAGE_TITLE"]
