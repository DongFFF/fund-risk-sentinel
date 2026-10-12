"""能力③ 提示模板：结果解释与证据定位。

可解释性直接决定审核人员对模型结果的采纳程度。这一阶段的提示词要求模型
把已有线索"翻译"成审核人可以直接使用的形式：结论摘要、分级处置建议、
以及每条线索的可追溯说明。同时强制它指出"哪些结论依赖了不确定信息"——
把模型的置信度暴露给使用者，而不是藏起来。
"""

from __future__ import annotations

from .common import GOVERNANCE_GUARDRAIL, JSON_OUTPUT_RULE, SYSTEM_PREAMBLE

EXPLANATION_SYSTEM = f"""{SYSTEM_PREAMBLE}

【本阶段任务】
把已经形成的风险线索整理为面向审核人员的解释性输出。

【解释要求】
1. summary：客观摘要。说明本次审查覆盖了哪些材料、形成了几条线索、分别集中在
   哪些方面。只陈述发现，不下合规判断。
2. 逐条解释：对每条线索，用审核人能理解的语言说明
   「制度怎么要求的 → 材料显示什么 → 差异在哪里 → 建议核对什么」。
3. 可追溯：明确写出该线索依赖的材料位置与制度条款。若某条线索缺少制度依据、
   仅凭经验判断，必须显式标注"该线索暂无明确制度依据，属经验性提示"。
4. 不确定信息单列：把"因为材料缺失/表述模糊而无法确认"的部分集中列出，
   供审核人优先补齐材料。
5. 处置建议要分层次：哪些必须办理前处理、哪些可以办理中补充、哪些仅需留痕。

{GOVERNANCE_GUARDRAIL}

{JSON_OUTPUT_RULE}

【输出 JSON 结构】
{{
  "summary": "整体摘要，150~300 字",
  "finding_explanations": [
    {{
      "title": "与线索标题一致",
      "plain_explanation": "用审核人语言解释这条线索，100~200 字",
      "traceability": "可追溯性说明：依据的材料位置与条款",
      "basis_quality": "explicit|experience|insufficient",
      "suggested_action": "建议的复核或处置动作"
    }}
  ],
  "uncertainties": ["因材料缺失或表述模糊而无法确认的事项"],
  "processing_levels": {{
    "before_processing": ["办理前必须处理的线索标题"],
    "during_processing": ["办理中可补充材料的线索标题"],
    "for_record": ["仅需留痕的线索标题"]
  }},
  "recommended_actions": ["给审核人的总体行动建议，按优先级排序"],
  "notes": ["其他提醒"]
}}""".strip()


def build_explanation_user(
    findings_block: str,
    *,
    material_manifest: str = "",
    statistics: str = "",
    degraded_note: str = "",
) -> str:
    """构造解释阶段的用户提示。

    ``degraded_note`` 用于告知模型"本次部分结果来自降级路径"（例如模型不可用、
    走的是规则引擎），使其在摘要中如实反映能力边界。
    """
    parts: list[str] = []
    if material_manifest:
        parts.append(f"【材料清单】\n{material_manifest}")
    if statistics:
        parts.append(f"【线索统计】\n{statistics}")
    if degraded_note:
        parts.append(f"【能力边界说明】\n{degraded_note}")
    parts.append("【待解释的风险线索】")
    parts.append(findings_block)
    parts.append("请按系统提示约定的 JSON 结构输出解释性结果。")
    return "\n\n".join(parts)


def explanation_schema() -> dict:
    """解释阶段的 JSON Schema。"""
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "finding_explanations": {"type": "array", "items": {"type": "object"}},
            "uncertainties": {"type": "array", "items": {"type": "string"}},
            "processing_levels": {"type": "object"},
            "recommended_actions": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary"],
    }


__all__ = ["EXPLANATION_SYSTEM", "build_explanation_user", "explanation_schema"]
