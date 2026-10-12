"""能力② 提示模板：制度语义匹配与风险推理。

这是风险识别能力的核心阶段，也是提示词设计最讲究的地方。要点：

- **先给依据再下判断**：要求模型先说明"依据哪条制度/哪处材料"，再描述疑点，
  顺序反过来会显著增加凭空生成。
- **显式枚举风险类别**：缺失 / 冲突 / 偏离 / 越权 / 异常组合——对应课题五元框架的
  "风险"一元，枚举能有效抑制类别漂移。
- **强制列出反向证据**：要求模型主动写出"支持本事项合规的信息"，这对降低误报、
  提高审核采纳度很关键。
"""

from __future__ import annotations

from typing import Any

from .common import GOVERNANCE_GUARDRAIL, JSON_OUTPUT_RULE, SYSTEM_PREAMBLE

REASONING_SYSTEM = f"""{SYSTEM_PREAMBLE}

【本阶段任务】
将业务材料与制度条款、校内口径进行关联，识别待核验的风险线索。

【风险类别（category 只能取以下五个值）】
- missing    缺失：应当具备的材料、字段、审批环节缺失
- conflict   冲突：不同材料之间的信息相互矛盾（金额、日期、主体、事由等）
- deviation  偏离：超出预算额度、超出开支标准、用途与科目不符、超出办理时限
- override   越权：超出审批权限、审批程序缺失或时序倒置、化整为零规避审批
- anomaly    异常组合：单笔看正常但组合起来异常（同一供应商短期内多笔、
              金额高度接近审批阈值、周末/节假日办理、频繁小额累加等）

【风险等级（level 只能取以下四个值）】
- high     有明确材料证据指向，且可能造成资金损失或廉政风险
- medium   存在疑点但需补充材料或人工确认后才能定性
- low      属于规范性提醒，不影响资金安全
- unknown  证据不足，无法判断——这是合法且常见的取值，不要回避

{GOVERNANCE_GUARDRAIL}

【推理要求】
- 先定位依据（制度条款或材料原文），再描述疑点，最后给出建议复核点。
- 每条线索必须写明「为什么这构成疑点」的推理链条（rationale），
  不能只给结论性短句。
- 必须填写 counter_evidence：列出支持本事项可能合规的信息，或说明你在哪些
  信息上不确定。若确实没有反向证据，写"未发现支持性材料"。
- 制度依据来自下方提供的「制度依据」区块时，请引用其编号与条款号；
  若你认为需要引用未提供的条款，请把该条款放入 matched_clauses 并在
  rationale 中说明"该条款未在提供的制度依据中，需人工确认"。

{JSON_OUTPUT_RULE}

【输出 JSON 结构】
{{
  "findings": [
    {{
      "title": "线索标题，一句话说清疑点",
      "category": "missing|conflict|deviation|override|anomaly",
      "level": "high|medium|low|unknown",
      "event_type": "reimbursement|contract|budget|advance|procurement|transfer|settlement|other",
      "description": "疑点的客观描述，不含定性结论",
      "rationale": "推理链条：依据什么 → 发现什么 → 为什么构成疑点",
      "matched_clauses": [
        {{
          "doc_title": "制度文件名称",
          "doc_no": "文号",
          "article": "条款号",
          "text": "条款原文摘录（可截取关键句）"
        }}
      ],
      "evidence": [
        {{
          "material_name": "材料文件名",
          "locator": "定位信息，如 第2页 / 表格1第3行",
          "snippet": "原文摘录，20~60 字"
        }}
      ],
      "counter_evidence": ["支持本事项可能合规的信息，或你的不确定点"],
      "suggested_dispositions": ["supplement|return|restrict|consult|release|pending_review"],
      "suggestion": "建议复核点，写成审核人可以照着做的动作",
      "confidence": 0.75
    }}
  ],
  "summary": "本次审查的客观摘要，只陈述发现，不下合规判断",
  "notes": ["其他提醒，如 某类材料缺失导致某维度无法判断"]
}}

若确实未发现任何线索，返回 {{"findings": [], "summary": "未发现需人工介入的风险线索", "notes": []}}，
不要为了填充而编造线索。""".strip()


def build_reasoning_user(
    materials_text: str,
    *,
    event_type_label: str = "",
    extracted_summary: str = "",
    clause_block: str = "",
    rule_hits_block: str = "",
    institution_context: str = "",
) -> str:
    """构造推理阶段的用户提示。

    ``rule_hits_block`` 是规则引擎的命中结果——让模型在确定性结论之上做语义增强，
    而不是从零开始判断。这正是"规则+大模型"融合的关键：规则负责确定性，
    模型负责语义与情境。
    """
    parts: list[str] = []
    if event_type_label:
        parts.append(f"【事项类型】{event_type_label}")
    if institution_context:
        parts.append(f"【本单位管理口径】\n{institution_context}")
    if extracted_summary:
        parts.append(f"【已抽取的结构化要素】\n{extracted_summary}")
    if rule_hits_block:
        parts.append(
            "【规则引擎已命中的确定性项（请在此基础上补充语义层面的疑点，"
            "不要简单重复；如认为某条命中不成立，请说明理由）】\n" + rule_hits_block
        )
    if clause_block:
        parts.append(f"【制度依据（可引用的条款）】\n{clause_block}")
    parts.append("【待审查材料】")
    parts.append(materials_text)
    parts.append("请按系统提示约定的 JSON 结构输出风险线索。")
    return "\n\n".join(parts)


def reasoning_schema() -> dict[str, Any]:
    """推理阶段的 JSON Schema。"""
    finding = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "category": {"enum": ["missing", "conflict", "deviation", "override", "anomaly"]},
            "level": {"enum": ["high", "medium", "low", "unknown"]},
            "event_type": {"type": "string"},
            "description": {"type": "string"},
            "rationale": {"type": "string"},
            "matched_clauses": {"type": "array", "items": {"type": "object"}},
            "evidence": {"type": "array", "items": {"type": "object"}},
            "counter_evidence": {"type": "array", "items": {"type": "string"}},
            "suggested_dispositions": {"type": "array", "items": {"type": "string"}},
            "suggestion": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["title", "category", "level", "description"],
    }
    return {
        "type": "object",
        "properties": {
            "findings": {"type": "array", "items": finding},
            "summary": {"type": "string"},
            "notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["findings"],
    }


__all__ = ["REASONING_SYSTEM", "build_reasoning_user", "reasoning_schema"]
