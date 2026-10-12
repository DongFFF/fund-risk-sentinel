"""规则层：受限表达式沙箱、事实构建、规则引擎与知识库检索。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fund_risk_sentinel.core.enums import DocumentRole, EventType, RiskLevel
from fund_risk_sentinel.core.exceptions import RuleSyntaxError
from fund_risk_sentinel.core.models import (
    ExtractedField,
    ExtractedFields,
    Material,
)
from fund_risk_sentinel.rules.engine import (
    RuleEngine,
    build_facts,
    safe_eval,
    validate_condition,
)
from fund_risk_sentinel.rules.knowledge_base import KnowledgeBase, tokenize
from fund_risk_sentinel.rules.loader import load_all_rules


class TestSafeEval:
    def test_arithmetic_and_comparison(self) -> None:
        assert safe_eval("amount > 1000 and amount <= 5000", {"amount": 3000}) is True

    def test_allowed_helper_functions(self) -> None:
        facts = {"amount": 0, "text": "无发票", "purpose": ""}
        assert safe_eval("is_empty(purpose)", facts) is True
        assert safe_eval("not is_empty(text)", facts) is True
        assert safe_eval('contains_any(text, "发票", "收据")', facts) is True
        assert safe_eval("to_number('1,234') == 1234", facts) is True

    def test_fields_get_is_allowed(self) -> None:
        facts = {"fields": {"amount_all": "9,999"}}
        assert safe_eval('to_number(fields.get("amount_all","")) == 9999', facts) is True

    @pytest.mark.parametrize(
        "expr",
        [
            "__import__('os').system('echo hi')",
            "().__class__.__bases__",
            "open('/etc/passwd')",
            "[x for x in range(3)]",
            "lambda: 1",
            "amount.__class__",
        ],
    )
    def test_dangerous_expressions_rejected(self, expr: str) -> None:
        with pytest.raises(RuleSyntaxError):
            safe_eval(expr, {"amount": 1})

    def test_syntax_error_raises(self) -> None:
        with pytest.raises(RuleSyntaxError):
            safe_eval("amount >", {"amount": 1})

    def test_empty_expression_is_false(self) -> None:
        assert safe_eval("   ", {}) is False

    def test_validate_condition_uses_full_probe(self) -> None:
        """合法规则不应因探针变量缺失被误判。"""
        assert validate_condition("not is_empty(applicant) and applicant == approver") == []
        assert validate_condition("amount > 0") == []


class TestBuildFacts:
    def _extracted(self) -> ExtractedFields:
        return ExtractedFields(
            fields={
                "amount": ExtractedField(name="amount", value="9,560"),
                "budget_amount": ExtractedField(name="budget_amount", value="3,800"),
                "contract_amount": ExtractedField(name="contract_amount", value="8,660"),
                "applicant": ExtractedField(name="applicant", value="张伟"),
                "approver": ExtractedField(name="approver", value="张伟"),
                "purpose": ExtractedField(name="purpose", value="差旅费"),
            }
        )

    def test_core_numbers(self) -> None:
        facts = build_facts(self._extracted(), [], event_type=EventType.REIMBURSEMENT)
        assert facts["amount"] == pytest.approx(9560.0)
        assert facts["budget_amount"] == pytest.approx(3800.0)
        assert facts["contract_amount"] == pytest.approx(8660.0)
        assert facts["event_type"] == "reimbursement"

    def test_applicant_and_approver_exposed(self) -> None:
        """ATH-002 依赖这两个变量，二者必须同时可用。"""
        facts = build_facts(self._extracted())
        assert facts["applicant"] == "张伟"
        assert facts["approver"] == "张伟"
        assert safe_eval("applicant == approver", facts) is True

    def test_attachment_flags_from_roles(self) -> None:
        materials = [
            Material(filename="发票.pdf", role=DocumentRole.INVOICE, text="发票"),
            Material(filename="合同.pdf", role=DocumentRole.CONTRACT, text="合同"),
        ]
        facts = build_facts(self._extracted(), materials)
        assert facts["has_invoice"] is True
        assert facts["has_contract"] is True
        assert facts["has_acceptance"] is False
        assert facts["material_count"] == 2

    def test_days_after_event_derived_from_dates(self) -> None:
        ext = ExtractedFields(
            fields={
                "event_date": ExtractedField(name="event_date", value="2026-03-01"),
                "last_date": ExtractedField(name="last_date", value="2026-03-21"),
            }
        )
        facts = build_facts(ext)
        assert facts["days_after_event"] == 20


class TestRuleEngine:
    @pytest.fixture()
    def engine(self) -> RuleEngine:
        rules = load_all_rules(Path(__file__).resolve().parents[1] / "config" / "rules")
        engine = RuleEngine()
        engine.add_rules(rules)
        return engine

    def test_builtin_rules_load(self, engine: RuleEngine) -> None:
        assert engine.describe()["total"] >= 30

    def test_all_builtin_rules_validate(self, engine: RuleEngine) -> None:
        assert engine.validate() == []

    def test_amount_over_budget_hits_deviation(self, engine: RuleEngine) -> None:
        facts = build_facts(
            ExtractedFields(
                fields={
                    "amount": ExtractedField(name="amount", value="9560"),
                    "budget_amount": ExtractedField(name="budget_amount", value="3800"),
                }
            ),
            [],
            event_type=EventType.REIMBURSEMENT,
        )
        hits = engine.evaluate(facts)
        assert hits, "超预算应当命中至少一条规则"
        assert any(hit.dimension.value == "amount" for hit in hits)

    def test_same_applicant_and_approver_hits_authority_rule(self, engine: RuleEngine) -> None:
        facts = build_facts(
            ExtractedFields(
                fields={
                    "applicant": ExtractedField(name="applicant", value="张伟"),
                    "approver": ExtractedField(name="approver", value="张伟"),
                }
            ),
            [],
            event_type=EventType.REIMBURSEMENT,
        )
        hits = engine.evaluate(facts)
        assert any(hit.rule_id == "ATH-002" for hit in hits)

    def test_no_hit_on_clean_facts(self, engine: RuleEngine) -> None:
        facts = build_facts(
            ExtractedFields(
                fields={
                    "amount": ExtractedField(name="amount", value="1000"),
                    "budget_amount": ExtractedField(name="budget_amount", value="50000"),
                    "applicant": ExtractedField(name="applicant", value="张伟"),
                    "approver": ExtractedField(name="approver", value="李强"),
                }
            ),
            [
                Material(filename="发票.pdf", role=DocumentRole.INVOICE, text="发票"),
                Material(filename="合同.pdf", role=DocumentRole.CONTRACT, text="合同"),
            ],
            event_type=EventType.REIMBURSEMENT,
        )
        hits = engine.evaluate(facts)
        # 干净的报销仍可能因"缺验收单"等触发，但不应触发越权与超预算
        assert not any(hit.rule_id == "ATH-002" for hit in hits)
        assert not any(hit.dimension.value == "amount" for hit in hits)

    def test_stats_reflect_enabled_count(self, engine: RuleEngine) -> None:
        stats = engine.describe()
        assert stats["enabled"] == stats["total"]


class TestKnowledgeBase:
    def test_tokenize_produces_bigrams(self) -> None:
        tokens = tokenize("科研经费")
        assert tokens
        assert all(isinstance(t, str) for t in tokens)

    def _write_kb(self, tmp_path: Path) -> Path:
        import yaml

        path = tmp_path / "kb.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "doc_title": "测试制度汇编",
                    "clauses": [
                        {
                            "article": "第十二条",
                            "text": "差旅费应当按照规定标准据实报销，不得超标准列支。",
                        },
                        {
                            "article": "第十三条",
                            "text": "单台设备金额超过五十万元的应当履行政府采购程序。",
                        },
                    ],
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        return path

    def test_search_returns_relevant_clause(self, tmp_path: Path) -> None:
        kb = KnowledgeBase()
        kb.add_from_yaml(self._write_kb(tmp_path))
        results = kb.search("差旅费超标准报销", top_k=2)
        assert results
        assert "差旅" in results[0].text

    def test_empty_query_returns_empty(self, tmp_path: Path) -> None:
        kb = KnowledgeBase()
        kb.add_from_yaml(self._write_kb(tmp_path))
        assert kb.search("") == []

    def test_len_and_stats(self, tmp_path: Path) -> None:
        kb = KnowledgeBase()
        kb.add_from_yaml(self._write_kb(tmp_path))
        assert len(kb) == 2
        assert kb.stats()["clause_count"] == 2

    def test_rule_ids_keep_authored_values(self) -> None:
        """回归：规则 ID 应保留 YAML 中作者写的值，而非文件名派生的编号。"""
        rules = load_all_rules(Path(__file__).resolve().parents[1] / "config" / "rules")
        ids = {r.rule_id for r in rules}
        assert "ATH-002" in ids
        assert "AMT-001" in ids
        assert not any(i.startswith("05-") for i in ids)

    def test_load_from_project_corpus(self) -> None:
        from fund_risk_sentinel.rules.knowledge_base import load_knowledge_base

        kb = load_knowledge_base(Path(__file__).resolve().parents[1] / "config" / "knowledge")
        assert len(kb) >= 20
