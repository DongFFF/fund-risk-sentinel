"""领域枚举定义。

全部枚举都继承 ``str``，因此可直接参与 JSON 序列化、YAML 配置与 SQLite 存储，
无需额外转换。中文标签统一放在 ``*_LABELS`` 映射中，供界面展示复用。
"""

from __future__ import annotations

import re
from enum import Enum

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def _label_map_names(cls_name: str) -> tuple[str, str]:
    """由类名推出中文标签映射的候选名。

    ``EventType`` → ``EVENT_TYPE_LABELS``（规范写法）与 ``EVENTTYPE_LABELS``（历史写法）。
    两个都试，避免因命名风格不一致导致中文标签解析静默失效。
    """
    snake = _CAMEL_BOUNDARY.sub("_", cls_name).upper()
    return f"{snake}_LABELS", f"{cls_name.upper()}_LABELS"


class StrEnum(str, Enum):
    """字符串枚举基类：值即字符串，可直接比较、序列化。"""

    def __str__(self) -> str:  # pragma: no cover - 便捷方法
        return str(self.value)

    @classmethod
    def values(cls) -> list[str]:
        return [m.value for m in cls]

    @classmethod
    def from_value(cls, value: str | None, default: "StrEnum | None" = None):
        """宽松解析：接受枚举成员、值字符串、成员名或中文标签。"""
        if value is None:
            return default
        if isinstance(value, cls):
            return value
        text = str(value).strip()
        for member in cls:
            if text == member.value or text == member.name:
                return member
        for map_name in _label_map_names(cls.__name__):
            labels = globals().get(map_name)
            if not isinstance(labels, dict):
                continue
            for member, label in labels.items():
                if text == label:
                    return member
        return default


# ---------------------------------------------------------------- 五元框架：事项

class EventType(StrEnum):
    """事项类型：经费使用业务动作。"""

    BUDGET = "budget"                  # 预算编报
    ADVANCE = "advance"                # 借款预支
    REIMBURSEMENT = "reimbursement"    # 支出报销
    CONTRACT = "contract"              # 合同签订
    PROCUREMENT = "procurement"        # 采购执行
    TRANSFER = "transfer"              # 外拨转拨
    SETTLEMENT = "settlement"          # 结题结算
    OTHER = "other"


EVENT_TYPE_LABELS = {
    EventType.BUDGET: "预算编报",
    EventType.ADVANCE: "借款预支",
    EventType.REIMBURSEMENT: "支出报销",
    EventType.CONTRACT: "合同签订",
    EventType.PROCUREMENT: "采购执行",
    EventType.TRANSFER: "外拨转拨",
    EventType.SETTLEMENT: "结题结算",
    EventType.OTHER: "其他事项",
}


class RuleDimension(StrEnum):
    """规则维度：额度、用途、程序、时点、授权。"""

    AMOUNT = "amount"        # 额度
    PURPOSE = "purpose"      # 用途
    PROCEDURE = "procedure"  # 程序
    TIMING = "timing"        # 时点
    AUTHORITY = "authority"  # 授权


RULE_DIMENSION_LABELS = {
    RuleDimension.AMOUNT: "额度",
    RuleDimension.PURPOSE: "用途",
    RuleDimension.PROCEDURE: "程序",
    RuleDimension.TIMING: "时点",
    RuleDimension.AUTHORITY: "授权",
}


class EvidenceStatus(StrEnum):
    """证据状态：齐全、缺失、不一致。"""

    PRESENT = "present"            # 齐全
    MISSING = "missing"            # 缺失
    INCONSISTENT = "inconsistent"  # 不一致
    ILLEGIBLE = "illegible"        # 无法辨识


EVIDENCE_STATUS_LABELS = {
    EvidenceStatus.PRESENT: "齐全",
    EvidenceStatus.MISSING: "缺失",
    EvidenceStatus.INCONSISTENT: "不一致",
    EvidenceStatus.ILLEGIBLE: "无法辨识",
}


