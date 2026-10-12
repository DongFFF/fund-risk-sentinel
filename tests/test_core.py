"""核心层：枚举、领域模型、文本口径。"""

from __future__ import annotations

import pytest

from fund_risk_sentinel.core.enums import (
    RISK_LEVEL_WEIGHT,
    EventType,
    HumanVerdict,
    RiskCategory,
    RiskLevel,
    StrEnum,
)
from fund_risk_sentinel.core.models import (
    Evidence,
    ExtractedField,
    ExtractedFields,
    Material,
    ReviewConclusion,
    RiskFinding,
)
from fund_risk_sentinel.core.text_patterns import (
    extract_fields,
    parse_date,
    to_number,
)


class TestStrEnum:
    def test_values_returns_all(self) -> None:
        assert "reimbursement" in EventType.values()

    def test_from_value_accepts_value_name_and_label(self) -> None:
        assert EventType.from_value("reimbursement") is EventType.REIMBURSEMENT
        assert EventType.from_value("REIMBURSEMENT") is EventType.REIMBURSEMENT
        assert EventType.from_value("支出报销") is EventType.REIMBURSEMENT

    def test_from_value_falls_back_to_default(self) -> None:
        assert EventType.from_value("不存在", EventType.OTHER) is EventType.OTHER
        assert EventType.from_value(None) is None

    def test_str_enum_is_str(self) -> None:
        assert isinstance(EventType.OTHER, str)
        assert f"{EventType.OTHER}" == "other"


class TestRiskLevel:
    def test_weight_is_monotonic(self) -> None:
        assert RISK_LEVEL_WEIGHT[RiskLevel.LOW] < RISK_LEVEL_WEIGHT[RiskLevel.MEDIUM]
        assert RISK_LEVEL_WEIGHT[RiskLevel.MEDIUM] < RISK_LEVEL_WEIGHT[RiskLevel.HIGH]


class TestRiskFinding:
    def test_defaults_to_needs_human_review(self) -> None:
        """治理边界：线索默认必须经人工复核。"""
        finding = RiskFinding(
            title="测试线索",
            category=RiskCategory.MISSING,
            level=RiskLevel.MEDIUM,
            rationale="因为……",
        )
        assert finding.needs_human_review is True

    def test_auto_id_prefix(self) -> None:
        finding = RiskFinding(
            title="t", category=RiskCategory.MISSING, level=RiskLevel.LOW, rationale="r"
        )
        assert finding.finding_id.startswith("risk")


class TestReviewConclusion:
    def test_recount_computes_statistics_and_overall(self) -> None:
        conclusion = ReviewConclusion()
        conclusion.add_finding(
            RiskFinding(title="a", category=RiskCategory.MISSING, level=RiskLevel.HIGH, rationale="r")
        )
        conclusion.add_finding(
            RiskFinding(title="b", category=RiskCategory.CONFLICT, level=RiskLevel.LOW, rationale="r")
        )
        conclusion.recount()
        assert conclusion.statistics["high"] == 1
        assert conclusion.statistics["low"] == 1
        assert conclusion.overall_level is RiskLevel.HIGH

    def test_disclaimer_present_by_default(self) -> None:
        assert ReviewConclusion().disclaimer.strip()


class TestTextPatterns:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1,234.56", 1234.56),
            ("￥8,660.00", 8660.0),
            ("人民币 320000", 320000.0),
            ("3,800.00", 3800.0),
        ],
    )
    def test_to_number(self, raw: str, expected: float) -> None:
        assert to_number(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", ["", "不是数字", None])
    def test_to_number_returns_none_when_unparseable(self, raw) -> None:
        """解析失败返回 None，让调用方自己决定兜底值，而不是悄悄当成 0。"""
        assert to_number(raw) is None

    def test_parse_date_returns_date_object(self) -> None:
        import datetime

        assert parse_date("2026年3月5日") == datetime.date(2026, 3, 5)
        assert parse_date("2026/03/05") == datetime.date(2026, 3, 5)

    def test_parse_date_returns_none_for_garbage(self) -> None:
        assert parse_date("没有日期") is None

    def test_extract_fields_pulls_known_keys(self) -> None:
        text = (
            "项目名称：面向智能制造的多源数据融合关键技术研究\n"
            "项目编号：2026ZD-001\n"
            "报销人：张伟\n"
            "审批人：李强\n"
            "预算余额：3,800.00\n"
        )
        fields = extract_fields(text)
        assert fields["project_code"]["value"] == "2026ZD-001"
        assert fields["applicant"]["value"] == "张伟"
        assert fields["approver"]["value"] == "李强"
        # 预算类字段优先取"余额"，因为规则关心的是"还能花多少"
        assert to_number(fields["budget_amount"]["value"]) == pytest.approx(3800.0)

    def test_extract_fields_normalizes_amount_to_two_decimals(self) -> None:
        fields = extract_fields("预算余额：3800")
        assert fields["budget_amount"]["value"] == "3800.00"


class TestMaterial:
    def test_char_count_reflects_text(self) -> None:
        material = Material(filename="a.txt", role="application", text="12345")
        assert material.char_count == 5

    def test_extracted_fields_container(self) -> None:
        ext = ExtractedFields(fields={"amount": ExtractedField(name="amount", value="100")})
        assert ext.fields["amount"].value == "100"


class TestEvidence:
    def test_locator_optional(self) -> None:
        evidence = Evidence(material_name="发票.pdf")
        assert evidence.material_name == "发票.pdf"

    def test_human_verdict_enum(self) -> None:
        assert HumanVerdict.from_value("accepted") is HumanVerdict.ACCEPTED
