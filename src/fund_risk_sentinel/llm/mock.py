"""离线模拟模型（mock provider）。

作用不是"假数据凑数"，而是承担三个真实职责：

1. **零配置可运行**：首次打开程序、还没配 API Key 时，用户能立刻跑通全流程看到效果。
2. **降级兜底**：真实模型不可用（断网、欠费、限流）时，规则+启发式仍能给出可用线索，
   结论中会明确标注 ``degraded_stages``，不会假装是模型输出。
3. **确定性基线**：输出完全由输入决定、不含随机数，因此适合做单元测试，
   也适合作为评测体系里"规则基线"的对照组。

实现方式：正则抽取关键字段 + 关键词/数值启发式判断风险线索，输出结构与真实模型
约定的 JSON 契约完全一致。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ..core.text_patterns import (
    AMOUNT_PATTERN,
    DEFAULT_EXPECTED_ATTACHMENTS,
    context_snippet,
    extract_fields,
    find_attachments,
    to_number,
)
from .base import BaseLLMProvider, LLMRequest, LLMResponse

# ---------------------------------------------------------------- 风险启发式

#: (线索标题, 风险类别, 等级, 触发正则, 客观描述, 建议复核点, 处置建议)
_HEURISTICS: list[tuple[str, str, str, str, str, str, list[str]]] = [
    (
        "缺少合同或合同编号",
        "missing",
        "medium",
        r"(未附合同|无合同|合同缺失|未见合同|未签订合同)",
        "材料中未见合同或合同编号，无法核对采购或服务事项的约定条件。",
        "请补充合同文本或说明无需签订合同的依据。",
        ["supplement"],
    ),
    (
        "缺少发票或票据",
        "missing",
        "high",
        r"(未附发票|无发票|发票缺失|未见发票|无票据|无票据凭证)",
        "材料中未见发票或票据凭证，支出真实性无法核对。",
        "请补充发票原件影像或票据查验结果。",
        ["supplement", "return"],
    ),
    (
        "缺少验收环节材料",
        "missing",
        "medium",
        r"(未验收|无验收|验收单缺失|未见验收)",
        "材料中未见验收单或验收记录，无法确认事项已完成。",
        "请补充验收单或成果交付确认材料。",
        ["supplement"],
    ),
    (
        "支出超出预算科目余额",
        "deviation",
        "high",
        r"(超预算|超出预算|预算不足|预算余额不足|超支)",
        "本笔支出金额可能超出对应预算科目余额。",
        "请核对预算科目余额，确认是否需要办理预算调整。",
        ["restrict", "consult"],
    ),
    (
        "开支内容与预算科目不符",
        "deviation",
        "medium",
        r"(科目不符|列支科目有误|不属于该科目|科目不匹配|开支内容与预算不符)",
        "开支内容与所列预算科目的适用范围可能不一致。",
        "请核对科目适用范围，确认列支科目是否正确。",
        ["return", "consult"],
    ),
    (
        "审批程序不完整或倒置",
        "override",
        "high",
        r"(先支后批|未审批|无审批|越权审批|审批缺失|未经批准|先报销后审批)",
        "审批环节可能缺失或时序倒置，程序合规性无法确认。",
        "请补充审批记录，并说明程序倒置的原因。",
        ["return", "consult"],
    ),
    (
        "超出规定时限办理",
        "deviation",
        "medium",
        r"(超期|逾期报销|超过时限|跨年报销|超过报销期限)",
        "业务发生与办理时间间隔可能超出制度规定的时限。",
        "请核对制度时限要求，说明超期原因。",
        ["consult"],
    ),
    (
        "同一供应商短期内多笔支出",
        "anomaly",
        "medium",
        r"(同一供应商多次|短期内多笔|拆分报销|化整为零)",
        "存在同一供应商或同类事项短期内多笔支出的情形，需关注是否规避审批权限。",
        "请核查是否存在化整为零以规避审批额度的情况。",
        ["consult"],
    ),
    (
        "关联方或异常供应商",
        "anomaly",
        "medium",
        r"(关联方|关联企业|疑似关联|供应商与负责人同名)",
        "供应商可能与项目组成员存在关联关系。",
        "请核实供应商与项目组成员的关系，确认是否需回避。",
        ["consult"],
    ),
    (
        "劳务费发放对象异常",
        "anomaly",
        "high",
        r"(虚报人员|冒领|名单与实际不符|劳务费发放对象存疑|虚假名单)",
        "劳务费发放对象信息存疑，可能存在虚报冒领风险。",
        "请核对发放名单与身份信息、签收记录。",
        ["restrict", "consult"],
    ),
]

_CATEGORY_TO_LEVEL_DEFAULT = {"missing": "medium", "conflict": "high", "deviation": "medium",
                              "override": "high", "anomaly": "medium"}


class MockProvider(BaseLLMProvider):
    """确定性离线模型。"""

    provider_type = "mock"

    def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        task = str(request.metadata.get("task") or "generic")
        user_text = "\n".join(m.content for m in request.messages if m.role == "user")
        prompt_chars = request.prompt_chars

        if task == "extract":
            payload = self._mock_extract(user_text)
        elif task == "reason":
            payload = self._mock_reason(user_text)
        elif task == "explain":
            payload = self._mock_explain(user_text)
        else:
            payload = {
                "answer": "离线模拟模型已收到请求。配置真实模型后可得更高质量结果。",
                "task": task,
            }

        text = json.dumps(payload, ensure_ascii=False, indent=2)
        return LLMResponse(
            text=text,
            parsed=payload,
            provider=self.name,
            model=self.config.model or "mock-rules-v1",
            # 粗略估算：中文约 1 字符 ≈ 1 token，便于界面展示成本量级
            prompt_tokens=prompt_chars,
            completion_tokens=len(text),
            finish_reason="stop",
            latency_ms=self._elapsed_ms(started),
            attempts=1,
            raw=None,
        )

    # ---------------------------------------------------------- 抽取

    def _mock_extract(self, text: str) -> dict[str, Any]:
        fields = extract_fields(text)
        found, missing = find_attachments(text)
        expected = list(DEFAULT_EXPECTED_ATTACHMENTS["reimbursement"])

        issues: list[str] = []
        if "amount_all" in fields:
            issues.append(
                f"材料中出现多个不同金额（{fields['amount_all']['value']}），需核对是否一致。"
            )
        for name in expected:
            if name not in found:
                issues.append(f"材料中未检出：{name}。")

        return {
            "fields": fields,
            "attachments_expected": expected,
            "attachments_present": [a for a in found if a in expected] or found,
            "consistency_issues": issues,
            "notes": [
                "本结果由离线模拟模型（规则+正则）生成，仅用于流程验证与降级兜底。",
                f"输入文本长度 {len(text)} 字符。",
            ],
        }

    # ---------------------------------------------------------- 风险推理

    def _mock_reason(self, text: str) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []

        for title, category, level, pattern, desc, suggestion, dispositions in _HEURISTICS:
            match = re.search(pattern, text)
            if not match:
                continue
            findings.append(
                {
                    "title": title,
                    "category": category,
                    "level": level,
                    "description": f"{desc}（触发表述：{match.group(0)}）",
                    "rationale": (
                        f"材料中出现「{match.group(0)}」的表述，"
                        "对应科研经费管理制度对材料齐全性、程序合规性的要求，构成待核验线索。"
                    ),
                    "matched_clauses": [],
                    "evidence": [
                        {
                            "material_name": "（见材料正文）",
                            "locator": f"字符位置 {match.start()}",
                            "snippet": context_snippet(text, match.start(), match.end()),
                        }
                    ],
                    "dispositions": dispositions,
                    "suggestion": suggestion,
                    "confidence": 0.6,
                }
            )

        # 金额一致性：两处金额不同 → 冲突
        amounts = [m.group(1).replace(",", "") for m in AMOUNT_PATTERN.finditer(text)]
        if len(set(amounts)) > 1:
            findings.append(
                {
                    "title": "不同材料间金额不一致",
                    "category": "conflict",
                    "level": "high",
                    "description": f"材料中出现多个不同金额：{'、'.join(sorted(set(amounts)))}。",
                    "rationale": "同一事项的不同材料金额应当一致，金额差异可能源于重复报销、票据不符或录入错误。",
                    "matched_clauses": [],
                    "evidence": [
                        {
                            "material_name": "（见材料正文）",
                            "locator": "多处金额字段",
                            "snippet": "、".join(sorted(set(amounts))),
                        }
                    ],
                    "dispositions": ["return", "consult"],
                    "suggestion": "请逐项核对发票、报销单与合同金额，确认差异原因。",
                    "confidence": 0.65,
                }
            )

        # 金额与预算比对
        budget_match = re.search(r"预算(?:金额|额度|余额)[：:\s]*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text)
        if amounts and budget_match:
            spend = to_number(amounts[0])
            budget = to_number(budget_match.group(1))
            if spend is not None and budget is not None and spend > budget:
                findings.append(
                    {
                        "title": "支出金额超出预算额度",
                        "category": "deviation",
                        "level": "high",
                        "description": (
                            f"本笔支出 {spend:,.2f} 元，预算额度 {budget:,.2f} 元，"
                            f"超出 {spend - budget:,.2f} 元。"
                        ),
                        "rationale": "科研经费管理要求支出控制在预算额度内，超出部分需办理预算调整或说明来源。",
                        "matched_clauses": [],
                        "evidence": [
                            {
                                "material_name": "（见材料正文）",
                                "locator": "金额与预算字段",
                                "snippet": f"支出 {spend:,.2f} / 预算 {budget:,.2f}",
                            }
                        ],
                        "dispositions": ["restrict", "consult"],
                        "suggestion": "请核对预算科目余额，确认是否需要办理预算调整。",
                        "confidence": 0.7,
                    }
                )

        return {
            "findings": findings,
            "notes": [
                "本结果由离线模拟模型基于关键词与数值启发式生成，覆盖范围有限，"
                "配置真实模型后可获得更强的语义推理能力。"
            ],
        }

    # ---------------------------------------------------------- 解释生成

    def _mock_explain(self, text: str) -> dict[str, Any]:
        """离线生成解释。

        线索统计直接从提示里的【线索统计】行解析——那行由 ``_format_statistics``
        统一产出（``总数 N；按等级 high:3，medium:7；…``），比在正文里数关键词可靠。
        """
        counts = {"high": 0, "medium": 0, "low": 0, "unknown": 0}
        total = 0

        stats_match = re.search(r"总数\s*(\d+)", text)
        if stats_match:
            total = int(stats_match.group(1))
        level_match = re.search(r"按等级\s*([^\n；;]+)", text)
        if level_match:
            for name, value in re.findall(r"([A-Za-z_]+)\s*[:：]\s*(\d+)", level_match.group(1)):
                if name in counts:
                    counts[name] = int(value)

        if total <= 0:
            # 兜底：数提示里的线索条目标题（``[线索 N]``）
            total = len(re.findall(r"\[线索\s*\d+\]", text))
        if not any(counts.values()) and total:
            counts["medium"] = total

        actions: list[str] = []
        if counts["high"]:
            actions.append("对高等级线索，建议在支出办理前退回或限制，并组织会商。")
        if counts["medium"]:
            actions.append("对中等级线索，建议要求补充材料后再行办理。")
        if counts["unknown"]:
            actions.append("对证据不足的线索，建议先补齐材料后重新审查。")
        if not total:
            actions.append("未发现需人工介入的线索，建议按正常流程办理并留痕。")

        return {
            "summary": (
                f"本次审查共形成 {total} 条待核验线索"
                f"（高 {counts['high']} / 中 {counts['medium']} / 低 {counts['low']}）。"
                "线索已附命中依据与关联材料位置，请复核后决定处置方式。"
            ),
            "recommended_actions": actions,
            "notes": ["摘要由离线模拟模型生成。"],
        }


__all__ = ["MockProvider"]