class RiskCategory(StrEnum):
    """风险类别：缺失、冲突、偏离、越权、异常组合。"""

    MISSING = "missing"      # 缺失（材料/字段/审批环节缺失）
    CONFLICT = "conflict"    # 冲突（跨材料信息矛盾）
    DEVIATION = "deviation"  # 偏离（超出预算/用途/标准）
    OVERRIDE = "override"    # 越权（超授权范围、程序倒置）
    ANOMALY = "anomaly"      # 异常组合（统计/组合层面的异常）


RISK_CATEGORY_LABELS = {
    RiskCategory.MISSING: "缺失",
    RiskCategory.CONFLICT: "冲突",
    RiskCategory.DEVIATION: "偏离",
    RiskCategory.OVERRIDE: "越权",
    RiskCategory.ANOMALY: "异常组合",
}


class RiskLevel(StrEnum):
    """风险等级。``UNKNOWN`` 用于证据不足、无法定级的情形。"""

    UNKNOWN = "unknown"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


RISK_LEVEL_LABELS = {
    RiskLevel.UNKNOWN: "待核验",
    RiskLevel.LOW: "低",
    RiskLevel.MEDIUM: "中",
    RiskLevel.HIGH: "高",
}

#: 等级排序权重，用于比较与排序（数值越大越严重）
RISK_LEVEL_WEIGHT = {
    RiskLevel.UNKNOWN: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
}


class DispositionType(StrEnum):
    """处置建议：补充、退回、限制、会商、放行留痕。"""

    SUPPLEMENT = "supplement"        # 补充材料
    RETURN = "return"                # 退回
    RESTRICT = "restrict"            # 限制
    CONSULT = "consult"              # 会商
    RELEASE = "release"              # 放行留痕
    PENDING_REVIEW = "pending_review"  # 待人工复核


DISPOSITION_TYPE_LABELS = {
    DispositionType.SUPPLEMENT: "补充材料",
    DispositionType.RETURN: "退回",
    DispositionType.RESTRICT: "限制",
    DispositionType.CONSULT: "会商",
    DispositionType.RELEASE: "放行留痕",
    DispositionType.PENDING_REVIEW: "待人工复核",
}


# ---------------------------------------------------------------- 三类核心能力

class Capability(StrEnum):
    """课题界定的三类核心能力。"""

    EXTRACTION = "extraction"    # ① 材料理解与结构化抽取
    RISK_REASONING = "risk_reasoning"  # ② 制度语义匹配与风险推理
    EXPLANATION = "explanation"  # ③ 结果解释与证据定位


CAPABILITY_LABELS = {
    Capability.EXTRACTION: "材料理解与结构化抽取",
    Capability.RISK_REASONING: "制度语义匹配与风险推理",
    Capability.EXPLANATION: "结果解释与证据定位",
}


class AdaptationPath(StrEnum):
    """四类适配路径。"""

    RULE_KB = "rule_kb"   # 结构化规则 + 知识检索
    RAG = "rag"           # 检索增强生成
    PROMPT = "prompt"     # 提示模板与指令工程
    PEFT = "peft"         # 参数高效微调（LoRA/QLoRA）


ADAPTATION_PATH_LABELS = {
    AdaptationPath.RULE_KB: "结构化规则+知识检索",
    AdaptationPath.RAG: "检索增强生成（RAG）",
    AdaptationPath.PROMPT: "提示模板与指令工程",
    AdaptationPath.PEFT: "参数高效微调（LoRA/QLoRA）",
}


# ---------------------------------------------------------------- 材料与流程

class DocumentFormat(StrEnum):
    """材料格式。"""

    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    CSV = "csv"
    JSON = "json"
    TXT = "txt"
    MARKDOWN = "md"
    IMAGE = "image"
    UNKNOWN = "unknown"


