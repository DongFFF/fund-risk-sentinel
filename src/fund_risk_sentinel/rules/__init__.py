"""规则引擎与制度知识库——课题"四类适配路径"中的基线路径。

.. code-block:: python

    from fund_risk_sentinel.rules import RuleEngine, KnowledgeBase, build_facts
    from fund_risk_sentinel.rules.loader import load_all_rules
    from fund_risk_sentinel.rules.knowledge_base import load_knowledge_base

    engine = RuleEngine(load_all_rules("config/rules"))
    facts = build_facts(extracted, materials, event_type=EventType.REIMBURSEMENT)
    hits = engine.evaluate(facts)

    kb = load_knowledge_base("config/knowledge")
    refs = kb.search("差旅费 报销 标准 超标", top_k=6)
"""

from .engine import (
    RuleEngine,
    RuleEngineStats,
    build_facts,
    rule_from_dict,
    safe_eval,
    validate_condition,
)
from .knowledge_base import Clause, KnowledgeBase, load_knowledge_base, tokenize
from .loader import (
    load_all_rules,
    load_rules_from_dir,
    load_rules_from_dirs,
    load_rules_from_file,
    resolve_rule_dirs,
    save_rules_to_file,
)

__all__ = [
    # engine
    "RuleEngine",
    "RuleEngineStats",
    "build_facts",
    "rule_from_dict",
    "safe_eval",
    "validate_condition",
    # knowledge base
    "Clause",
    "KnowledgeBase",
    "load_knowledge_base",
    "tokenize",
    # loader
    "load_all_rules",
    "load_rules_from_file",
    "load_rules_from_dir",
    "load_rules_from_dirs",
    "save_rules_to_file",
    "resolve_rule_dirs",
]
