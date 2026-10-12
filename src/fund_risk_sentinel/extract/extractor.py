"""能力① 实现：材料理解与结构化抽取。

三条路径同时跑，互为校验：

1. **规则路径**（``core.text_patterns``）：正则抽取关键字段。零依赖、零成本、
   确定性，是模型不可用时的底线能力。
2. **模型路径**（大模型）：处理正则覆盖不到的情形——事由的语义归类、
   审批层级的隐含表述、跨段落的信息拼接。
3. **交叉校验**：两条路径对同一字段给出不同值时，不静默取其一，而是写入
   ``consistency_issues``。字段级的分歧本身就是一条值得人工看一眼的线索。

合并策略：模型值优先（语义理解更强），规则值补齐空缺；两者都有且差异明显时
保留两者并标注分歧。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field as dc_field
from typing import Any

from ..config.settings import ReviewSettings
from ..core.enums import EventType, StageStatus
from ..core.exceptions import LLMError
from ..core.models import (
    ExtractedField,
    ExtractedFields,
    Material,
    PipelineStageRecord,
    utc_now,
)
from ..core.text_patterns import (
    DEFAULT_EXPECTED_ATTACHMENTS,
    extract_fields,
    find_attachments,
    to_number,
)
from ..llm.registry import LLMClient
from ..prompts.extraction import EXTRACTION_SYSTEM, build_extraction_user

logger = logging.getLogger(__name__)

STAGE_NAME = "extract"
STAGE_TITLE = "材料理解与结构化抽取"

#: 数值字段差异超过该比例即视为分歧
_NUMERIC_TOLERANCE = 0.01


@dataclass
class ExtractionOutcome:
    """抽取阶段的产出。"""

    fields: ExtractedFields
    stage: PipelineStageRecord
    warnings: list[str] = dc_field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return self.stage.status == StageStatus.DEGRADED


class Extractor:
    """材料结构化抽取器。"""

    def __init__(
        self,
        client: LLMClient | None = None,
        settings: ReviewSettings | None = None,
        *,
        enable_llm: bool | None = None,
    ) -> None:
        self.client = client
        self.settings = settings or ReviewSettings()
        self.enable_llm = self.settings.enable_llm_reasoning if enable_llm is None else enable_llm

    def extract(
        self,
        materials: list[Material],
        *,
        materials_text: str = "",
        event_type: EventType = EventType.OTHER,
        rule_hints: list[str] | None = None,
        provider: str | None = None,
    ) -> ExtractionOutcome:
        stage = PipelineStageRecord(stage=STAGE_NAME, title=STAGE_TITLE, started_at=utc_now())
        warnings: list[str] = []
        combined_text = materials_text or "\n".join(m.text for m in materials)

        # ---------------- 路径 1：规则抽取（永远执行）
        baseline = extract_fields(combined_text)
        expected = list(DEFAULT_EXPECTED_ATTACHMENTS.get(event_type.value, ("发票",)))
        found_attachments, _missing = find_attachments(combined_text)

        # ---------------- 路径 2：模型抽取（可选）
        llm_fields: dict[str, dict[str, Any]] = {}
        llm_expected: list[str] = []
        llm_present: list[str] = []
        llm_issues: list[str] = []
        llm_notes: list[str] = []
        provider_name = ""
        model_name = ""

        if self.enable_llm and self.client is not None:
            try:
                user_prompt = build_extraction_user(
                    combined_text,
                    event_type_label=_event_label(event_type),
                    expected_attachments=expected,
                    rule_hints=rule_hints,
                )
                response = self.client.chat_json(
                    EXTRACTION_SYSTEM,
                    user_prompt,
                    provider=provider,
                    temperature=0.0,
                    metadata={"task": "extract", "stage": STAGE_NAME},
                )
                payload = response.parsed if isinstance(response.parsed, dict) else {}
                llm_fields = _normalize_fields(payload.get("fields") or {})
                llm_expected = _as_str_list(payload.get("attachments_expected"))
                llm_present = _as_str_list(payload.get("attachments_present"))
                llm_issues = _as_str_list(payload.get("consistency_issues"))
                llm_notes = _as_str_list(payload.get("notes"))
                provider_name = response.provider
                model_name = response.model
                stage.status = StageStatus.DEGRADED if self.client.last_degraded else StageStatus.OK
                if self.client.last_degraded:
                    stage.message = "主模型不可用，已回退到备用模型"
                stage.duration_ms = response.latency_ms
                stage.prompt_tokens = response.prompt_tokens
                stage.completion_tokens = response.completion_tokens
                warnings.extend(response.warnings)
            except LLMError as exc:
                if not self.settings.degrade_on_llm_failure:
                    raise
                stage.status = StageStatus.DEGRADED
                stage.message = f"模型抽取不可用，已降级为规则抽取：{exc}"
                warnings.append(
                    "结构化抽取已降级为规则引擎（正则）结果，字段覆盖范围有限，"
                    "结论中已标注该降级信息。"
                )
                logger.warning("抽取阶段降级：%s", exc)
        else:
            stage.status = StageStatus.SKIPPED
            stage.message = "未启用大模型抽取，仅使用规则抽取"

        # ---------------- 路径 3：合并与交叉校验
        merged, conflicts = _merge_fields(baseline, llm_fields)
        expected_all = _dedupe(expected + llm_expected)
        present_all = _dedupe(llm_present + found_attachments)

        issues: list[str] = []
        issues.extend(llm_issues)
        for name in expected_all:
            if name not in present_all:
                issues.append(f"未见「{name}」相关材料或字段。")
        issues.extend(conflicts)
        # 规则侧已经发现的多金额问题也带上
        if "amount_all" in baseline:
            value = baseline["amount_all"]["value"]
            issues.append(f"材料中出现多个不同金额（{value}），需核对是否一致。")

        fields = ExtractedFields(
            fields={name: ExtractedField(name=name, **data) for name, data in merged.items()},
            attachments_expected=expected_all,
            attachments_present=present_all,
            consistency_issues=_dedupe(issues),
            notes=_dedupe(llm_notes),
            produced_by=_produced_by(baseline, llm_fields, stage.status),
            provider=provider_name,
            model=model_name,
        )
        stage.provider = provider_name
        stage.model = model_name
        return ExtractionOutcome(fields=fields, stage=stage, warnings=warnings)


# ================================================================ 辅助

def _event_label(event_type: EventType) -> str:
    from ..core.enums import EVENT_TYPE_LABELS

    return EVENT_TYPE_LABELS.get(event_type, event_type.value)


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _normalize_fields(raw: Any) -> dict[str, dict[str, Any]]:
    """把模型返回的字段结构规整成 ``{名: {value, raw, confidence, locator}}``。"""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for name, payload in raw.items():
        key = str(name).strip()
        if not key:
            continue
        if isinstance(payload, dict):
            value = payload.get("value")
            value_str = "" if value is None else str(value).strip()
            try:
                confidence = float(payload.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            out[key] = {
                "value": value_str,
                "raw": str(payload.get("raw") or value_str),
                "confidence": max(0.0, min(1.0, confidence)),
                "source_material_id": str(payload.get("source_material_id") or ""),
                "locator": str(payload.get("locator") or payload.get("source_material") or ""),
            }
        elif payload is not None:
            out[key] = {
                "value": str(payload).strip(),
                "raw": str(payload).strip(),
                "confidence": 0.6,
                "source_material_id": "",
                "locator": "模型直接给出",
            }
    return out


def _merge_fields(
    baseline: dict[str, dict[str, Any]],
    llm: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """合并两路抽取结果，返回 ``(合并结果, 分歧列表)``。"""
    merged: dict[str, dict[str, Any]] = {name: dict(data) for name, data in baseline.items()}
    conflicts: list[str] = []

    for name, data in llm.items():
        rule_data = merged.get(name)
        if rule_data is None:
            merged[name] = dict(data)
            continue

        rule_value = str(rule_data.get("value") or "").strip()
        llm_value = str(data.get("value") or "").strip()
        if not llm_value:
            continue
        if not rule_value:
            merged[name] = dict(data)
            continue
        if _values_equal(name, rule_value, llm_value):
            # 两路一致：提升置信度，这是最可信的情形
            merged[name] = {
                **data,
                "confidence": min(1.0, max(float(data.get("confidence") or 0.0),
                                           float(rule_data.get("confidence") or 0.0)) + 0.05),
            }
            continue

        # 数值字段差异按容差判断
        if _numeric_diverges(name, rule_value, llm_value):
            conflicts.append(
                f"字段「{name}」模型识别为 {llm_value}，规则抽取为 {rule_value}，需核对。"
            )
        elif _looks_like_same_entity(name, rule_value, llm_value):
            pass  # 表述差异，不视为分歧
        else:
            conflicts.append(
                f"字段「{name}」模型识别为「{llm_value}」，规则抽取为「{rule_value}」，存在分歧。"
            )
        # 保留模型值，但在 locator 里标注规则侧的取值
        merged[name] = {
            **data,
            "locator": (str(data.get("locator") or "") + f"（规则侧：{rule_value}）").strip(),
        }

    return merged, conflicts


def _values_equal(name: str, a: str, b: str) -> bool:
    if a == b:
        return True
    if name in ("amount", "budget_amount", "contract_amount", "invoice_count"):
        na, nb = to_number(a), to_number(b)
        if na is not None and nb is not None:
            return abs(na - nb) <= max(0.01, abs(na) * _NUMERIC_TOLERANCE)
    return a.replace(" ", "").replace("　", "") == b.replace(" ", "").replace("　", "")


def _numeric_diverges(name: str, a: str, b: str) -> bool:
    if name not in ("amount", "budget_amount", "contract_amount"):
        return False
    na, nb = to_number(a), to_number(b)
    if na is None or nb is None:
        return False
    return abs(na - nb) > max(0.01, max(abs(na), abs(nb)) * _NUMERIC_TOLERANCE)


def _looks_like_same_entity(name: str, a: str, b: str) -> bool:
    """名称类字段的宽松匹配：一方包含另一方即视为同一实体。"""
    if name not in ("supplier", "project_name", "applicant", "approver", "budget_subject"):
        return False
    a_clean = a.replace("（", "(").replace("）", ")").strip()
    b_clean = b.replace("（", "(").replace("）", ")").strip()
    if not a_clean or not b_clean:
        return False
    return a_clean in b_clean or b_clean in a_clean


def _produced_by(baseline: dict[str, Any], llm: dict[str, Any], status: StageStatus) -> str:
    if status == StageStatus.SKIPPED or not llm:
        return "rule"
    if not baseline:
        return "llm"
    return "hybrid"


__all__ = ["Extractor", "ExtractionOutcome", "STAGE_NAME", "STAGE_TITLE"]