class DocumentRole(StrEnum):
    """材料在证据链中的角色，用于提示模板选择与规则绑定。"""

    APPLICATION = "application"    # 申请单 / 报销单
    CONTRACT = "contract"          # 合同
    INVOICE = "invoice"            # 发票 / 票据
    APPROVAL = "approval"          # 审批意见 / 签批
    BUDGET = "budget"              # 预算书 / 预算调整表
    ACCEPTANCE = "acceptance"      # 验收单 / 结题材料
    POLICY = "policy"              # 制度文本
    OTHER = "other"


DOCUMENT_ROLE_LABELS = {
    DocumentRole.APPLICATION: "申请/报销单",
    DocumentRole.CONTRACT: "合同",
    DocumentRole.INVOICE: "发票/票据",
    DocumentRole.APPROVAL: "审批意见",
    DocumentRole.BUDGET: "预算书",
    DocumentRole.ACCEPTANCE: "验收/结题材料",
    DocumentRole.POLICY: "制度文本",
    DocumentRole.OTHER: "其他",
}


class ReviewStatus(StrEnum):
    """审查任务状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_HUMAN = "needs_human"    # 已出线索，等待人工复核


REVIEW_STATUS_LABELS = {
    ReviewStatus.PENDING: "待处理",
    ReviewStatus.RUNNING: "审查中",
    ReviewStatus.COMPLETED: "已完成",
    ReviewStatus.FAILED: "失败",
    ReviewStatus.NEEDS_HUMAN: "待人工复核",
}


class HumanVerdict(StrEnum):
    """人工复核结论——用于评测的基准标签。"""

    PENDING = "pending"      # 尚未复核
    ACCEPTED = "accepted"    # 采纳
    PARTIAL = "partial"      # 部分采纳
    REJECTED = "rejected"    # 否决
    MISSED = "missed"        # 人工补充的新线索（模型漏报）


HUMAN_VERDICT_LABELS = {
    HumanVerdict.PENDING: "待复核",
    HumanVerdict.ACCEPTED: "采纳",
    HumanVerdict.PARTIAL: "部分采纳",
    HumanVerdict.REJECTED: "否决",
    HumanVerdict.MISSED: "模型漏报",
}


class MetricDimension(StrEnum):
    """评测体系的六个维度。"""

    ACCURACY = "accuracy"              # 准确性
    RECALL = "recall"                  # 召回性
    LATENCY = "latency"                # 时延
    CONSISTENCY = "consistency"        # 一致性
    EXPLAINABILITY = "explainability"  # 可解释性
    ADOPTION = "adoption"              # 审核采纳度


METRIC_DIMENSION_LABELS = {
    MetricDimension.ACCURACY: "准确性",
    MetricDimension.RECALL: "召回性",
    MetricDimension.LATENCY: "时延",
    MetricDimension.CONSISTENCY: "一致性",
    MetricDimension.EXPLAINABILITY: "可解释性",
    MetricDimension.ADOPTION: "审核采纳度",
}


class StageStatus(StrEnum):
    """流水线单阶段执行状态。"""

    OK = "ok"
    DEGRADED = "degraded"    # 降级完成（如模型不可用，退回规则结果）
    SKIPPED = "skipped"
    FAILED = "failed"


__all__ = [
    "StrEnum",
    "EventType", "EVENT_TYPE_LABELS",
    "RuleDimension", "RULE_DIMENSION_LABELS",
    "EvidenceStatus", "EVIDENCE_STATUS_LABELS",
    "RiskCategory", "RISK_CATEGORY_LABELS",
    "RiskLevel", "RISK_LEVEL_LABELS", "RISK_LEVEL_WEIGHT",
    "DispositionType", "DISPOSITION_TYPE_LABELS",
    "Capability", "CAPABILITY_LABELS",
    "AdaptationPath", "ADAPTATION_PATH_LABELS",
    "DocumentFormat", "DocumentRole", "DOCUMENT_ROLE_LABELS",
    "ReviewStatus", "REVIEW_STATUS_LABELS",
    "HumanVerdict", "HUMAN_VERDICT_LABELS",
    "MetricDimension", "METRIC_DIMENSION_LABELS",
    "StageStatus",
]
