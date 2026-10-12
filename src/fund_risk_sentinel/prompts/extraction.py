"""能力① 提示模板：材料理解与结构化抽取。

这一阶段的判定标准相对客观（字段抽取的准确率与召回率、附件齐全性判断的一致率），
所以提示词的重点是"字段口径统一"和"不许臆测"：抽不到就留空并说明，而不是
凭常识补一个看起来合理的值——后者会污染下游全部推理。
"""

from __future__ import annotations

from typing import Any

from .common import GOVERNANCE_GUARDRAIL, JSON_OUTPUT_RULE, SYSTEM_PREAMBLE

EXTRACTION_SYSTEM = f"""{SYSTEM_PREAMBLE}

【本阶段任务】
从经费业务材料中抽取结构化关键要素，并判断附件齐全性与字段一致性。
这是后续风险推理的基础，因此宁可留空也不要臆测。

{GOVERNANCE_GUARDRAIL}

【抽取字段口径】
- project_name      项目名称（不含项目编号）
- project_code      项目编号 / 经费卡号
- budget_subject    经费科目 / 预算科目（如"差旅费""材料费""劳务费"）
- amount            本笔业务金额，纯数字字符串，不带千分位与货币符号
- budget_amount     对应预算科目的额度或余额，纯数字
- contract_amount   合同金额，纯数字
- event_date        业务发生日期，YYYY-MM-DD
- supplier          供应商 / 收款方 / 开票方全称
- applicant         申请人 / 报销人 / 经办人
- approver          审批人 / 审核人
- approver_level    审批层级（如"学院""科研处""分管校领导"）
- invoice_no        发票号码
- contract_no       合同编号
- purpose           事由 / 用途 / 开支内容
- 其他你认为对风险识别重要的字段，可自行增加，字段名用英文小写下划线

【字段置信度 confidence 取值参考】
0.9~1.0 材料中明确标注、格式规整；0.7~0.89 需从上下文推断但依据充分；
0.5~0.69 表述模糊或多处冲突；<0.5 基本靠猜测（此时建议直接留空）。

{JSON_OUTPUT_RULE}

【输出 JSON 结构】
{{
  "fields": {{
    "字段名": {{
      "value": "规范化后的值",
      "raw": "材料中的原始表述",
      "confidence": 0.85,
      "source_material": "该字段来自的材料文件名",
      "locator": "定位信息，如 第2页 / 表格1第3行 / 字符位置"
    }}
  }},
  "attachments_expected": ["按制度要求应当具备的附件名"],
  "attachments_present": ["材料中实际检出的附件名"],
  "consistency_issues": ["字段间不一致的客观描述，如 合同金额与报销金额相差 2000 元"],
  "notes": ["其他需要提醒审核人的信息，如 某份材料未识别出文字"]
}}""".strip()


def build_extraction_user(
    materials_text: str,
    *,
    event_type_label: str = "",
    expected_attachments: list[str] | None = None,
    rule_hints: list[str] | None = None,
) -> str:
    """构造抽取阶段的用户提示。

    ``rule_hints`` 用于把规则库的字段口径提示给模型（例如"差旅费需附行程单"），
    使模型抽取与规则校验关注同一批字段，减少口径错位。
    """
    parts: list[str] = []
    if event_type_label:
        parts.append(f"【事项类型】{event_type_label}")
    if expected_attachments:
        parts.append("【本事项通常应具备的附件】" + "、".join(expected_attachments))
    if rule_hints:
        parts.append("【本单位规则库提示】\n" + "\n".join(f"- {h}" for h in rule_hints))
    parts.append("【待抽取材料】")
    parts.append(materials_text)
    parts.append("请按系统提示约定的 JSON 结构输出抽取结果。")
    return "\n\n".join(parts)


def extraction_schema() -> dict[str, Any]:
    """抽取阶段的 JSON Schema，供支持 json_schema 的服务端做结构化约束。"""
    return {
        "type": "object",
        "properties": {
            "fields": {"type": "object"},
            "attachments_expected": {"type": "array", "items": {"type": "string"}},
            "attachments_present": {"type": "array", "items": {"type": "string"}},
            "consistency_issues": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["fields"],
    }


__all__ = ["EXTRACTION_SYSTEM", "build_extraction_user", "extraction_schema"]
